from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from adapters.base import AdapterToolSpec

from .forward import (
    FULL_MAX_CHARS,
    FULL_MAX_DEPTH,
    FULL_MAX_NODES,
    nested_forward_from_segments,
)
from .message import onebot_message_segments

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:
            return str(value)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def _json_result(action: str, **payload: Any) -> str:
    data = {
        "ok": bool(payload.pop("ok", True)),
        "action": action,
    }
    data.update(payload)
    return json.dumps(data, ensure_ascii=False, default=_json_default)


def _json_error(action: str, error: Exception | str = "", **payload: Any) -> str:
    payload["error"] = str(error) if error else "unknown error"
    return _json_result(action, ok=False, **payload)


class OneBotV11ToolsMixin:
    """Backend tools backed by OneBot v11 APIs."""

    def get_adapter_tools(self) -> list[AdapterToolSpec]:
        tools = [
            AdapterToolSpec(
                name="onebotv11_get_login_info",
                callable=self.onebotv11_get_login_info,
                intro="查询当前 OneBot v11 登录号信息。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_status",
                callable=self.onebotv11_get_status,
                intro="查询 OneBot v11 实现运行状态。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_version_info",
                callable=self.onebotv11_get_version_info,
                intro="查询 OneBot v11 实现版本信息。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_group_info",
                callable=self.onebotv11_get_group_info,
                intro="查询当前或指定 QQ 群信息。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_group_list",
                callable=self.onebotv11_get_group_list,
                intro="查询当前登录号加入的 QQ 群列表。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_group_member_info",
                callable=self.onebotv11_get_group_member_info,
                intro="按已知 QQ 号查询当前或指定 QQ 群成员资料。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_group_member_list",
                callable=self.onebotv11_get_group_member_list,
                intro="查询当前或指定 QQ 群成员列表；不同 OneBot 实现可能返回缓存或部分字段。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_group_honor_info",
                callable=self.onebotv11_get_group_honor_info,
                intro="查询当前或指定 QQ 群荣誉信息。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_friend_list",
                callable=self.onebotv11_get_friend_list,
                intro="查询当前登录号好友列表。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_msg",
                callable=self.onebotv11_get_msg,
                intro="按 OneBot message_id 查询消息详情。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_forward_msg",
                callable=self.onebotv11_get_forward_msg,
                intro="按合并转发 id 获取合并转发消息的原始 JSON。日常读聊天记录请优先用 onebotv11_get_chat_history。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_get_chat_history",
                callable=self.onebotv11_get_chat_history,
                intro="按聊天记录 id 或消息 id 获取完整聊天记录，返回逐条结构化文本（含嵌套记录，图片只标 [图片]）。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_send_private_msg",
                callable=self.onebotv11_send_private_msg,
                intro="向指定 QQ 用户发送私聊文本消息；这是对外发送操作，执行前必须确认目标和内容。",
                risk="medium",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_send_group_msg",
                callable=self.onebotv11_send_group_msg,
                intro="向当前或指定 QQ 群发送文本消息；这是对外发送操作，执行前必须确认目标和内容。",
                risk="medium",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_send_like",
                callable=self.onebotv11_send_like,
                intro="给指定 QQ 好友发送点赞；可能被平台频率限制。",
                risk="medium",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_delete_msg",
                callable=self.onebotv11_delete_msg,
                intro="高风险：撤回 OneBot 消息；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_ban",
                callable=self.onebotv11_set_group_ban,
                intro="高风险：禁言或解除禁言 QQ 群成员；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_kick",
                callable=self.onebotv11_set_group_kick,
                intro="高风险：踢出 QQ 群成员；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_whole_ban",
                callable=self.onebotv11_set_group_whole_ban,
                intro="高风险：开启或关闭 QQ 群全员禁言；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_admin",
                callable=self.onebotv11_set_group_admin,
                intro="高风险：设置或取消 QQ 群管理员；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_card",
                callable=self.onebotv11_set_group_card,
                intro="高风险：设置 QQ 群名片/群备注，不是改 QQ 昵称；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_name",
                callable=self.onebotv11_set_group_name,
                intro="高风险：修改 QQ 群名称；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_leave",
                callable=self.onebotv11_set_group_leave,
                intro="高风险：退出或解散 QQ 群；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_set_group_special_title",
                callable=self.onebotv11_set_group_special_title,
                intro="高风险：设置 QQ 群专属头衔；执行前必须先获得主人明确确认。",
                risk="high",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
        ]
        if bool(getattr(self, "enable_napcat_api", False)):
            tools.extend(self._napcat_tool_specs())
        return tools

    def _napcat_tool_specs(self) -> list[AdapterToolSpec]:
        return [
            AdapterToolSpec(
                name="onebotv11_napcat_send_poke",
                callable=self.onebotv11_napcat_send_poke,
                intro="NapCat 扩展：向好友或群成员发送戳一戳；执行前应确认目标。",
                risk="medium",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_mark_private_msg_as_read",
                callable=self.onebotv11_napcat_mark_private_msg_as_read,
                intro="NapCat 扩展：标记指定私聊为已读。",
                risk="medium",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_mark_group_msg_as_read",
                callable=self.onebotv11_napcat_mark_group_msg_as_read,
                intro="NapCat 扩展：标记当前或指定群聊为已读。",
                risk="medium",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_get_friend_msg_history",
                callable=self.onebotv11_napcat_get_friend_msg_history,
                intro="NapCat 扩展：获取指定好友私聊历史记录。",
                risk="medium",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_fetch_custom_face",
                callable=self.onebotv11_napcat_fetch_custom_face,
                intro="NapCat 扩展：获取自定义表情列表。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_get_ai_characters",
                callable=self.onebotv11_napcat_get_ai_characters,
                intro="NapCat 扩展：获取当前或指定群可用的 AI 语音角色列表。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_get_ai_record",
                callable=self.onebotv11_napcat_get_ai_record,
                intro="NapCat 扩展：将文本转换为指定 AI 角色语音链接。",
                risk="low",
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_send_group_ai_record",
                callable=self.onebotv11_napcat_send_group_ai_record,
                intro="NapCat 扩展：向当前或指定群发送 AI 语音；执行前应确认群与内容。",
                risk="medium",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
            AdapterToolSpec(
                name="onebotv11_napcat_send_forward_msg",
                callable=self.onebotv11_napcat_send_forward_msg,
                intro="NapCat 扩展：发送合并转发消息，messages_json 必须是 OneBot node 数组 JSON。",
                risk="medium",
                requires_owner_confirmation=True,
                platform="onebotv11",
            ),
        ]

    async def _call_onebot_tool(self, action: str, params: dict[str, Any] | None = None, **context: Any) -> str:
        try:
            response = await self.call_api(action, params or {})
            return _json_result(
                action,
                status=response.get("status"),
                retcode=response.get("retcode"),
                result=response.get("data"),
                **context,
            )
        except Exception as exc:
            logger.warning("[onebotv11-tools] %s failed params=%s: %s", action, params, exc)
            return _json_error(action, exc, **context)

    def _onebot_int(self, value: Any, name: str) -> int:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError(f"{name} is required.")
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be numeric, got {raw!r}.") from exc

    def _onebot_group_id(self, group_id: str = "", chat_id: str = "") -> int:
        raw = str(group_id or chat_id or "").strip()
        if raw:
            return self._onebot_int(raw, "group_id")
        current = str(getattr(self, "current_chat_id", "") or "").strip()
        chat_types = getattr(self, "_chat_types", {}) or {}
        if current and chat_types.get(current) == "group":
            return self._onebot_int(current, "group_id")
        raise ValueError("group_id is required when the current OneBot chat is not a group.")

    def _onebot_user_id(self, user_id: str) -> int:
        return self._onebot_int(user_id, "user_id")

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

    async def onebotv11_get_login_info(self) -> str:
        """Get current OneBot login account info."""
        return await self._call_onebot_tool("get_login_info")

    async def onebotv11_get_status(self) -> str:
        """Get OneBot implementation status."""
        return await self._call_onebot_tool("get_status")

    async def onebotv11_get_version_info(self) -> str:
        """Get OneBot implementation version info."""
        return await self._call_onebot_tool("get_version_info")

    async def onebotv11_get_friend_list(self, no_cache: bool = False) -> str:
        """
        Get current account friend list.

        :param no_cache: Whether to avoid cached data where supported by the implementation.
        """
        return await self._call_onebot_tool("get_friend_list", {"no_cache": bool(no_cache)})

    async def onebotv11_get_group_info(
        self,
        group_id: str = "",
        no_cache: bool = False,
        chat_id: str = "",
    ) -> str:
        """
        Get QQ group info.

        :param group_id: QQ group id. Leave empty to use current group chat.
        :param no_cache: Whether to avoid cached data.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "get_group_info"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(action, {"group_id": gid, "no_cache": bool(no_cache)}, group_id=str(gid))
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_get_group_list(self) -> str:
        """Get QQ group list of current account."""
        return await self._call_onebot_tool("get_group_list")

    async def onebotv11_get_group_member_info(
        self,
        user_id: str,
        group_id: str = "",
        no_cache: bool = False,
        chat_id: str = "",
    ) -> str:
        """
        Get QQ group member info by known user_id.

        :param user_id: Target QQ user id.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param no_cache: Whether to avoid cached data.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "get_group_member_info"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            uid = self._onebot_user_id(user_id)
            return await self._call_onebot_tool(
                action,
                {"group_id": gid, "user_id": uid, "no_cache": bool(no_cache)},
                group_id=str(gid),
                target_user_id=str(uid),
            )
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))

    async def onebotv11_get_group_member_list(self, group_id: str = "", chat_id: str = "") -> str:
        """
        Get QQ group member list.

        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "get_group_member_list"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(action, {"group_id": gid}, group_id=str(gid))
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_get_group_honor_info(
        self,
        group_id: str = "",
        honor_type: str = "all",
        chat_id: str = "",
    ) -> str:
        """
        Get QQ group honor info.

        :param group_id: QQ group id. Leave empty to use current group chat.
        :param honor_type: Honor type: talkative, performer, legend, strong_newbie, emotion, or all.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "get_group_honor_info"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(
                action,
                {"group_id": gid, "type": str(honor_type or "all")},
                group_id=str(gid),
            )
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_get_msg(self, message_id: str) -> str:
        """
        Get OneBot message by message_id.

        :param message_id: OneBot message id.
        """
        action = "get_msg"
        try:
            mid = self._onebot_int(message_id, "message_id")
            return await self._call_onebot_tool(action, {"message_id": mid}, message_id=str(mid))
        except Exception as exc:
            return _json_error(action, exc, message_id=str(message_id or ""))

    async def onebotv11_get_forward_msg(self, forward_id: str) -> str:
        """
        Get merged forward message content.

        :param forward_id: Merged forward id from a forward message segment.
        """
        return await self._call_onebot_tool("get_forward_msg", {"id": str(forward_id or "")}, forward_id=str(forward_id or ""))

    async def onebotv11_get_chat_history(self, record_id: str) -> str:
        """
        Get a full QQ merged-forward chat record as readable structured text.

        Use this when a message contains a chat record ([聊天记录]) and you need the
        complete content, e.g. the inbound one was truncated. Nested chat records are
        expanded too; images and other media inside are shown as placeholders only.

        :param record_id: The chat record id shown as `id=...`, or the OneBot message_id of a message that contains the chat record.
        """
        action = "get_chat_history"
        raw_id = str(record_id or "").strip()
        if not raw_id:
            return _json_error(action, "record_id is required.")

        try:
            forward_id, inline_nodes = await self._resolve_chat_record_source(raw_id)
        except Exception as exc:
            logger.warning("[onebotv11-tools] resolve chat record failed id=%s: %s", raw_id, exc)
            return _json_error(action, exc, record_id=raw_id)

        if not forward_id and not inline_nodes:
            return _json_error(
                action,
                "No chat record found for this id. It may be expired, or the id belongs to a normal message.",
                record_id=raw_id,
            )

        try:
            content = await self.render_forward_record(
                forward_id=forward_id,
                inline_nodes=inline_nodes,
                max_depth=FULL_MAX_DEPTH,
                max_nodes=FULL_MAX_NODES,
                max_chars=FULL_MAX_CHARS,
            )
        except Exception as exc:
            logger.warning("[onebotv11-tools] render chat record failed id=%s: %s", raw_id, exc)
            return _json_error(action, exc, record_id=raw_id)

        return _json_result(action, record_id=raw_id, forward_id=forward_id or raw_id, result=content)

    async def _resolve_chat_record_source(self, raw_id: str) -> tuple[str, list[Any]]:
        """把用户给的 id 解析成 (forward_id, inline_nodes)。

        先当作 forward id 试；拿不到再当作 message_id 走 get_msg 找里面的 forward 段。
        """
        nodes = await self._fetch_forward_nodes(raw_id)
        if nodes:
            return raw_id, nodes

        try:
            response = await self.call_api("get_msg", {"message_id": int(raw_id)})
        except (TypeError, ValueError):
            return "", []
        except Exception as exc:
            logger.debug("[onebotv11-tools] get_msg failed for chat record id=%s: %s", raw_id, exc)
            return "", []

        data = response.get("data") or {}
        segments = onebot_message_segments(data.get("message"), raw_message=str(data.get("raw_message") or ""))
        nested = nested_forward_from_segments(segments)
        if not nested:
            return "", []
        return nested["forward_id"], nested["inline_nodes"]

    async def onebotv11_send_private_msg(self, user_id: str, message: str, auto_escape: bool = False) -> str:
        """
        Send a text message to a QQ private chat.

        :param user_id: Target QQ user id.
        :param message: Text content to send. Do not pass raw OneBot message arrays or guessed CQ codes.
        :param auto_escape: Whether to treat the string as plain text and escape CQ codes.
        """
        action = "send_private_msg"
        try:
            uid = self._onebot_user_id(user_id)
            text = str(message or "")
            if not text.strip():
                raise ValueError("message is required.")
            return await self._call_onebot_tool(
                action,
                {"user_id": uid, "message": text, "auto_escape": bool(auto_escape)},
                target_user_id=str(uid),
            )
        except Exception as exc:
            return _json_error(action, exc, target_user_id=str(user_id or ""))

    async def onebotv11_send_group_msg(
        self,
        message: str,
        group_id: str = "",
        auto_escape: bool = False,
        chat_id: str = "",
    ) -> str:
        """
        Send a text message to a QQ group.

        :param message: Text content to send. Do not pass raw OneBot message arrays or guessed CQ codes.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param auto_escape: Whether to treat the string as plain text and escape CQ codes.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "send_group_msg"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            text = str(message or "")
            if not text.strip():
                raise ValueError("message is required.")
            return await self._call_onebot_tool(
                action,
                {"group_id": gid, "message": text, "auto_escape": bool(auto_escape)},
                group_id=str(gid),
            )
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_send_like(self, user_id: str, times: int = 1) -> str:
        """
        Send QQ profile likes to a friend.

        :param user_id: Target QQ user id.
        :param times: Like count, normally 1-10.
        """
        action = "send_like"
        try:
            uid = self._onebot_user_id(user_id)
            count = max(1, min(int(times or 1), 10))
            return await self._call_onebot_tool(action, {"user_id": uid, "times": count}, target_user_id=str(uid))
        except Exception as exc:
            return _json_error(action, exc, target_user_id=str(user_id or ""))

    async def onebotv11_delete_msg(self, message_id: str, reason: str = "") -> str:
        """
        Delete/revoke a OneBot message.

        :param message_id: OneBot message id to delete.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "delete_msg"
        try:
            mid = self._onebot_int(message_id, "message_id")
            result = await self._call_onebot_tool(action, {"message_id": mid}, message_id=str(mid))
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, message_id=str(message_id or ""))

    async def onebotv11_set_group_ban(
        self,
        user_id: str,
        duration: int = 1800,
        group_id: str = "",
        reason: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Mute or unmute a QQ group member. duration=0 un-mutes.

        :param user_id: Target QQ user id.
        :param duration: Mute duration in seconds. 0 means unmute.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param reason: Human-readable reason for audit/result context.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_ban"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            uid = self._onebot_user_id(user_id)
            result = await self._call_onebot_tool(
                action,
                {"group_id": gid, "user_id": uid, "duration": max(0, int(duration or 0))},
                group_id=str(gid),
                target_user_id=str(uid),
            )
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))

    async def onebotv11_set_group_kick(
        self,
        user_id: str,
        reject_add_request: bool = False,
        group_id: str = "",
        reason: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Kick a QQ group member.

        :param user_id: Target QQ user id.
        :param reject_add_request: Whether to reject future join requests from this user.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param reason: Human-readable reason for audit/result context.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_kick"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            uid = self._onebot_user_id(user_id)
            result = await self._call_onebot_tool(
                action,
                {"group_id": gid, "user_id": uid, "reject_add_request": bool(reject_add_request)},
                group_id=str(gid),
                target_user_id=str(uid),
            )
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))

    async def onebotv11_set_group_whole_ban(
        self,
        enable: bool = True,
        group_id: str = "",
        reason: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Enable or disable whole-group mute.

        :param enable: True to enable whole-group mute; false to disable.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param reason: Human-readable reason for audit/result context.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_whole_ban"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            result = await self._call_onebot_tool(action, {"group_id": gid, "enable": bool(enable)}, group_id=str(gid))
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_set_group_admin(
        self,
        user_id: str,
        enable: bool = True,
        group_id: str = "",
        reason: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Set or unset QQ group admin.

        :param user_id: Target QQ user id.
        :param enable: True to set admin; false to unset.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param reason: Human-readable reason for audit/result context.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_admin"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            uid = self._onebot_user_id(user_id)
            result = await self._call_onebot_tool(
                action,
                {"group_id": gid, "user_id": uid, "enable": bool(enable)},
                group_id=str(gid),
                target_user_id=str(uid),
            )
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))

    async def onebotv11_set_group_card(
        self,
        user_id: str,
        card: str = "",
        group_id: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Set QQ group card/remark. This is not the user's QQ nickname.

        :param user_id: Target QQ user id.
        :param card: New group card. Empty clears it.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_card"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            uid = self._onebot_user_id(user_id)
            return await self._call_onebot_tool(
                action,
                {"group_id": gid, "user_id": uid, "card": str(card or "")},
                group_id=str(gid),
                target_user_id=str(uid),
            )
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))

    async def onebotv11_set_group_name(self, group_name: str, group_id: str = "", chat_id: str = "") -> str:
        """
        Rename a QQ group.

        :param group_name: New group name.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_name"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(action, {"group_id": gid, "group_name": str(group_name or "")}, group_id=str(gid))
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_set_group_leave(
        self,
        group_id: str = "",
        is_dismiss: bool = False,
        reason: str = "",
        chat_id: str = "",
    ) -> str:
        """
        Leave or dismiss a QQ group.

        :param group_id: QQ group id. Leave empty to use current group chat.
        :param is_dismiss: Whether to dismiss the group when the login account is owner.
        :param reason: Human-readable reason for audit/result context.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_leave"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            result = await self._call_onebot_tool(action, {"group_id": gid, "is_dismiss": bool(is_dismiss)}, group_id=str(gid))
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_set_group_special_title(
        self,
        user_id: str,
        special_title: str = "",
        group_id: str = "",
        duration: int = -1,
        chat_id: str = "",
    ) -> str:
        """
        Set QQ group member special title.

        :param user_id: Target QQ user id.
        :param special_title: New special title. Empty clears it.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param duration: Title duration in seconds. -1 means permanent where supported.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "set_group_special_title"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            uid = self._onebot_user_id(user_id)
            return await self._call_onebot_tool(
                action,
                {
                    "group_id": gid,
                    "user_id": uid,
                    "special_title": str(special_title or ""),
                    "duration": int(duration),
                },
                group_id=str(gid),
                target_user_id=str(uid),
            )
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))

    async def onebotv11_napcat_send_poke(
        self,
        user_id: str,
        group_id: str = "",
        chat_id: str = "",
        reason: str = "",
    ) -> str:
        """
        NapCat extension: send poke in private chat or group.

        :param user_id: Target QQ user id.
        :param group_id: Optional QQ group id. Leave empty to use current group when available; otherwise private poke.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "send_poke"
        try:
            uid = self._onebot_user_id(user_id)
            params: dict[str, Any] = {"user_id": uid}
            try:
                gid = self._onebot_group_id(group_id, chat_id)
                params["group_id"] = gid
                context = {"group_id": str(gid), "target_user_id": str(uid)}
            except Exception:
                context = {"target_user_id": str(uid)}
            result = await self._call_onebot_tool(action, params, **context)
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, target_user_id=str(user_id or ""))

    async def onebotv11_napcat_mark_private_msg_as_read(self, user_id: str) -> str:
        """
        NapCat extension: mark a private chat as read.

        :param user_id: Target QQ user id.
        """
        action = "mark_private_msg_as_read"
        try:
            uid = self._onebot_user_id(user_id)
            return await self._call_onebot_tool(action, {"user_id": uid}, target_user_id=str(uid))
        except Exception as exc:
            return _json_error(action, exc, target_user_id=str(user_id or ""))

    async def onebotv11_napcat_mark_group_msg_as_read(self, group_id: str = "", chat_id: str = "") -> str:
        """
        NapCat extension: mark a group chat as read.

        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "mark_group_msg_as_read"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(action, {"group_id": gid}, group_id=str(gid))
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_napcat_get_friend_msg_history(
        self,
        user_id: str,
        message_seq: str = "0",
        count: int = 20,
        reverse_order: bool = False,
    ) -> str:
        """
        NapCat extension: get private friend message history.

        :param user_id: Target QQ user id.
        :param message_seq: Start message sequence. Default 0.
        :param count: Message count, default 20.
        :param reverse_order: Whether to return in reverse order.
        """
        action = "get_friend_msg_history"
        try:
            uid = self._onebot_user_id(user_id)
            params = {
                "user_id": str(uid),
                "message_seq": str(message_seq or "0"),
                "count": max(1, min(int(count or 20), 100)),
                "reverseOrder": bool(reverse_order),
            }
            return await self._call_onebot_tool(action, params, target_user_id=str(uid))
        except Exception as exc:
            return _json_error(action, exc, target_user_id=str(user_id or ""))

    async def onebotv11_napcat_fetch_custom_face(self, count: int = 48) -> str:
        """
        NapCat extension: fetch custom face list.

        :param count: Maximum count to fetch.
        """
        return await self._call_onebot_tool("fetch_custom_face", {"count": max(1, min(int(count or 48), 200))})

    async def onebotv11_napcat_get_ai_characters(
        self,
        group_id: str = "",
        chat_type: int = 1,
        chat_id: str = "",
    ) -> str:
        """
        NapCat extension: get AI voice characters.

        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_type: NapCat chat type value. Default 1.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "get_ai_characters"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(action, {"group_id": gid, "chat_type": int(chat_type or 1)}, group_id=str(gid))
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_napcat_get_ai_record(
        self,
        character: str,
        text: str,
        group_id: str = "",
        chat_id: str = "",
    ) -> str:
        """
        NapCat extension: convert text to an AI voice record URL.

        :param character: NapCat AI voice character id.
        :param text: Text to synthesize.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "get_ai_record"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            return await self._call_onebot_tool(
                action,
                {"character": str(character or ""), "group_id": gid, "text": str(text or "")},
                group_id=str(gid),
            )
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_napcat_send_group_ai_record(
        self,
        character: str,
        text: str,
        group_id: str = "",
        chat_id: str = "",
        reason: str = "",
    ) -> str:
        """
        NapCat extension: send an AI voice record to a QQ group.

        :param character: NapCat AI voice character id.
        :param text: Text to synthesize and send.
        :param group_id: QQ group id. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        :param reason: Human-readable reason for audit/result context.
        """
        action = "send_group_ai_record"
        try:
            gid = self._onebot_group_id(group_id, chat_id)
            result = await self._call_onebot_tool(
                action,
                {"character": str(character or ""), "group_id": gid, "text": str(text or "")},
                group_id=str(gid),
            )
            return self._with_reason(result, reason)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""))

    async def onebotv11_napcat_send_forward_msg(
        self,
        messages_json: str,
        message_type: str = "",
        user_id: str = "",
        group_id: str = "",
        chat_id: str = "",
    ) -> str:
        """
        NapCat extension: send merged forward message. messages_json must be a JSON array of OneBot node segments.

        :param messages_json: JSON array of OneBot node messages.
        :param message_type: private or group. Leave empty to infer from user_id/group_id/current group.
        :param user_id: Target QQ user id for private forward.
        :param group_id: Target QQ group id for group forward. Leave empty to use current group chat.
        :param chat_id: Auto-injected current platform chat id; normally leave empty.
        """
        action = "send_forward_msg"
        try:
            messages = json.loads(messages_json or "[]")
            if not isinstance(messages, list):
                raise ValueError("messages_json must decode to a JSON array.")
            params: dict[str, Any] = {"messages": messages}
            mtype = str(message_type or "").strip().lower()
            if group_id or chat_id or mtype == "group":
                gid = self._onebot_group_id(group_id, chat_id)
                params["group_id"] = gid
                params["message_type"] = "group"
                context = {"group_id": str(gid)}
            elif user_id or mtype == "private":
                uid = self._onebot_user_id(user_id)
                params["user_id"] = uid
                params["message_type"] = "private"
                context = {"target_user_id": str(uid)}
            else:
                raise ValueError("user_id or group_id is required when current chat is not a group.")
            return await self._call_onebot_tool(action, params, **context)
        except Exception as exc:
            return _json_error(action, exc, group_id=str(group_id or chat_id or ""), target_user_id=str(user_id or ""))
