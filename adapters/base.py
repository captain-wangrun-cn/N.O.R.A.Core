from __future__ import annotations

import asyncio
import inspect
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional


MessageHandler = Callable[[Dict[str, Any]], Awaitable[Any] | Any]


def normalize_mentioned_users(value: Any) -> list[dict[str, str]]:
    """规范化可靠的被提及用户显示名映射。"""
    if not isinstance(value, (list, tuple)):
        return []

    seen: set[str] = set()
    mentioned_users: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        user_id = str(item.get("user_id") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        if not user_id or not display_name or display_name == user_id or user_id in seen:
            continue
        seen.add(user_id)
        mentioned_users.append({"user_id": user_id, "display_name": display_name})
    return mentioned_users


@dataclass(slots=True)
class AdapterToolSpec:
    """Adapter-provided tool exposed to the backend ToolManager."""

    name: str
    callable: Callable[..., Any]
    intro: str
    risk: str = "low"
    requires_owner_confirmation: bool = False
    platform: str = ""


@dataclass(slots=True)
class PlatformFeatures:
    """平台能力描述。"""

    supports_edit_message: bool = False
    supports_delete_message: bool = False
    supports_reactions: bool = False
    supports_typing_indicator: bool = True
    supports_rich_media: bool = False
    supports_reply: bool = True
    supports_threads: bool = False
    supports_buttons: bool = False
    supports_chat_member_lookup: bool = False
    supports_chat_moderation: bool = False
    supports_member_tags: bool = False
    supports_message_controls: bool = False
    max_message_length: int | None = None


@dataclass(slots=True)
class AdapterPlatformInfo:
    """适配器对接的真实平台信息。"""

    name: str
    type: str = "chat"
    protocol: str = ""
    official_website: str = ""
    description: str = ""
    notes: list[str] = field(default_factory=list)
    privacy_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AdapterPlatformInfo":
        data = data or {}
        return cls(
            name=str(data.get("name") or "unknown"),
            type=str(data.get("type") or "chat"),
            protocol=str(data.get("protocol") or ""),
            official_website=str(data.get("official_website") or data.get("website") or ""),
            description=str(data.get("description") or ""),
            notes=[str(item) for item in data.get("notes", []) or []],
            privacy_notes=[str(item) for item in data.get("privacy_notes", []) or []],
        )


@dataclass(slots=True)
class AdapterMetadata:
    """适配器元数据清单。"""

    adapter_id: str
    name: str
    version: str
    author: str
    entrypoint: str
    description: str = ""
    platform: AdapterPlatformInfo = field(default_factory=lambda: AdapterPlatformInfo(name="unknown"))
    features: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AdapterMetadata":
        data = dict(data or {})
        return cls(
            adapter_id=str(data.get("adapter_id") or data.get("id") or "unknown"),
            name=str(data.get("name") or data.get("adapter_id") or "unknown"),
            version=str(data.get("version") or "0.0.0"),
            author=str(data.get("author") or "unknown"),
            entrypoint=str(data.get("entrypoint") or ""),
            description=str(data.get("description") or ""),
            platform=AdapterPlatformInfo.from_dict(data.get("platform")),
            features=dict(data.get("features") or {}),
            limits=dict(data.get("limits") or {}),
            tags=[str(item) for item in data.get("tags", []) or []],
            raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MessageSegment:
    """通用轻量消息段，参考 NoneBot MessageSegment 但保持 NORA 文本标记兼容。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        return cls("text", {"text": text})

    @classmethod
    def image(cls, path: str) -> "MessageSegment":
        return cls("image", {"path": path})

    @classmethod
    def video(cls, path: str) -> "MessageSegment":
        return cls("video", {"path": path})

    @classmethod
    def audio(cls, path: str) -> "MessageSegment":
        return cls("audio", {"path": path})

    @classmethod
    def file(cls, path: str) -> "MessageSegment":
        return cls("file", {"path": path})

    @classmethod
    def sticker(cls, emoji: str = "", set_name: str = "", path: str = "") -> "MessageSegment":
        return cls("sticker", {"emoji": emoji, "set_name": set_name, "path": path})

    @classmethod
    def reply(cls, text: str) -> "MessageSegment":
        return cls("reply", {"text": text})

    @classmethod
    def reply_reference(cls, message_id: str) -> "MessageSegment":
        return cls("reply_reference", {"message_id": str(message_id)})

    @classmethod
    def mention(cls, user_id: str) -> "MessageSegment":
        return cls("mention", {"user_id": str(user_id)})

    def is_text(self) -> bool:
        return self.type == "text"

    def to_text_marker(self) -> str:
        if self.type == "text":
            return str(self.data.get("text") or "")
        if self.type == "reply":
            return f"[回复: {self.data.get('text') or ''}]"
        if self.type == "reply_reference":
            return f"[reply:{self.data.get('message_id') or ''}]"
        if self.type == "mention":
            return f"[at:{self.data.get('user_id') or ''}]"
        if self.type == "sticker":
            emoji = str(self.data.get("emoji") or "")
            set_name = str(self.data.get("set_name") or "")
            path = str(self.data.get("path") or "")
            label = f"[sticker: {emoji} from {set_name}]".strip()
            return f"{label}\n[sticker: {path}]" if path else label
        if self.type in {"image", "video", "audio", "file", "doc"}:
            return f"[{self.type}: {self.data.get('path') or self.data.get('url') or ''}]"
        return f"[{self.type}: {self.data}]"

    def __str__(self) -> str:
        return self.to_text_marker()


class AdapterMessage(list[MessageSegment]):
    """通用消息序列，可转换回 NORA 现有文本协议。"""

    def __init__(self, message: str | MessageSegment | Iterable[MessageSegment] | None = None):
        super().__init__()
        if message is None:
            return
        if isinstance(message, str):
            self.append(MessageSegment.text(message))
        elif isinstance(message, MessageSegment):
            self.append(message)
        else:
            self.extend(message)

    def append(self, item: str | MessageSegment) -> None:  # type: ignore[override]
        if isinstance(item, str):
            item = MessageSegment.text(item)
        super().append(item)

    def extend(self, items: Iterable[str | MessageSegment]) -> None:  # type: ignore[override]
        for item in items:
            self.append(item)

    def extract_plain_text(self) -> str:
        return "".join(str(segment.data.get("text") or "") for segment in self if segment.is_text())

    def to_text(self) -> str:
        parts = [segment.to_text_marker() for segment in self]
        return "\n".join(part for part in parts if part).strip()

    def __str__(self) -> str:
        return self.to_text()


@dataclass(slots=True)
class AdapterEvent:
    """标准输入事件，向 controller 输出时保持 dict context 兼容。"""

    platform: str
    chat_id: str
    user_id: str
    text: str = ""
    chat_type: str = "private"
    user_name: str = "Unknown"
    platform_message_id: str | None = None
    reply_to_message_id: str | None = None
    reply_to_user_id: str | None = None
    reply_to_user_name: str | None = None
    mentioned_user_ids: list[str] = field(default_factory=list)
    mentioned_users: list[dict[str, str]] = field(default_factory=list)
    message: AdapterMessage | None = None
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_context(cls, context: Mapping[str, Any], default_platform: str = "unknown") -> "AdapterEvent":
        message = context.get("message")
        adapter_message = message if isinstance(message, AdapterMessage) else None
        text = str(context.get("text") or (adapter_message.to_text() if adapter_message else ""))
        chat_id = str(context.get("chat_id") or "")
        return cls(
            platform=str(context.get("platform") or default_platform),
            chat_id=chat_id,
            user_id=str(context.get("user_id") or chat_id),
            text=text,
            chat_type=str(context.get("chat_type") or "private"),
            user_name=str(context.get("user_name") or "Unknown"),
            platform_message_id=(
                str(context.get("platform_message_id"))
                if context.get("platform_message_id") is not None
                else None
            ),
            reply_to_message_id=(
                str(context.get("reply_to_message_id"))
                if context.get("reply_to_message_id") is not None
                else None
            ),
            reply_to_user_id=(
                str(context.get("reply_to_user_id"))
                if context.get("reply_to_user_id") is not None
                else None
            ),
            reply_to_user_name=(
                str(context.get("reply_to_user_name"))
                if context.get("reply_to_user_name") is not None
                else None
            ),
            mentioned_user_ids=[
                str(user_id)
                for user_id in (context.get("mentioned_user_ids") or [])
                if user_id is not None and str(user_id).strip()
            ],
            mentioned_users=normalize_mentioned_users(context.get("mentioned_users")),
            message=adapter_message,
            raw=context.get("raw"),
            metadata=dict(context.get("metadata") or {}),
        )

    def get_plaintext(self) -> str:
        return self.message.extract_plain_text() if self.message else self.text

    def to_context(self) -> dict[str, Any]:
        context = {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "text": self.text or (self.message.to_text() if self.message else ""),
            "chat_type": self.chat_type,
            "user_name": self.user_name,
        }
        if self.platform_message_id is not None:
            context["platform_message_id"] = self.platform_message_id
        if self.reply_to_message_id is not None:
            context["reply_to_message_id"] = self.reply_to_message_id
        if self.reply_to_user_id is not None:
            context["reply_to_user_id"] = self.reply_to_user_id
        if self.reply_to_user_name is not None:
            context["reply_to_user_name"] = self.reply_to_user_name
        if self.mentioned_user_ids:
            context["mentioned_user_ids"] = list(self.mentioned_user_ids)
        mentioned_users = normalize_mentioned_users(self.mentioned_users)
        if mentioned_users:
            context["mentioned_users"] = mentioned_users
        if self.message is not None:
            context["message"] = self.message
        if self.raw is not None:
            context["raw"] = self.raw
        if self.metadata:
            context["metadata"] = self.metadata
        return context


class BaseAdapter(ABC):
    """平台适配器基础抽象层。"""

    def __init__(self):
        self.current_chat_id: str | None = None
        self._message_handler: MessageHandler | None = None
        self._is_running: bool = False
        self._metadata_cache: AdapterMetadata | None = None

    @property
    def platform_name(self) -> str:
        """平台名称，子类可覆盖。"""
        return self.__class__.__name__.replace("Adapter", "").lower() or "unknown"

    @property
    def platform_features(self) -> PlatformFeatures:
        """平台能力，子类可覆盖。"""
        return PlatformFeatures()

    @property
    def metadata(self) -> AdapterMetadata:
        return self.load_metadata()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def load_metadata(self) -> AdapterMetadata:
        """读取 adapters/<platform>/metadata.json；缺失时返回最小元数据。"""
        metadata_cache = getattr(self, "_metadata_cache", None)
        if metadata_cache is not None:
            return metadata_cache

        metadata_path = Path(__file__).resolve().parent / self.platform_name / "metadata.json"
        data: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if not data:
            data = {
                "adapter_id": self.platform_name,
                "name": self.platform_name,
                "version": "0.0.0",
                "author": "unknown",
                "entrypoint": f"adapters.{self.platform_name}:Adapter",
                "platform": {"name": self.platform_name},
            }
        self._metadata_cache = AdapterMetadata.from_dict(data)
        return self._metadata_cache

    def normalize_event(self, event_or_context: AdapterEvent | Mapping[str, Any]) -> AdapterEvent:
        """把平台事件或旧 dict context 标准化为 AdapterEvent。"""
        if isinstance(event_or_context, AdapterEvent):
            return event_or_context
        return AdapterEvent.from_context(event_or_context, default_platform=self.platform_name)

    def format_message(self, message: AdapterMessage | MessageSegment | str) -> str:
        """将通用消息结构转换为 NORA 当前文本协议。"""
        if isinstance(message, AdapterMessage):
            return message.to_text()
        if isinstance(message, MessageSegment):
            return message.to_text_marker()
        return str(message)

    def get_adapter_tools(self) -> list[AdapterToolSpec]:
        """Return tools this adapter wants to expose to the backend tool loop."""
        return []

    @abstractmethod
    def run(self, message_handler: MessageHandler):
        """启动适配器并开始监听消息（阻塞式，单适配器兼容入口）。"""

    async def run_async(self, message_handler: MessageHandler) -> None:
        """非阻塞并发入口：在调用方已有的事件循环内启动并常驻。

        多适配器同时在线时，主程序用单个事件循环 gather 各适配器的
        ``run_async()``。子类应覆盖此方法，用平台原生 async API 启动监听且
        不自行 ``asyncio.run(...)``。默认实现把同步 ``run()`` 丢进线程池兜底，
        以兼容尚未适配的子类。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.run, message_handler)

    async def startup(self) -> None:
        """生命周期：适配器启动时触发。"""
        self._is_running = True
        await self.on_startup()

    async def on_startup(self) -> None:
        """钩子：子类可覆盖。"""
        return None

    async def on_ready(self) -> None:
        """钩子：适配器完成初始化并可收发消息时触发。"""
        return None

    async def before_message(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """钩子：消息分发给控制器前触发，可改写 context。"""
        return context

    async def after_message(self, context: Dict[str, Any], result: Any = None) -> None:
        """钩子：消息处理后触发。"""
        return None

    async def dispatch_message(self, event_or_context: AdapterEvent | Mapping[str, Any]) -> Any:
        """标准消息分发：normalize -> before hook -> handler -> after hook。"""
        if not self._message_handler:
            return None
        context = self.normalize_event(event_or_context).to_context()
        processed_context = await self.before_message(context)
        maybe_result = self._message_handler(processed_context)
        result = await maybe_result if inspect.isawaitable(maybe_result) else maybe_result
        await self.after_message(processed_context, result)
        return result

    async def shutdown(self) -> None:
        """生命周期：适配器关闭时触发。"""
        self._is_running = False
        await self.on_shutdown()

    async def on_shutdown(self) -> None:
        """钩子：子类可覆盖。"""
        return None

    async def health_check(self) -> Dict[str, Any]:
        """返回基础健康状态。"""
        return {
            "ok": self._is_running,
            "platform": self.platform_name,
            "running": self._is_running,
            "features": asdict(self.platform_features),
            "metadata": self.metadata.to_dict(),
        }

    @abstractmethod
    async def send_message(self, chat_id: str, text: str, **kwargs) -> List[str]:
        """发送一条或多条平台消息，返回平台消息 ID 列表。"""

    @abstractmethod
    async def start_typing(self, chat_id: str):
        """显示“正在输入”状态。"""

    @abstractmethod
    async def stop_typing(self, chat_id: str):
        """停止“正在输入”状态（若平台支持）。"""

    async def edit_message(self, chat_id: str, message_id: str, new_text: str, **kwargs) -> bool:
        """编辑消息；默认不支持。"""
        return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """删除消息；默认不支持。"""
        return False
