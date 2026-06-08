from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict

from aiohttp import web

from adapters.aggregator import MessageAggregator
from adapters.base import BaseAdapter, PlatformFeatures
from adapters.config_utils import load_adapter_config
from memory.message_history import MessageHistory
from workspace_config import get_workspace_manager

from .client import OneBotWebSocketClient
from .constants import ONEBOT_MSG_MAX_LENGTH
from .media import OneBotMediaMixin
from .message import (
    is_at_self,
    onebot_event_to_adapter_event,
    onebot_message_segments,
    remove_empty_text_edges,
    strip_at_self,
)
from .sender import OneBotSenderMixin
from .tools import OneBotV11ToolsMixin

logger = logging.getLogger(__name__)


DEFAULT_CONFIG = {
    "connection_type": "websocket",
    "websocket_url": "ws://127.0.0.1:6700/",
    "reverse_host": "127.0.0.1",
    "reverse_port": 6701,
    "reverse_path": "/onebot/v11",
    "access_token": "",
    "self_id": "",
    "nickname": "Nora",
    "group_message_policy": "at_or_reply",
    "enable_napcat_api": False,
    "download_media": True,
    "reconnect_interval_seconds": 5,
}


class OneBotV11Adapter(
    OneBotMediaMixin,
    OneBotSenderMixin,
    OneBotV11ToolsMixin,
    BaseAdapter,
):
    """OneBot v11 adapter using WebSocket/Reverse WebSocket."""

    @property
    def platform_name(self) -> str:
        return "onebotv11"

    @property
    def platform_features(self) -> PlatformFeatures:
        return PlatformFeatures(
            supports_edit_message=False,
            supports_delete_message=True,
            supports_reactions=False,
            supports_typing_indicator=False,
            supports_rich_media=True,
            supports_reply=True,
            supports_threads=False,
            supports_buttons=False,
            supports_chat_member_lookup=True,
            supports_chat_moderation=True,
            supports_member_tags=False,
            max_message_length=ONEBOT_MSG_MAX_LENGTH,
        )

    def __init__(self):
        super().__init__()
        self.config = load_adapter_config("onebotv11", DEFAULT_CONFIG)
        workspace_manager = get_workspace_manager()
        self.workspace_root = str(workspace_manager.root)
        self.downloads_dir = str(workspace_manager.downloads_dir)
        self.data_dir = str(workspace_manager.data_dir)
        self.onebot_data_dir = os.path.join(self.data_dir, "onebotv11")
        os.makedirs(self.onebot_data_dir, exist_ok=True)

        self.connection_type = str(self.config.get("connection_type") or "websocket").strip().lower()
        self.websocket_url = str(self.config.get("websocket_url") or "").strip()
        self.access_token = str(self.config.get("access_token") or "").strip()
        self.self_id = str(self.config.get("self_id") or "").strip()
        self.nickname = str(self.config.get("nickname") or "Nora").strip()
        self.group_message_policy = str(self.config.get("group_message_policy") or "at_or_reply").strip().lower()
        self.enable_napcat_api = bool(self.config.get("enable_napcat_api", False))
        self.download_media = bool(self.config.get("download_media", True))
        self.reconnect_interval = max(1, int(self.config.get("reconnect_interval_seconds") or 5))

        self.client = OneBotWebSocketClient(self)
        self._aggregator: MessageAggregator | None = None
        self.message_history = MessageHistory()
        self._chat_types: dict[str, str] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ready_fired = False

    def run(self, message_handler: Callable[[Dict[str, Any]], Any]):
        self._message_handler = message_handler
        import config

        app_config = config.get_config()
        interaction_config = app_config.get("interaction", {}) if app_config else {}
        buffer_timeout = interaction_config.get("buffer_timeout", 3.0)
        self._aggregator = MessageAggregator(
            timeout=buffer_timeout,
            on_complete=self.on_aggregator_complete,
        )
        logger.info("[onebotv11] Adapter starting with connection_type=%s", self.connection_type)
        try:
            asyncio.run(self._run_forever())
        except KeyboardInterrupt:
            logger.info("[onebotv11] Adapter stopped by keyboard interrupt.")

    async def _run_forever(self) -> None:
        await self.startup()
        try:
            if self.connection_type in {"reverse", "reverse_websocket", "ws_reverse"}:
                await self._run_reverse_server()
            else:
                await self._run_websocket_loop()
        finally:
            await self.shutdown()

    async def _run_websocket_loop(self) -> None:
        if not self.websocket_url:
            raise ValueError("onebotv11 websocket_url is required for connection_type=websocket.")
        while True:
            try:
                await self.client.connect(self.websocket_url, self.access_token)
                await self._init_self_info()
                await self._notify_ready_once()
                await self.client.listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[onebotv11] WebSocket loop error: %s", exc, exc_info=True)
            finally:
                await self.client.close()
            await asyncio.sleep(self.reconnect_interval)

    async def _run_reverse_server(self) -> None:
        app = web.Application()
        app.router.add_get(str(self.config.get("reverse_path") or "/onebot/v11"), self._reverse_ws_handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        host = str(self.config.get("reverse_host") or "127.0.0.1")
        port = int(self.config.get("reverse_port") or 6701)
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        logger.info("[onebotv11] Reverse WebSocket server listening on ws://%s:%s%s", host, port, self.config.get("reverse_path") or "/onebot/v11")
        while True:
            await asyncio.sleep(3600)

    async def _reverse_ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if self.access_token:
            auth = request.headers.get("Authorization", "")
            expected = f"Bearer {self.access_token}"
            if auth != expected:
                logger.warning("[onebotv11] Reverse WebSocket authorization failed.")
                return web.Response(status=401, text="Unauthorized")
        header_self_id = request.headers.get("X-Self-ID", "")
        if header_self_id:
            self.self_id = str(header_self_id)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        await self.client.attach(ws)
        await self._init_self_info(best_effort=True)
        await self._notify_ready_once()
        try:
            await self.client.listen()
        finally:
            await self.client.close()
        return ws

    async def _init_self_info(self, best_effort: bool = False) -> None:
        if self.self_id:
            return
        try:
            response = await self.call_api("get_login_info", {})
            data = response.get("data") or {}
            if data.get("user_id") is not None:
                self.self_id = str(data.get("user_id"))
            if data.get("nickname"):
                self.nickname = str(data.get("nickname"))
            logger.info("[onebotv11] login self_id=%s nickname=%s", self.self_id, self.nickname)
        except Exception:
            if not best_effort:
                logger.warning("[onebotv11] get_login_info failed.", exc_info=True)

    async def _notify_ready_once(self) -> None:
        if self._ready_fired:
            return
        self._ready_fired = True
        await self.on_ready()

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.client.call_api(action, params or {})

    def _message_triggers_bot(self, event: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
        message_type = str(event.get("message_type") or "private")
        if message_type == "private":
            return True
        policy = self.group_message_policy
        if policy in {"all", "always"}:
            return True
        if policy in {"none", "never"}:
            return False
        at_me = is_at_self(segments, self.self_id)
        if at_me:
            return True
        if policy in {"at", "mention"}:
            return False
        reply_to_me = str(event.get("reply_to_user_id") or "") == str(self.self_id)
        return bool(reply_to_me)

    async def handle_onebot_event(self, event: dict[str, Any]) -> None:
        post_type = str(event.get("post_type") or "")
        if post_type != "message":
            return
        message_type = str(event.get("message_type") or "private")
        raw_message = str(event.get("raw_message") or "")
        segments = onebot_message_segments(event.get("message"), raw_message=raw_message)
        await self._enrich_reply_context(event, segments)
        if not self._message_triggers_bot(event, segments):
            return
        if message_type == "group":
            segments = remove_empty_text_edges(strip_at_self(segments, self.self_id))

        text = await self.segments_to_nora_text(segments)
        if not text:
            return
        adapter_event = onebot_event_to_adapter_event(event, text=text)
        context = adapter_event.to_context()
        context["self_id"] = self.self_id
        if event.get("reply_to_message_id") is not None:
            context["reply_to_message_id"] = str(event.get("reply_to_message_id"))
        if event.get("reply_to_user_id") is not None:
            context["reply_to_user_id"] = str(event.get("reply_to_user_id"))
            context["reply_to_user_name"] = str(event.get("reply_to_user_name") or "")

        chat_id = context["chat_id"]
        self.current_chat_id = chat_id
        self._chat_types[chat_id] = context["chat_type"]
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text, context)

    async def _enrich_reply_context(self, event: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        reply_id = ""
        for segment in segments:
            if str(segment.get("type") or "") == "reply":
                reply_id = str((segment.get("data") or {}).get("id") or "")
                break
        if not reply_id:
            return
        event["reply_to_message_id"] = reply_id
        try:
            response = await self.call_api("get_msg", {"message_id": int(reply_id)})
            data = response.get("data") or {}
            sender = data.get("sender") or {}
            user_id = sender.get("user_id")
            if user_id is not None:
                event["reply_to_user_id"] = str(user_id)
            name = sender.get("card") or sender.get("nickname") or ""
            if name:
                event["reply_to_user_name"] = str(name)
        except Exception as exc:
            logger.debug("[onebotv11] get reply message failed: %s", exc)

    async def on_aggregator_complete(self, context: Dict[str, Any]):
        if not self._message_handler:
            return
        processed_context = await self.before_message(context)
        try:
            result = self._message_handler(processed_context)
            if asyncio.iscoroutine(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            chat_id = processed_context.get("chat_id")
            logger.error("[onebotv11] message handling failed: %s", exc, exc_info=True)
            if chat_id:
                try:
                    await self.send_message(chat_id, "抱歉，处理请求时发生错误，请稍后重试。", parse_media=False)
                except Exception:
                    logger.debug("[onebotv11] failed to send error message.", exc_info=True)
            return
        await self.after_message(processed_context, result)

    async def shutdown(self) -> None:
        logger.info("[onebotv11] shutting down adapter...")
        await self.client.close()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        await super().shutdown()

    async def health_check(self) -> Dict[str, Any]:
        base = await super().health_check()
        base["self_id"] = self.self_id
        base["connected"] = self.client.connected
        try:
            status = await self.call_api("get_status", {})
            base["onebot_status"] = status.get("data")
            base["ok"] = True
        except Exception:
            base["ok"] = False
        return base
