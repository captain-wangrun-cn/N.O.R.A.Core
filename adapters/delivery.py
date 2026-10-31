from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class DeliveryContext:
    """逻辑轮的投递快照；只含来源标识，不含平台凭据或密钥。"""

    platform: str
    chat_id: str
    chat_type: str
    source_message_ids: tuple[str, ...] = ()
    mode: str = "response"

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> "DeliveryContext":
        values = context.get("platform_message_ids") or []
        ids = list(values) if isinstance(values, (list, tuple)) else []
        ids.append(context.get("platform_message_id"))
        historical = str(context.get("reply_to_message_id") or "")
        return cls(
            platform=str(context.get("platform") or ""),
            chat_id=str(context.get("chat_id") or ""),
            chat_type=str(context.get("chat_type") or "private"),
            source_message_ids=tuple(dict.fromkeys(
                str(mid) for mid in ids if mid is not None and str(mid) and str(mid) != historical
            )),
        )

    def for_target(self, platform: str, chat_id: str, chat_type: str) -> "DeliveryContext":
        if (platform, chat_id, chat_type) == (self.platform, self.chat_id, self.chat_type):
            return self
        return self.proactive(platform, chat_id, chat_type)

    @classmethod
    def proactive(cls, platform: str, chat_id: str, chat_type: str = "private") -> "DeliveryContext":
        return cls(platform, chat_id, chat_type, (), "proactive")


class DeliveryReceipt(list[str]):
    """兼容 list[str] 的回执，同时携带已确认可见内容。"""

    def __init__(self, ids=(), visible_text: str = "") -> None:
        super().__init__(ids)
        self.visible_text = visible_text


def delivered_text(receipt: list[str], original: str) -> str:
    if not receipt:
        return ""
    return receipt.visible_text if isinstance(receipt, DeliveryReceipt) else original


def backend_archive_text(generated: str, visible_parts: list[str], *, internal: bool) -> str:
    """内部轮保留模型产物，可见轮只保留确认送达内容。"""
    return generated if internal else "\n".join(visible_parts)


class DeliveryCancelled(asyncio.CancelledError):
    """保持任务取消语义，同时携带取消前确认已发出的部分。"""

    def __init__(self, *, sent_ids=(), sent_texts=()) -> None:
        super().__init__("Delivery cancelled")
        self.sent_ids = list(sent_ids)
        self.sent_texts = list(sent_texts)

    @property
    def visible_text(self) -> str:
        return "\n".join(self.sent_texts)


class DeliveryError(RuntimeError):
    """发送失败，保留已确认可见的部分；uncertain 表示不能安全重发。"""

    def __init__(
        self,
        message: str,
        *,
        sent_ids: list[str] | None = None,
        sent_texts: list[str] | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.sent_ids = list(sent_ids or [])
        self.sent_texts = list(sent_texts or [])
        self.uncertain = uncertain

    @property
    def visible_text(self) -> str:
        return "\n".join(self.sent_texts)
