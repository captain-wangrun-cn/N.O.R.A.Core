from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

logger = logging.getLogger(__name__)


class OneBotAPIError(RuntimeError):
    def __init__(self, action: str, response: dict[str, Any]):
        self.action = action
        self.response = response
        super().__init__(f"OneBot action {action!r} failed: {response}")


class OneBotWebSocketClient:
    """Small OneBot v11 WebSocket client for both API calls and event receiving."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self.ws = None
        self.session: ClientSession | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self.ws and not self.ws.closed)

    async def connect(self, url: str, access_token: str = "") -> None:
        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self.session = ClientSession(timeout=ClientTimeout(total=None))
        self.ws = await self.session.ws_connect(url, headers=headers, heartbeat=30)
        logger.info("[onebotv11] WebSocket connected: %s", url)

    async def attach(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        logger.info("[onebotv11] Reverse WebSocket attached.")

    async def listen(self) -> None:
        if not self.ws:
            raise RuntimeError("OneBot WebSocket is not connected.")
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                continue
            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.debug("[onebotv11] Ignoring non-json ws payload: %s", msg.data[:200])
                continue
            await self.handle_payload(payload)

    async def handle_payload(self, payload: dict[str, Any]) -> None:
        echo = payload.get("echo")
        if echo is not None and str(echo) in self.pending:
            future = self.pending.pop(str(echo))
            if not future.done():
                future.set_result(payload)
            return
        if isinstance(payload, dict) and payload.get("post_type"):
            await self.adapter.handle_onebot_event(payload)

    async def call_api(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not self.ws or self.ws.closed:
            raise RuntimeError("OneBot WebSocket is not connected.")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        request = {"action": action, "params": params or {}, "echo": echo}
        async with self._lock:
            await self.ws.send_str(json.dumps(request, ensure_ascii=False))
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(echo, None)

        status = str(response.get("status") or "")
        retcode = response.get("retcode")
        failed_status = bool(status and status not in {"ok", "async"})
        failed_retcode = bool(retcode not in {0, None} and status != "async")
        if failed_status or failed_retcode:
            raise OneBotAPIError(action, response)
        return response

    async def close(self) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        self.pending.clear()
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session:
            await self.session.close()
        self.ws = None
        self.session = None
