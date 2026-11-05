from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from adapters.aggregator import MessageAggregator
from adapters.base import BaseAdapter, MessageHandler, PlatformFeatures
from adapters.config_utils import is_placeholder_value, load_adapter_config
from adapters.delivery import DeliveryContext

from .client import QQBotClient
from .constants import DEFAULT_CONFIG, MESSAGE_EVENTS
from .interactions import InteractionStore
from .media import QQBotMedia
from .message import attachment_type, event_to_context, scene_ext
from .sender import QQBotSender
from .state import QQBotState

logger = logging.getLogger(__name__)


def _scene_log_id(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode()).hexdigest()[:12] if chat_id else "missing"


class QQBotAdapter(BaseAdapter):
    """QQ 官方 API v2 adapter；全群接收仍遵守 Core 的在线状态。

    事件按场景分链处理：同群/同私聊内保持到达顺序，跨场景并发，
    生成轮与媒体下载不再阻塞其它聊天的入站管线（对齐 ob11 模型）。
    """

    notify_delivery_failure = True

    def __init__(self, adapter_config: dict[str, Any] | None = None, *, workspace_root: Path | None = None) -> None:
        super().__init__()
        self.config = {**DEFAULT_CONFIG, **(adapter_config if adapter_config is not None else load_adapter_config("qqbot", DEFAULT_CONFIG))}
        self.workspace_root = workspace_root
        self.self_id = str(self.config.get("self_id") or "")
        self.state = QQBotState(int(self.config["cache_size"]))
        self.interactions = InteractionStore()
        self.client = QQBotClient(str(self.config["app_id"]), str(self.config["app_secret"]), request_timeout=float(self.config["request_timeout_seconds"]))
        self.media: QQBotMedia | None = None
        self.sender: QQBotSender | None = None
        self._aggregator: MessageAggregator | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(self.config["event_queue_size"])))
        self._worker: asyncio.Task | None = None
        self._event_tasks: set[asyncio.Task] = set()
        self._event_chains: dict[str, asyncio.Task] = {}
        self._interaction_tasks: set[asyncio.Task] = set()
        self._chat_types: dict[str, str] = {}
        self._blocked_scenes: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._ready_fired = False
        self._group_message_handler = None
        self._group_presence_for = None

    @property
    def platform_name(self) -> str:
        return "qqbot"

    @property
    def platform_features(self) -> PlatformFeatures:
        return PlatformFeatures(
            supports_edit_message=False, supports_delete_message=True,
            supports_typing_indicator=False, supports_rich_media=True,
            supports_reply=True, supports_buttons=True, supports_message_controls=True,
        )

    def run(self, message_handler: MessageHandler) -> None:
        try:
            asyncio.run(self.run_async(message_handler))
        except KeyboardInterrupt:
            pass

    def _configure(self, message_handler: MessageHandler) -> None:
        self._message_handler = message_handler
        controller = getattr(message_handler, "__self__", None)
        self._group_message_handler = getattr(controller, "submit_group_message", None)
        self._group_presence_for = getattr(controller, "group_presence_for", None)
        logger.info("[qqbot] group hooks: handler=%s presence=%s", callable(self._group_message_handler), callable(self._group_presence_for))
        import config
        cfg = config.get_config() or {}
        self._aggregator = MessageAggregator(
            float((cfg.get("interaction") or {}).get("buffer_timeout", 3)), self.on_aggregator_complete,
        )
        if self.workspace_root is None:
            from workspace_config import get_workspace_manager
            self.workspace_root = Path(get_workspace_manager().root)
        self.media = QQBotMedia(self.client, self.workspace_root, max_bytes=int(self.config["max_media_bytes"]))
        self.sender = QQBotSender(self.client, self.state, self.media, chunk_size=int(self.config["text_chunk_size"]))

    async def run_async(self, message_handler: MessageHandler) -> None:
        if is_placeholder_value(self.config["app_id"]) or is_placeholder_value(self.config["app_secret"]):
            raise ValueError("Configure app_id/app_secret in adapters/qqbot/config.json")
        self._configure(message_handler)
        await self.startup()
        self._worker = asyncio.create_task(self._consume_events())
        try:
            await self.client.run(self._enqueue_event, self._on_gateway_ready)
        finally:
            await self.shutdown()

    async def _on_gateway_ready(self) -> None:
        self.self_id = str(self.config.get("self_id") or self.client.ready_user.get("id") or self.self_id)
        logger.info("[qqbot] gateway ready: self_id_available=%s source=%s", bool(self.self_id), "config" if self.config.get("self_id") else "ready")
        if not self._ready_fired:
            self._ready_fired = True
            await self.on_ready()

    async def _enqueue_event(self, packet: dict[str, Any]) -> None:
        logger.info("[qqbot] gateway dispatch: event=%s queue_depth=%d", str(packet.get("t") or "")[:80], self._event_queue.qsize())
        if packet.get("t") == "INTERACTION_CREATE":
            # 互动 ACK 不排在媒体/模型后面，业务仍走绑定 token 的独立入口。
            if len(self._interaction_tasks) >= 32:
                raise RuntimeError("QQ interaction queue full")
            task = asyncio.create_task(self.handle_event(packet))
            self._interaction_tasks.add(task)
            task.add_done_callback(self._interaction_done)
        else:
            self._event_queue.put_nowait(packet)

    def _interaction_done(self, task: asyncio.Task) -> None:
        self._interaction_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning("[qqbot] interaction failed (%s)", type(task.exception()).__name__)

    async def _consume_events(self) -> None:
        while True:
            packet = await self._event_queue.get()
            self._event_queue.task_done()
            self._dispatch_event(packet)

    @staticmethod
    def _chain_key_for(packet: dict[str, Any]) -> str:
        if str(packet.get("t") or "") not in MESSAGE_EVENTS:
            # 关系变更等控制事件轻量，共用一条链保持相互之间的顺序。
            return "control"
        data = packet.get("d") or {}
        if data.get("group_openid"):
            return f"group:{data['group_openid']}"
        openid = str(data.get("user_openid") or (data.get("author") or {}).get("user_openid") or "")
        return f"c2c:{openid}" if openid else ""

    def _dispatch_event(self, packet: dict[str, Any]) -> None:
        key = self._chain_key_for(packet)
        previous = self._event_chains.get(key) if key else None
        if previous is not None and previous.done():
            previous = None
        task = asyncio.create_task(self._run_event_in_order(packet, previous))
        if key:
            self._event_chains[key] = task
        self._event_tasks.add(task)
        task.add_done_callback(self._on_event_task_done)

    async def _run_event_in_order(self, packet: dict[str, Any], previous: asyncio.Task | None) -> None:
        if previous is not None and not previous.done():
            try:
                await previous
            except Exception:
                pass
        await self.handle_event(packet)

    def _on_event_task_done(self, task: asyncio.Task) -> None:
        self._event_tasks.discard(task)
        for key, chained in list(self._event_chains.items()):
            if chained is task:
                self._event_chains.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("[qqbot] event processing failed (%s)", type(exc).__name__)

    async def handle_event(self, packet: dict[str, Any]) -> None:
        event_type = str(packet.get("t") or "")
        if event_type == "INTERACTION_CREATE":
            from .interactions import handle_interaction
            await handle_interaction(self, packet)
            return
        if event_type not in MESSAGE_EVENTS:
            relation_scene = "group" if event_type.startswith("GROUP_") else "private"
            data = packet.get("d") or {}
            target_id = str(data.get("group_openid") or "") if relation_scene == "group" else str(data.get("openid") or data.get("user_openid") or "")
            if event_type in {"FRIEND_DEL", "C2C_MSG_REJECT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT"} and target_id:
                self._blocked_scenes[(relation_scene, target_id)] = None
                while len(self._blocked_scenes) > int(self.config["cache_size"]):
                    self._blocked_scenes.popitem(last=False)
            elif event_type in {"FRIEND_ADD", "C2C_MSG_RECEIVE", "GROUP_ADD_ROBOT", "GROUP_MSG_RECEIVE"} and target_id:
                self._blocked_scenes.pop((relation_scene, target_id), None)
            return
        data = packet.get("d") or {}
        if (data.get("author") or {}).get("bot"):
            logger.info("[qqbot] message ignored: event=%s reason=bot_author", event_type)
            return
        context = event_to_context(packet, self_id=self.self_id, state=self.state)
        chat_type, chat_id = context["chat_type"], context["chat_id"]
        message_id = str(data.get("id") or "")
        if not chat_id or not context["user_id"] or not message_id:
            logger.warning("[qqbot] message ignored: event=%s reason=missing_ids chat=%s user=%s message=%s", event_type, bool(chat_id), bool(context["user_id"]), bool(message_id))
            return
        logger.info("[qqbot] message parsed: event=%s scene=%s:%s directed=%s reply_to_bot=%s mentions=%d self_mention_marked=%s text_chars=%d attachments=%d", event_type, chat_type, _scene_log_id(chat_id), context["explicit_bot_mention"], context["reply_to_bot"], len(data.get("mentions") or []), any(isinstance(item, dict) and item.get("is_you") is True for item in (data.get("mentions") or [])), len(context["text"]), len(data.get("attachments") or []))
        ext = scene_ext(data)
        identifier = ext.get("msg_idx") or message_id
        if self.state.is_duplicate(chat_type, chat_id, identifier):
            logger.info("[qqbot] message ignored: event=%s scene=%s:%s reason=duplicate", event_type, chat_type, _scene_log_id(chat_id))
            return
        self._chat_types[chat_id] = chat_type
        while len(self._chat_types) > int(self.config["cache_size"]):
            self._chat_types.pop(next(iter(self._chat_types)))
        self.state.remember_credential(chat_type, chat_id, message_id, ttl=float(self.config["passive_ttl_seconds"]))
        self.state.remember_reference(
            chat_type, chat_id, message_id, ext.get("msg_idx", ""),
            user_id=context["user_id"], user_name=context["user_name"],
        )
        online = False
        if chat_type == "group":
            if callable(self._group_presence_for):
                mode = self._group_presence_for(f"qqbot:{chat_id}")
                online = str(getattr(mode, "value", mode)).lower() == "online"
            if not online and not context["explicit_bot_mention"] and not context["reply_to_bot"]:
                # 全量事件可能先于 AT 事件：忽略的 SEMI 消息不能吞掉后到的定向版本。
                self.state.forget_duplicate(chat_type, chat_id, identifier)
                logger.info("[qqbot] group gate: scene=%s online=%s directed=%s reply_to_bot=%s action=ignore_non_directed", _scene_log_id(chat_id), online, context["explicit_bot_mention"], context["reply_to_bot"])
                return
            logger.info("[qqbot] group gate: scene=%s online=%s directed=%s reply_to_bot=%s action=accept", _scene_log_id(chat_id), online, context["explicit_bot_mention"], context["reply_to_bot"])
        if data.get("attachments"):
            parts = [context["text"]]
            for attachment in data["attachments"][:20]:
                if not isinstance(attachment, dict):
                    continue
                kind = attachment_type(attachment)
                asr = str(attachment.get("asr_refer_text") or "").strip()
                if asr:
                    parts.append(f"[语音转写参考]\n{asr}")
                if self.config.get("download_media") and self.media:
                    try:
                        path = await self.media.download(attachment, chat_type, chat_id)
                        marker = "file" if kind == "audio" else kind
                        parts.append(f"[{marker}: {path}]")
                    except Exception as exc:
                        logger.warning("[qqbot] media download failed (%s)", type(exc).__name__)
                        parts.append(f"[{kind}附件下载失败]")
                else:
                    parts.append(f"[{kind}附件]")
            context["text"] = "\n".join(part for part in parts if part)
        if not context["text"]:
            logger.info("[qqbot] message ignored: event=%s scene=%s:%s reason=empty_text", event_type, chat_type, _scene_log_id(chat_id))
            return
        self.current_chat_id = chat_id
        if chat_type == "group" and callable(self._group_message_handler):
            if await self._group_message_handler(context):
                logger.info("[qqbot] message routed: scene=group:%s target=group_listener", _scene_log_id(chat_id))
                return
        logger.info("[qqbot] message routed: scene=%s:%s target=%s", chat_type, _scene_log_id(chat_id), "aggregator" if self._aggregator else "core")
        if self._aggregator:
            await self._aggregator.add_message(chat_id, context["text"], context)
        else:
            await self.on_aggregator_complete(context)

    async def on_aggregator_complete(self, context: dict[str, Any]) -> None:
        # 聚合完成之后建立快照，避免只保存最后一条来源。
        context = dict(context)
        context["delivery_context"] = DeliveryContext.from_context(context)
        logger.info("[qqbot] core dispatch: scene=%s:%s sources=%d", context.get("chat_type"), _scene_log_id(str(context.get("chat_id") or "")), len(context["delivery_context"].source_message_ids))
        await self.dispatch_message(context)

    async def before_command(self, context: dict[str, Any], identity: Any) -> bool:
        from .commands import before_command
        return await before_command(self, context, identity)

    async def send_message(self, chat_id: str, text: str, **kwargs) -> list[str]:
        if not self.sender:
            raise RuntimeError("QQ adapter is not started")
        chat_type = str(kwargs.get("chat_type") or self._chat_types.get(str(chat_id)) or "")
        if (chat_type, str(chat_id)) in self._blocked_scenes:
            from adapters.delivery import DeliveryError
            raise DeliveryError("QQ target has disabled receiving messages")
        return await self.sender.send(
            str(chat_id), self.format_message(text), chat_type=chat_type,
            delivery_context=kwargs.get("delivery_context"),
            reply_to_message_id=str(kwargs.get("reply_to_message_id") or ""),
            keyboard=kwargs.get("keyboard"), markdown=bool(kwargs.get("markdown", True)),
            parse_media=bool(kwargs.get("parse_media", True)),
        )

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        if not self.sender:
            return False
        chat_type = str(kwargs.get("chat_type") or self._chat_types.get(str(chat_id)) or "")
        return await self.sender.delete(str(chat_id), str(message_id), chat_type=chat_type)

    async def start_typing(self, chat_id: str) -> None:
        return None

    async def stop_typing(self, chat_id: str) -> None:
        return None

    async def on_shutdown(self) -> None:
        tasks = list(self._interaction_tasks)
        if self._worker:
            tasks.append(self._worker)
        tasks.extend(self._event_tasks)
        if self._aggregator:
            tasks.extend(self._aggregator._timers.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._interaction_tasks.clear()
        self._event_tasks.clear()
        self._event_chains.clear()
        await self.client.close()
        if self.media:
            await self.media.close()
