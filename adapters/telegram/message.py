from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Callable

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


class TelegramUserLabelCache:
    """Bounded in-memory cache for Telegram mention display labels."""

    def __init__(
        self,
        *,
        max_size: int = 512,
        ttl: float = 3600.0,
        negative_ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_size = max(1, int(max_size))
        self.ttl = max(0.0, float(ttl))
        self.negative_ttl = max(0.0, float(negative_ttl))
        self._clock = clock
        self._entries: OrderedDict[str, tuple[str | None, float]] = OrderedDict()

    def get(self, user_id: str) -> tuple[bool, str | None]:
        key = str(user_id or "").strip()
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        label, expires_at = entry
        if expires_at <= self._clock():
            self._entries.pop(key, None)
            return False, None
        self._entries.move_to_end(key)
        return True, label

    def put(self, user_id: str, label: str) -> None:
        key = str(user_id or "").strip()
        value = str(label or "").strip()
        if not key.isdigit() or not value or value == key:
            return
        self._store(key, value, self.ttl)

    def put_missing(self, user_id: str) -> None:
        key = str(user_id or "").strip()
        if key.isdigit():
            self._store(key, None, self.negative_ttl)

    def remember_user(self, user: Any) -> None:
        user_id = str(getattr(user, "id", "") or "").strip()
        if not user_id.isdigit():
            return
        label = telegram_display_name(user)
        if label and label != user_id:
            self.put(user_id, label)

    def _store(self, user_id: str, label: str | None, ttl: float) -> None:
        self._entries[user_id] = (label, self._clock() + ttl)
        self._entries.move_to_end(user_id)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)


def telegram_reliable_users(update: Any) -> list[Any]:
    """Return reliable Telegram users found in an Update, deduplicated by id."""
    users: list[Any] = []
    seen: set[str] = set()

    def add(user: Any) -> None:
        user_id = str(getattr(user, "id", "") or "").strip()
        if not user_id.isdigit() or user_id in seen:
            return
        seen.add(user_id)
        users.append(user)

    add(getattr(update, "effective_user", None))
    message = getattr(update, "message", None)
    if not message:
        return users

    reply_message = getattr(message, "reply_to_message", None)
    add(getattr(reply_message, "from_user", None))
    entities = list(getattr(message, "entities", None) or [])
    entities.extend(list(getattr(message, "caption_entities", None) or []))
    for entity in entities:
        if str(getattr(entity, "type", "") or "").lower() == "text_mention":
            add(getattr(entity, "user", None))
    return users


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


def telegram_text_mention_markers(
    message: Any,
    *,
    bot_user_id: str = "",
) -> tuple[list[str], list[dict[str, str]], str]:
    """Return reliable text_mention ids, display names, and marker prefix."""
    if not message:
        return [], [], ""
    entities = list(getattr(message, "entities", None) or getattr(message, "caption_entities", None) or [])
    user_ids: list[str] = []
    mentioned_users: list[dict[str, str]] = []
    seen: set[str] = set()
    for entity in entities:
        entity_type = str(getattr(entity, "type", "") or "").lower()
        user = getattr(entity, "user", None)
        user_id = str(getattr(user, "id", "") or "").strip()
        if entity_type != "text_mention" or not user_id or user_id == str(bot_user_id or ""):
            continue
        if not _entity_text(message, entity) or user_id in seen:
            continue
        seen.add(user_id)
        user_ids.append(user_id)
        display_name = telegram_display_name(user)
        if display_name and display_name != user_id:
            mentioned_users.append({"user_id": user_id, "display_name": display_name})
    return user_ids, mentioned_users, "".join(format_mention_marker(user_id) for user_id in user_ids)


def decorate_native_references(
    message: Any,
    text: str,
    *,
    bot_user_id: str = "",
) -> tuple[str, list[str], list[dict[str, str]]]:
    """Prefix Telegram reply/text_mention semantics using canonical markers."""
    parts: list[str] = []
    reply_message = getattr(message, "reply_to_message", None) if message else None
    reply_marker = format_reply_marker(str(getattr(reply_message, "message_id", "") or ""))
    if reply_marker:
        parts.append(reply_marker)
    user_ids, mentioned_users, mention_markers = telegram_text_mention_markers(
        message,
        bot_user_id=bot_user_id,
    )
    if mention_markers:
        parts.append(mention_markers)
    return "".join(parts) + str(text or ""), user_ids, mentioned_users


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
    mentioned_user_ids, mentioned_users, _ = telegram_text_mention_markers(
        telegram_message,
        bot_user_id=bot_user_id,
    )
    if mentioned_user_ids:
        context["mentioned_user_ids"] = mentioned_user_ids
    if mentioned_users:
        context["mentioned_users"] = mentioned_users
    return context


def media_message(media_type: str, path: str, caption: str = "") -> AdapterMessage:
    """Create a standard media message that serializes to NORA's existing tags."""
    segment_type = "file" if media_type in {"doc", "document"} else media_type
    msg = AdapterMessage([MessageSegment(segment_type, {"path": path})])
    if caption:
        msg.append(MessageSegment.text(caption))
    return msg
