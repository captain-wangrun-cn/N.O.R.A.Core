from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .constants import API_BASE, INTENTS, TOKEN_URL

logger = logging.getLogger(__name__)


class QQBotAPIError(RuntimeError):
    def __init__(self, status: int, code: int = 0, retry_after: float = 0, trace_id: str = "") -> None:
        # 平台 message/请求 URL 可能含 token、rkey，异常只暴露状态和错误码。
        # trace_id 是官方响应头 X-Tps-Trace-Id，供联系开放平台排查，不含敏感信息。
        super().__init__(f"QQ API failed: HTTP {status}, code {code}")
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.trace_id = trace_id


class QQBotClient:
    """共享 HTTP 会话、互斥 token 刷新及可恢复 WebSocket。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        session: Any = None,
        request_timeout: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.session = session
        self._owns_session = session is None
        self.request_timeout = request_timeout
        self.clock = clock
        self._token = ""
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None
        self._awaiting_ack = False
        self.session_id = ""
        self.seq: int | None = None
        self.ready_user: dict[str, Any] = {}

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
            )

    async def access_token(self, *, rejected_token: str = "") -> str:
        async with self._token_lock:
            if self._token and self.clock() < self._refresh_at and self._token != rejected_token:
                return self._token
            await self.start()
            async with self.session.post(
                TOKEN_URL, json={"appId": self.app_id, "clientSecret": self.app_secret},
                allow_redirects=False,
            ) as response:
                try:
                    data = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise QQBotAPIError(response.status) from None
                if response.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
                    raise QQBotAPIError(response.status, int(data.get("code") or 0) if isinstance(data, dict) else 0)
                expires = max(1, int(data.get("expires_in") or 7200))
                self._token = str(data["access_token"])
                self._refresh_at = self.clock() + max(1, expires - min(60, expires / 2))
                return self._token

    async def request(self, method: str, path: str, *, json: Any = None) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("QQ API path must be relative")
        token = await self.access_token()
        for attempt in range(2):
            async with self.session.request(
                method, API_BASE + path, json=json,
                headers={"Authorization": f"QQBot {token}"}, allow_redirects=False,
            ) as response:
                trace_id = str(response.headers.get("X-Tps-Trace-Id") or "")
                if response.status == 401 and attempt == 0:
                    token = await self.access_token(rejected_token=token)
                    continue
                if response.status == 204:
                    return {}
                try:
                    data = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise QQBotAPIError(response.status, trace_id=trace_id) from None
                code = int(data.get("code") or 0) if isinstance(data, dict) else 0
                if not 200 <= response.status < 300 or code:
                    try:
                        retry_after = float(response.headers.get("Retry-After", 0))
                    except (ValueError, TypeError):
                        retry_after = 0
                    raise QQBotAPIError(response.status, code, retry_after, trace_id=trace_id)
                if not isinstance(data, dict):
                    raise QQBotAPIError(response.status, trace_id=trace_id)
                return data
        raise QQBotAPIError(401)

    async def _heartbeat(self, interval: float) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(interval)
            if self._awaiting_ack:
                await self._ws.close()
                return
            self._awaiting_ack = True
            await self._ws.send_json({"op": 1, "d": self.seq})

    async def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _receive(
        self,
        ws: Any,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        on_ready: Callable[[], Awaitable[None]],
    ) -> None:
        self._ws = ws
        self._awaiting_ack = False
        hello_received = False
        async for frame in ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                if frame.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    break
                continue
            packet = frame.json()
            op = packet.get("op")
            if op == 10:
                if hello_received:
                    continue
                hello_received = True
                interval = max(1, float(packet["d"]["heartbeat_interval"]) / 1000)
                token = await self.access_token()
                if self.session_id and self.seq is not None:
                    logger.info("[qqbot] gateway resume requested")
                    await ws.send_json({"op": 6, "d": {
                        "token": f"QQBot {token}", "session_id": self.session_id, "seq": self.seq,
                    }})
                else:
                    logger.info("[qqbot] gateway identify: intents=%d", INTENTS)
                    await ws.send_json({"op": 2, "d": {
                        "token": f"QQBot {token}", "intents": INTENTS,
                        "shard": [0, 1], "properties": {},
                    }})
                self._heartbeat_task = asyncio.create_task(self._heartbeat(interval))
            elif op == 11:
                self._awaiting_ack = False
            elif op == 1:
                await ws.send_json({"op": 1, "d": self.seq})
            elif op == 7:
                return
            elif op == 9:
                if not packet.get("d"):
                    self.session_id = ""
                    self.seq = None
                return
            elif op == 0:
                event_type = packet.get("t")
                if event_type == "READY":
                    self.session_id = str(packet["d"]["session_id"])
                    self.ready_user = dict(packet["d"].get("user") or {})
                    logger.info("[qqbot] gateway READY received")
                    await on_ready()
                elif event_type == "RESUMED":
                    logger.info("[qqbot] gateway RESUMED received")
                    await on_ready()
                else:
                    # handler 只入有界队列；入队失败不推进 seq，重连后重放。
                    await on_event(packet)
                if packet.get("s") is not None:
                    self.seq = int(packet["s"])

    async def run(
        self,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        on_ready: Callable[[], Awaitable[None]],
    ) -> None:
        await self.start()
        backoff = 1.0
        while not self._closed.is_set():
            try:
                gateway = await self.request("GET", "/gateway/bot")
                url = str(gateway.get("url") or "")
                if not url.startswith("wss://"):
                    raise ValueError("QQ gateway must use wss")
                async with self.session.ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_receive=120)) as ws:
                    await self._receive(ws, on_event, on_ready)
                backoff = 1.0 if self.session_id else min(30, backoff * 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[qqbot] gateway disconnected (%s); reconnecting", type(exc).__name__)
                backoff = min(30, backoff * 2)
            finally:
                await self._stop_heartbeat()
                self._ws = None
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._closed.set()
        if self._ws is not None:
            await self._ws.close()
        await self._stop_heartbeat()
        if self._owns_session and self.session is not None:
            await self.session.close()
