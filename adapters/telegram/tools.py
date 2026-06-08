from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

from telegram import ChatPermissions

from adapters.base import AdapterToolSpec

logger = logging.getLogger(__name__)


TELEGRAM_GROUP_TOOL_NAMES = {
    "telegram_get_chat_member_count",
    "telegram_get_chat_administrators",
    "telegram_get_chat_member",
    "telegram_mute_chat_member",
    "telegram_unmute_chat_member",
    "telegram_ban_chat_member",
    "telegram_unban_chat_member",
    "telegram_set_chat_administrator_custom_title",
    "telegram_set_chat_member_tag",
}


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:
            return str(value)
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _json_result(action: str, chat_id: str, target_user_id: str = "", **payload: Any) -> str:
    data = {
        "ok": bool(payload.pop("ok", True)),
        "action": action,
        "chat_id": str(chat_id or ""),
    }
    if target_user_id:
        data["target_user_id"] = str(target_user_id)
    data.update(payload)
    return json.dumps(data, ensure_ascii=False, default=_json_default)


def _json_error(action: str, chat_id: str, target_user_id: str = "", error: Exception | str = "") -> str:
    err_text = str(error) if error else "unknown error"
    return _json_result(
        action,
        chat_id,
        target_user_id,
        ok=False,
        error=err_text,
    )


class TelegramToolsMixin:
    """Backend tools backed by Telegram Bot API."""

    def get_adapter_tools(self) -> list[AdapterToolSpec]:
        return [
            AdapterToolSpec(
                name="telegram_get_chat_member_count",
                callable=self.telegram_get_chat_member_count,
                intro="查询当前或指定 Telegram 群的成员总数；Telegram Bot API 不支持枚举完整成员名单。",
                risk="low",
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_get_chat_administrators",
                callable=self.telegram_get_chat_administrators,
                intro="查询当前或指定 Telegram 群的管理员列表；可作为成员名单能力的可用替代。",
                risk="low",
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_get_chat_member",
                callable=self.telegram_get_chat_member,
                intro="按已知 user_id 查询 Telegram 群成员状态、权限和公开资料。",
                risk="low",
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_mute_chat_member",
                callable=self.telegram_mute_chat_member,
                intro="高风险：禁言 Telegram 群成员；执行前必须先获得主人明确口头确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_unmute_chat_member",
                callable=self.telegram_unmute_chat_member,
                intro="高风险：解除 Telegram 群成员禁言；执行前必须先获得主人明确口头确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_ban_chat_member",
                callable=self.telegram_ban_chat_member,
                intro="高风险：封禁或踢出 Telegram 群成员；执行前必须先获得主人明确口头确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_unban_chat_member",
                callable=self.telegram_unban_chat_member,
                intro="高风险：解除 Telegram 群成员封禁；执行前必须先获得主人明确口头确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_set_chat_administrator_custom_title",
                callable=self.telegram_set_chat_administrator_custom_title,
                intro="高风险：设置 Telegram 超级群管理员自定义头衔，不是修改用户昵称；执行前必须先获得主人明确口头确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="telegram",
            ),
            AdapterToolSpec(
                name="telegram_set_chat_member_tag",
                callable=self.telegram_set_chat_member_tag,
                intro="高风险：设置 Telegram 普通成员 tag，不是修改用户昵称；执行前必须先获得主人明确口头确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="telegram",
            ),
        ]

    def _telegram_tool_bot(self) -> Any:
        app = getattr(self, "application", None)
        bot = getattr(app, "bot", None)
        if bot is None:
            raise RuntimeError("Telegram bot is not initialized.")
        return bot

    def _telegram_tool_chat_id(self, chat_id: str = "") -> str:
        resolved = str(chat_id or getattr(self, "current_chat_id", "") or "").strip()
        if not resolved:
            raise ValueError("chat_id is required when no current Telegram chat is active.")
        return resolved

    def _telegram_tool_user_id(self, user_id: str) -> int:
        raw = str(user_id or "").strip()
        if not raw:
            raise ValueError("user_id is required.")
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"user_id must be a Telegram numeric id, got {raw!r}.") from exc

    async def _call_bot_tool(
        self,
        *,
        action: str,
        chat_id: str,
        target_user_id: str = "",
        call: Callable[[Any], Any],
    ) -> str:
        bot = self._telegram_tool_bot()
        try:
            result = await call(bot)
            return _json_result(action, chat_id, target_user_id, result=result)
        except Exception as e:
            logger.warning(
                "[telegram-tools] %s failed chat_id=%s user_id=%s: %s",
                action,
                chat_id,
                target_user_id,
                e,
            )
            return _json_error(action, chat_id, target_user_id, e)

    async def telegram_get_chat_member_count(self, chat_id: str = "") -> str:
        """
        Get Telegram chat member count. Telegram Bot API cannot list all regular members.

        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        """
        action = "telegram_get_chat_member_count"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            return await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                call=lambda bot: bot.get_chat_member_count(resolved_chat_id),
            )
        except Exception as e:
            return _json_error(action, str(chat_id or ""), error=e)

    async def telegram_get_chat_administrators(self, chat_id: str = "") -> str:
        """
        Get Telegram chat administrators. This is not a full member list.

        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        """
        action = "telegram_get_chat_administrators"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            return await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                call=lambda bot: bot.get_chat_administrators(resolved_chat_id),
            )
        except Exception as e:
            return _json_error(action, str(chat_id or ""), error=e)

    async def telegram_get_chat_member(self, user_id: str, chat_id: str = "") -> str:
        """
        Get one Telegram chat member by known user_id.

        :param user_id: Target Telegram numeric user id.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        """
        action = "telegram_get_chat_member"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            return await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=lambda bot: bot.get_chat_member(resolved_chat_id, target_id),
            )
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    async def telegram_mute_chat_member(
        self,
        user_id: str,
        until_timestamp: int = 0,
        chat_id: str = "",
        reason: str = "",
    ) -> str:
        """
        Mute a Telegram chat member by removing send permissions.

        :param user_id: Target Telegram numeric user id.
        :param until_timestamp: Unix timestamp when the mute expires. 0 means platform default/permanent.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "telegram_mute_chat_member"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            permissions = ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )
            until_date = int(until_timestamp or 0) or None
            result = await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=lambda bot: bot.restrict_chat_member(
                    resolved_chat_id,
                    target_id,
                    permissions=permissions,
                    until_date=until_date,
                ),
            )
            return self._with_reason(result, reason)
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    async def telegram_unmute_chat_member(
        self,
        user_id: str,
        chat_id: str = "",
        reason: str = "",
    ) -> str:
        """
        Unmute a Telegram chat member by restoring common send permissions.

        :param user_id: Target Telegram numeric user id.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "telegram_unmute_chat_member"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            permissions = ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
            result = await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=lambda bot: bot.restrict_chat_member(
                    resolved_chat_id,
                    target_id,
                    permissions=permissions,
                ),
            )
            return self._with_reason(result, reason)
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    async def telegram_ban_chat_member(
        self,
        user_id: str,
        until_timestamp: int = 0,
        revoke_messages: bool = False,
        chat_id: str = "",
        reason: str = "",
    ) -> str:
        """
        Ban or kick a Telegram chat member.

        :param user_id: Target Telegram numeric user id.
        :param until_timestamp: Unix timestamp when the ban expires. 0 means platform default/permanent.
        :param revoke_messages: Whether to delete messages from this member where supported.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "telegram_ban_chat_member"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            until_date = int(until_timestamp or 0) or None
            result = await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=lambda bot: bot.ban_chat_member(
                    resolved_chat_id,
                    target_id,
                    until_date=until_date,
                    revoke_messages=bool(revoke_messages),
                ),
            )
            return self._with_reason(result, reason)
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    async def telegram_unban_chat_member(
        self,
        user_id: str,
        only_if_banned: bool = True,
        chat_id: str = "",
        reason: str = "",
    ) -> str:
        """
        Unban a Telegram chat member.

        :param user_id: Target Telegram numeric user id.
        :param only_if_banned: If true, only unban users currently banned.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "telegram_unban_chat_member"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            result = await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=lambda bot: bot.unban_chat_member(
                    resolved_chat_id,
                    target_id,
                    only_if_banned=bool(only_if_banned),
                ),
            )
            return self._with_reason(result, reason)
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    async def telegram_set_chat_administrator_custom_title(
        self,
        user_id: str,
        custom_title: str,
        chat_id: str = "",
    ) -> str:
        """
        Set a Telegram supergroup administrator custom title, not a user nickname.

        :param user_id: Target Telegram numeric user id.
        :param custom_title: New administrator custom title, 0-16 characters, no emoji.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        """
        action = "telegram_set_chat_administrator_custom_title"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            title = str(custom_title or "")
            return await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=lambda bot: bot.set_chat_administrator_custom_title(
                    resolved_chat_id,
                    target_id,
                    title,
                ),
            )
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    async def telegram_set_chat_member_tag(
        self,
        user_id: str,
        tag: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Set a Telegram regular member tag, not a user nickname. Requires Bot API 9.5.

        :param user_id: Target Telegram numeric user id.
        :param tag: New member tag, 0-16 characters, no emoji. Empty clears the tag.
        :param chat_id: Telegram chat id. Leave empty to use the current chat.
        """
        action = "telegram_set_chat_member_tag"
        try:
            resolved_chat_id = self._telegram_tool_chat_id(chat_id)
            target_id = self._telegram_tool_user_id(user_id)
            tag_value = str(tag or "")

            async def call(bot: Any) -> Any:
                if hasattr(bot, "set_chat_member_tag"):
                    return await bot.set_chat_member_tag(resolved_chat_id, target_id, tag_value)
                return await bot.do_api_request(
                    "setChatMemberTag",
                    api_kwargs={
                        "chat_id": resolved_chat_id,
                        "user_id": target_id,
                        "tag": tag_value,
                    },
                )

            return await self._call_bot_tool(
                action=action,
                chat_id=resolved_chat_id,
                target_user_id=str(target_id),
                call=call,
            )
        except Exception as e:
            return _json_error(action, str(chat_id or ""), str(user_id or ""), e)

    def _with_reason(self, result_json: str, reason: str) -> str:
        reason_text = str(reason or "").strip()
        if not reason_text:
            return result_json
        try:
            data = json.loads(result_json)
            data["reason"] = reason_text
            return json.dumps(data, ensure_ascii=False, default=_json_default)
        except Exception:
            return result_json
