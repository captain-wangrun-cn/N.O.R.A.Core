from __future__ import annotations

import re
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


def telegram_user_id(update: Update) -> str:
    """Return Telegram sender id, falling back to the chat id when sender is unavailable."""
    chat = update.effective_chat
    user = update.effective_user
    if user and user.id is not None:
        return str(user.id)
    return str(chat.id) if chat else ""


def telegram_user_name(update: Update) -> str:
    """Return a stable human-readable Telegram sender display name."""
    user = update.effective_user
    user_id = telegram_user_id(update)
    if not user:
        return user_id or "Unknown"

    name_parts = [part.strip() for part in (user.first_name or "", user.last_name or "") if part and part.strip()]
    if name_parts:
        return " ".join(name_parts)
    if user.username:
        return f"@{user.username}"
    return user_id or "Unknown"


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    """Remove trigger-only @bot mentions before handing text to the core model."""
    if not text or not bot_username:
        return text or ""
    username = bot_username.lstrip("@")
    if not username:
        return text
    stripped = re.sub(rf"(?i)(/[A-Za-z0-9_]+)@{re.escape(username)}\b", r"\1", text)
    stripped = re.sub(rf"(?i)(?<!\w)@{re.escape(username)}\b", "", stripped)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r" *\n *", "\n", stripped)
    return stripped.strip()


def has_bot_mention(text: str, bot_username: str | None) -> bool:
    """Return True when text contains an explicit @bot trigger mention."""
    if not text or not bot_username:
        return False
    username = bot_username.lstrip("@")
    if not username:
        return False
    pattern = rf"(?i)(?:/[A-Za-z0-9_]+@{re.escape(username)}\b|(?<!\w)@{re.escape(username)}\b)"
    return re.search(pattern, text) is not None


def context_from_update(
    update: Update,
    text: str,
    message: AdapterMessage | None = None,
    *,
    platform_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Best-effort Telegram Update -> controller context helper."""
    chat = update.effective_chat
    telegram_message: TelegramMessage | None = update.message
    chat_id = str(chat.id) if chat else ""
    event = build_telegram_event(
        chat_id=chat_id,
        user_id=telegram_user_id(update) or chat_id,
        text=text,
        chat_type=chat.type if chat else "private",
        user_name=telegram_user_name(update),
        platform_message_id=str(telegram_message.message_id) if telegram_message else None,
        raw=update,
        message=message,
    )
    context = event.to_context()
    if platform_message_ids:
        context["platform_message_ids"] = [str(mid) for mid in platform_message_ids if mid is not None]
    return context


def media_message(media_type: str, path: str, caption: str = "") -> AdapterMessage:
    """Create a standard media message that serializes to NORA's existing tags."""
    segment_type = "file" if media_type in {"doc", "document"} else media_type
    msg = AdapterMessage([MessageSegment(segment_type, {"path": path})])
    if caption:
        msg.append(MessageSegment.text(caption))
    return msg
