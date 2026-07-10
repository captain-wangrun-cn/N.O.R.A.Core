from __future__ import annotations

import logging
import time
from typing import Any, Dict

from telegram import Update

from .message import context_from_update

logger = logging.getLogger(__name__)


class TelegramGroupContextMixin:
    """Telegram group-scene context enrichment.

    Bot API exposes total member count via get_chat_member_count. It does not expose
    a real-time online member count, so that field is injected as unavailable instead
    of guessed.
    """

    GROUP_CONTEXT_CACHE_TTL_SECONDS = 300

    def _is_group_chat_object(self, chat: Any) -> bool:
        return bool(chat and getattr(chat, "type", None) in ("group", "supergroup"))

    def _chat_display_name(self, chat: Any) -> str:
        title = str(getattr(chat, "title", "") or "").strip()
        if title:
            return title
        username = str(getattr(chat, "username", "") or "").strip()
        if username:
            return f"@{username}"
        chat_id = getattr(chat, "id", None)
        return str(chat_id) if chat_id is not None else ""

    def _group_context_cache(self) -> Dict[str, Dict[str, Any]]:
        cache = getattr(self, "_group_context_cache_data", None)
        if cache is None:
            cache = {}
            self._group_context_cache_data = cache
        return cache

    async def _get_group_member_count(self, chat_id: str) -> tuple[int | None, str]:
        cache = self._group_context_cache()
        now = time.time()
        cached = cache.get(str(chat_id))
        if cached and now - float(cached.get("ts", 0.0)) < self.GROUP_CONTEXT_CACHE_TTL_SECONDS:
            return cached.get("member_count"), str(cached.get("member_count_status") or "cached")

        bot = getattr(getattr(self, "application", None), "bot", None)
        if bot is None:
            cache[str(chat_id)] = {
                "ts": now,
                "member_count": None,
                "member_count_status": "unavailable:no_bot",
            }
            return None, "unavailable:no_bot"

        try:
            if hasattr(bot, "get_chat_member_count"):
                count = await bot.get_chat_member_count(chat_id)
            elif hasattr(bot, "get_chat_members_count"):
                count = await bot.get_chat_members_count(chat_id)
            else:
                cache[str(chat_id)] = {
                    "ts": now,
                    "member_count": None,
                    "member_count_status": "unavailable:no_method",
                }
                return None, "unavailable:no_method"
            member_count = int(count) if count is not None else None
            cache[str(chat_id)] = {
                "ts": now,
                "member_count": member_count,
                "member_count_status": "ok",
            }
            return member_count, "ok"
        except Exception as e:
            status = f"error:{type(e).__name__}"
            cache[str(chat_id)] = {
                "ts": now,
                "member_count": None,
                "member_count_status": status,
            }
            logger.debug(f"[telegram] 获取群成员总数失败 chat_id={chat_id}: {e}")
            return None, status

    async def _enrich_group_context(
        self,
        context: Dict[str, Any],
        chat: Any,
        *,
        update: Update | None = None,
    ) -> Dict[str, Any]:
        if not self._is_group_chat_object(chat):
            return context

        enriched = dict(context)
        chat_id = str(getattr(chat, "id", "") or enriched.get("chat_id") or "")
        chat_title = self._chat_display_name(chat)
        if chat_title:
            enriched["chat_title"] = chat_title
            enriched["group_title"] = chat_title
            enriched["group_display_name"] = chat_title

        username = str(getattr(chat, "username", "") or "").strip()
        if username:
            enriched["chat_username"] = f"@{username}"

        if update and update.message:
            sender_tag = getattr(update.message, "sender_tag", None)
            if sender_tag:
                enriched["sender_group_tag"] = str(sender_tag)
            author_signature = getattr(update.message, "author_signature", None)
            if author_signature:
                enriched["author_signature"] = str(author_signature)

        member_count, member_status = await self._get_group_member_count(chat_id)
        enriched["group_member_count"] = member_count
        enriched["group_member_count_status"] = member_status
        enriched["group_online_count"] = None
        enriched["group_online_count_status"] = "unavailable:telegram_bot_api"
        return enriched

    async def _context_from_update_with_group(
        self,
        update: Update,
        text: str,
        *,
        platform_message_ids: list[str] | None = None,
    ) -> Dict[str, Any]:
        context = context_from_update(
            update,
            text,
            platform_message_ids=platform_message_ids,
            bot_user_id=str(getattr(self, "bot_user_id", "") or ""),
        )
        return await self._enrich_group_context(context, update.effective_chat, update=update)
