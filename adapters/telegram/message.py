from __future__ import annotations

import re
from typing import Any

from telegram import Message as TelegramMessage, Update

from adapters.base import AdapterEvent, AdapterMessage, MessageSegment
from adapters.message_controls import format_mention_marker, format_reply_marker


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


def telegram_display_name(user: Any) -> str:
    """Return a readable display name for a Telegram user-like object."""
    if not user:
        return ""
    name_parts = [
        part.strip()
        for part in (
            getattr(user, "first_name", "") or "",
            getattr(user, "last_name", "") or "",
        )
        if part and part.strip()
    ]
    if name_parts:
        return " ".join(name_parts)
    username = getattr(user, "username", "") or ""
    if username:
        return f"@{username}"
    user_id = getattr(user, "id", None)
    return str(user_id) if user_id is not None else ""


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


def _entity_text(message: Any, entity: Any) -> str:
    parse_entity = getattr(message, "parse_entity", None)
    if callable(parse_entity):
        try:
            return str(parse_entity(entity) or "")
        except Exception:
            pass
    source = str(getattr(message, "text", None) or getattr(message, "caption", None) or "")
    offset = int(getattr(entity, "offset", 0) or 0)
    length = int(getattr(entity, "length", 0) or 0)
    encoded = source.encode("utf-16-le")
    return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def telegram_text_mention_markers(message: Any, *, bot_user_id: str = "") -> tuple[list[str], str]:
    """Return reliable text_mention user ids and canonical marker prefix."""
    if not message:
        return [], ""
    entities = list(getattr(message, "entities", None) or getattr(message, "caption_entities", None) or [])
    user_ids: list[str] = []
    for entity in entities:
        entity_type = str(getattr(entity, "type", "") or "").lower()
        user = getattr(entity, "user", None)
        user_id = str(getattr(user, "id", "") or "").strip()
        if entity_type != "text_mention" or not user_id or user_id == str(bot_user_id or ""):
            continue
        if not _entity_text(message, entity):
            continue
        user_ids.append(user_id)
    user_ids = list(dict.fromkeys(user_ids))
    return user_ids, "".join(format_mention_marker(user_id) for user_id in user_ids)


def decorate_native_references(
    message: Any,
    text: str,
    *,
    bot_user_id: str = "",
) -> tuple[str, list[str]]:
    """Prefix Telegram reply/text_mention semantics using canonical markers."""
    parts: list[str] = []
    reply_message = getattr(message, "reply_to_message", None) if message else None
    reply_marker = format_reply_marker(str(getattr(reply_message, "message_id", "") or ""))
    if reply_marker:
        parts.append(reply_marker)
    user_ids, mention_markers = telegram_text_mention_markers(message, bot_user_id=bot_user_id)
    if mention_markers:
        parts.append(mention_markers)
    return "".join(parts) + str(text or ""), user_ids


def context_from_update(
    update: Update,
    text: str,
    message: AdapterMessage | None = None,
    *,
    platform_message_ids: list[str] | None = None,
    bot_user_id: str = "",
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
    if telegram_message and telegram_message.reply_to_message:
        reply_msg = telegram_message.reply_to_message
        context["reply_to_message_id"] = str(reply_msg.message_id)
        reply_user = getattr(reply_msg, "from_user", None)
        if reply_user and getattr(reply_user, "id", None) is not None:
            context["reply_to_user_id"] = str(reply_user.id)
            reply_user_name = telegram_display_name(reply_user)
            if reply_user_name:
                context["reply_to_user_name"] = reply_user_name
    mentioned_user_ids, _ = telegram_text_mention_markers(
        telegram_message,
        bot_user_id=bot_user_id,
    )
    if mentioned_user_ids:
        context["mentioned_user_ids"] = mentioned_user_ids
    return context


def media_message(media_type: str, path: str, caption: str = "") -> AdapterMessage:
    """Create a standard media message that serializes to NORA's existing tags."""
    segment_type = "file" if media_type in {"doc", "document"} else media_type
    msg = AdapterMessage([MessageSegment(segment_type, {"path": path})])
    if caption:
        msg.append(MessageSegment.text(caption))
    return msg
