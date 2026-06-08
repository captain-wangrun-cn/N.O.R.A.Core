from __future__ import annotations

from typing import Any

from telegram import Message as TelegramMessage, Update

from adapters.base import AdapterEvent, AdapterMessage, MessageSegment


def build_telegram_event(
    *,
    chat_id: str,
    user_id: str,
    text: str = "",
    chat_type: str = "private",
    user_name: str = "Unknown",
    platform_message_id: str | None = None,
    raw: Any = None,
    message: AdapterMessage | None = None,
) -> AdapterEvent:
    """Build NORA's standard AdapterEvent for Telegram inputs."""
    return AdapterEvent(
        platform="telegram",
        chat_id=str(chat_id),
        user_id=str(user_id or chat_id),
        text=text or (message.to_text() if message else ""),
        chat_type=str(chat_type or "private"),
        user_name=str(user_name or "Unknown"),
        platform_message_id=str(platform_message_id) if platform_message_id is not None else None,
        raw=raw,
        message=message,
    )


def event_context(event: AdapterEvent) -> dict[str, Any]:
    """Return the controller-compatible dict context for an AdapterEvent."""
    return event.to_context()


def context_from_update(update: Update, text: str, message: AdapterMessage | None = None) -> dict[str, Any]:
    """Best-effort Telegram Update -> controller context helper."""
    chat = update.effective_chat
    user = update.effective_user
    telegram_message: TelegramMessage | None = update.message
    chat_id = str(chat.id) if chat else ""
    event = build_telegram_event(
        chat_id=chat_id,
        user_id=str(user.id) if user else chat_id,
        text=text,
        chat_type=chat.type if chat else "private",
        user_name=user.first_name if user else "Unknown",
        platform_message_id=str(telegram_message.message_id) if telegram_message else None,
        raw=update,
        message=message,
    )
    return event.to_context()


def media_message(media_type: str, path: str, caption: str = "") -> AdapterMessage:
    """Create a standard media message that serializes to NORA's existing tags."""
    segment_type = "file" if media_type in {"doc", "document"} else media_type
    msg = AdapterMessage([MessageSegment(segment_type, {"path": path})])
    if caption:
        msg.append(MessageSegment.text(caption))
    return msg
