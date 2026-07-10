from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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
    native_reference_markers,
    onebot_event_to_adapter_event,
    onebot_message_segments,
    remove_empty_text_edges,
    reply_id_from_event,
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

MENTION_ONLY_TEXT = "[群聊单独艾特: 用户只艾特了你，没有附加文字。请自然地回应。]"
HISTORICAL_REPLY_MEDIA_NOTE = (
    "[系统备注] 用户回复的是一条历史 QQ 消息；如果上方包含图片、视频或文件媒体标记，"
    "这些媒体就是被回复消息里的真实内容。不要复述本系统备注，直接根据被引用内容自然回答。"
)


class OneBotV11Adapter(
    OneBotMediaMixin,
    OneBotSenderMixin,
    OneBotV11ToolsMixin,
    BaseAdapter,
):
    """OneBot v11 adapter using WebSocket/Reverse WebSocket."""

    GROUP_CONTEXT_CACHE_TTL_SECONDS = 300

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
            supports_member_tags=True,
            supports_message_controls=True,
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
        self._group_context_cache_data: dict[str, dict[str, Any]] = {}
        self._last_incoming_contexts: dict[str, dict[str, str]] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ready_fired = False

    def run(self, message_handler: Callable[[Dict[str, Any]], Any]):
        self._configure(message_handler)
        logger.info("[onebotv11] Adapter starting with connection_type=%s", self.connection_type)
        try:
            asyncio.run(self._run_forever())
        except KeyboardInterrupt:
            logger.info("[onebotv11] Adapter stopped by keyboard interrupt.")

    async def run_async(self, message_handler: Callable[[Dict[str, Any]], Any]) -> None:
        """并发入口：在调用方事件循环内常驻，不自建循环。"""
        self._configure(message_handler)
        logger.info("[onebotv11] Adapter starting (async) with connection_type=%s", self.connection_type)
        await self._run_forever()

    def _configure(self, message_handler: Callable[[Dict[str, Any]], Any]) -> None:
        """初始化 handler 与聚合器（run 与 run_async 共用）。"""
        self._message_handler = message_handler
        import config

        app_config = config.get_config()
        interaction_config = app_config.get("interaction", {}) if app_config else {}
        buffer_timeout = interaction_config.get("buffer_timeout", 3.0)
        self._aggregator = MessageAggregator(
            timeout=buffer_timeout,
            on_complete=self.on_aggregator_complete,
        )

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

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _get_group_context_snapshot(self, group_id: str) -> dict[str, Any]:
        group_id = str(group_id or "").strip()
        if not group_id:
            return {"status": "unavailable:no_group_id"}

        now = time.time()
        cached = self._group_context_cache_data.get(group_id)
        if cached and now - float(cached.get("ts", 0.0)) < self.GROUP_CONTEXT_CACHE_TTL_SECONDS:
            return dict(cached.get("data") or {})

        try:
            response = await self.call_api("get_group_info", {"group_id": int(group_id), "no_cache": False})
            data = response.get("data") or {}
            snapshot = {
                "status": "ok",
                "group_name": str(data.get("group_name") or "").strip(),
                "member_count": self._optional_int(data.get("member_count")),
                "max_member_count": self._optional_int(data.get("max_member_count")),
            }
        except Exception as exc:
            snapshot = {"status": f"error:{type(exc).__name__}"}
            logger.debug("[onebotv11] get_group_info failed for group_id=%s: %s", group_id, exc)

        self._group_context_cache_data[group_id] = {"ts": now, "data": snapshot}
        return snapshot

    async def _enrich_group_context(self, context: dict[str, Any]) -> dict[str, Any]:
        if str(context.get("chat_type") or "").lower() != "group":
            return context

        enriched = dict(context)
        group_id = str(enriched.get("chat_id") or "").strip()
        snapshot = await self._get_group_context_snapshot(group_id)
        group_name = str(snapshot.get("group_name") or "").strip()
        if group_name:
            enriched["chat_title"] = group_name
            enriched["group_title"] = group_name
            enriched["group_display_name"] = group_name

        enriched["group_member_count"] = snapshot.get("member_count")
        enriched["group_member_count_status"] = str(snapshot.get("status") or "unknown")
        enriched["group_max_member_count"] = snapshot.get("max_member_count")
        enriched["group_online_count"] = None
        enriched["group_online_count_status"] = "unavailable:onebotv11_standard"
        return enriched

    def _remember_incoming_context(self, chat_id: str, context: dict[str, Any]) -> None:
        remembered: dict[str, str] = {}
        for key in (
            "platform_message_id",
            "user_id",
            "user_name",
            "reply_to_message_id",
            "reply_to_user_id",
            "reply_to_user_name",
        ):
            value = context.get(key)
            if value is not None and str(value).strip():
                remembered[key] = str(value)
        if remembered:
            self._last_incoming_contexts[str(chat_id)] = remembered

    async def handle_onebot_event(self, event: dict[str, Any]) -> None:
        post_type = str(event.get("post_type") or "")
        if post_type != "message":
            return
        message_type = str(event.get("message_type") or "private")
        raw_message = str(event.get("raw_message") or "")
        segments = onebot_message_segments(event.get("message"), raw_message=raw_message)
        segment_reply_id = reply_id_from_event({}, segments)
        raw_reply_segments = [seg for seg in onebot_message_segments(None, raw_message=raw_message) if str(seg.get("type") or "") == "reply"]
        if not segment_reply_id and raw_reply_segments:
            segments = raw_reply_segments + segments
            logger.debug(
                "[onebotv11] reply segment recovered from raw_message message_id=%s raw=%s",
                event.get("message_id"),
                raw_message,
            )
        await self._enrich_reply_context(event, segments, include_text=False)
        has_reply = bool(event.get("reply_to_message_id"))
        mention_only_trigger = message_type == "group" and is_at_self(segments, self.self_id) and not has_reply
        if mention_only_trigger:
            logger.debug(
                "[onebotv11] mention-only trigger message_id=%s event=%s",
                event.get("message_id"),
                json.dumps(event, ensure_ascii=False, default=str)[:2000],
            )
        if not self._message_triggers_bot(event, segments):
            return
        await self._enrich_reply_text(event)
        reference_markers, mentioned_user_ids = native_reference_markers(
            segments,
            self_id=self.self_id,
            include_mentions=message_type == "group",
        )
        if message_type == "group":
            segments = remove_empty_text_edges(strip_at_self(segments, self.self_id))
            segments = [
                seg
                for seg in segments
                if str(seg.get("type") or "") not in {"reply", "at"}
            ]

        text = await self.segments_to_nora_text(segments)
        reply_text = str(event.get("reply_to_text") or "").strip()
        if reply_text:
            text = f"[回复内容]\n{reply_text}\n[/回复内容]\n{text}".strip()
        if reference_markers:
            text = f"{reference_markers}{text}"
        if not text and mention_only_trigger:
            text = MENTION_ONLY_TEXT
        if not text:
            return
        adapter_event = onebot_event_to_adapter_event(event, text=text)
        context = adapter_event.to_context()
        context["self_id"] = self.self_id
        if event.get("reply_to_message_id") is not None:
            context["reply_to_message_id"] = str(event.get("reply_to_message_id"))
            if event.get("reply_to_contains_media"):
                platform_ids = [str(event.get("reply_to_message_id"))]
                current_id = context.get("platform_message_id")
                if current_id is not None and str(current_id) not in platform_ids:
                    platform_ids.append(str(current_id))
                context["platform_message_ids"] = platform_ids
        if event.get("reply_to_user_id") is not None:
            context["reply_to_user_id"] = str(event.get("reply_to_user_id"))
            context["reply_to_user_name"] = str(event.get("reply_to_user_name") or "")
        if mentioned_user_ids:
            context["mentioned_user_ids"] = mentioned_user_ids
        context = await self._enrich_group_context(context)

        chat_id = context["chat_id"]
        self.current_chat_id = chat_id
        self._chat_types[chat_id] = context["chat_type"]
        self._remember_incoming_context(chat_id, context)
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text, context)

    async def _enrich_reply_context(
        self,
        event: dict[str, Any],
        segments: list[dict[str, Any]],
        *,
        include_text: bool = True,
    ) -> None:
        reply_id = reply_id_from_event(event, segments)
        if not reply_id:
            return
        event["reply_to_message_id"] = reply_id
        try:
            response = await self.call_api("get_msg", {"message_id": int(reply_id)})
            data = response.get("data") or {}
            event["_reply_message_data"] = data
            sender = data.get("sender") or {}
            user_id = sender.get("user_id")
            if user_id is not None:
                event["reply_to_user_id"] = str(user_id)
            name = sender.get("card") or sender.get("nickname") or ""
            if name:
                event["reply_to_user_name"] = str(name)
            if include_text:
                await self._enrich_reply_text(event)
        except Exception as exc:
            logger.debug("[onebotv11] get reply message failed: %s", exc)

    async def _enrich_reply_text(self, event: dict[str, Any]) -> None:
        if event.get("reply_to_text"):
            return
        data = event.get("_reply_message_data")
        if not isinstance(data, dict):
            return
        reply_segments = onebot_message_segments(data.get("message"), raw_message=str(data.get("raw_message") or ""))
        reply_text = await self.segments_to_nora_text(reply_segments)
        if not reply_text:
            return
        if any(str(segment.get("type") or "") in {"image", "video", "record", "file"} for segment in reply_segments):
            reply_text = f"{reply_text}\n{HISTORICAL_REPLY_MEDIA_NOTE}"
            event["reply_to_contains_media"] = True
        event["reply_to_text"] = reply_text

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
