import asyncio
from collections import deque
from typing import Callable, Any, Dict, Deque

from adapters.base import normalize_mentioned_users

class MessageAggregator:
    """消息聚合器，用于合并用户的连续输入。"""

    def __init__(self, timeout: float, on_complete: Callable[[Dict[str, Any]], Any]):
        self.timeout = timeout
        self.on_complete = on_complete
        self._buffers: Dict[str, str] = {}
        self._parts: Dict[str, list[Dict[str, Any]]] = {}
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self._timers: Dict[str, asyncio.Task] = {}
        # 记录最近处理过的 message_id，避免平台重复投递导致的重复拼接
        self._recent_message_ids: Dict[str, Deque[str]] = {}

    def _key_for(self, chat_id: str, context: Dict[str, Any]) -> str:
        platform = str(context.get("platform") or "telegram")
        chat_type = str(context.get("chat_type") or "private").lower()
        if chat_type == "group":
            user_id = str(context.get("user_id") or "").strip()
            if user_id:
                return f"{platform}:{chat_id}:{user_id}"
        return f"{platform}:{chat_id}"

    def _format_parts(self, parts: list[Dict[str, Any]], context: Dict[str, Any]) -> str:
        chat_type = str(context.get("chat_type") or "private").lower()
        if chat_type == "private":
            return "\n".join(str(part.get("text", "")).strip() for part in parts if str(part.get("text", "")).strip()).strip()

        lines = []
        multiple_senders = len({str(part.get("user_id", "")) for part in parts if part.get("user_id")}) > 1
        for part in parts:
            text = str(part.get("text", "")).strip()
            if not text:
                continue
            if multiple_senders:
                speaker = str(part.get("user_name") or part.get("user_id") or "User")
                lines.append(f"{speaker}: {text}")
            else:
                lines.append(text)
        return "\n".join(lines).strip()

    def _context_platform_message_ids(self, context: Dict[str, Any]) -> list[str]:
        raw_ids = context.get("platform_message_ids")
        ids: list[str] = []
        if isinstance(raw_ids, (list, tuple, set)):
            ids.extend(str(mid) for mid in raw_ids if mid is not None)
        elif raw_ids is not None:
            ids.append(str(raw_ids))
        single_id = context.get("platform_message_id")
        if single_id is not None:
            ids.append(str(single_id))
        return list(dict.fromkeys(mid for mid in ids if mid))

    def _reply_target_message_id(self, context: Dict[str, Any]) -> str:
        return str(context.get("reply_target_message_id") or "").strip()

    async def add_message(self, chat_id: str, text: str, context: Dict[str, Any]):
        """添加一条新消息到缓冲区。"""
        key = self._key_for(str(chat_id), context)
        platform_msg_id = context.get("platform_message_id")
        if platform_msg_id:
            msg_id_str = str(platform_msg_id)
            recent_ids = self._recent_message_ids.setdefault(key, deque(maxlen=50))
            if msg_id_str in recent_ids:
                # 已处理过同一平台消息，跳过重复聚合
                return
            recent_ids.append(msg_id_str)

        if key in self._timers:
            self._timers[key].cancel()

        parts = self._parts.setdefault(key, [])
        parts.append({
            "text": text,
            "user_id": context.get("user_id"),
            "user_name": context.get("user_name"),
            "platform_message_id": platform_msg_id,
            "platform_message_ids": self._context_platform_message_ids(context),
            "reply_to_message_id": str(context.get("reply_to_message_id") or "").strip(),
            "reply_to_user_id": str(context.get("reply_to_user_id") or "").strip(),
            "reply_to_user_name": str(context.get("reply_to_user_name") or "").strip(),
            "mentioned_user_ids": [
                str(user_id)
                for user_id in (context.get("mentioned_user_ids") or [])
                if user_id is not None and str(user_id).strip()
            ],
            "mentioned_users": normalize_mentioned_users(context.get("mentioned_users")),
            "reply_target_message_id": self._reply_target_message_id(context),
        })
        self._buffers[key] = self._format_parts(parts, context)
        
        # Update context, ensuring the 'text' field is always the latest full message
        if key not in self._contexts:
            self._contexts[key] = dict(context)
        else:
            existing = self._contexts[key]
            existing.update(context)
            existing.setdefault("first_user_id", parts[0].get("user_id"))
            existing.setdefault("first_user_name", parts[0].get("user_name"))
        self._contexts[key]["text"] = self._buffers[key]
        self._contexts[key]["contributors"] = [
            {"user_id": part.get("user_id"), "user_name": part.get("user_name")}
            for part in parts
        ]
        self._contexts[key]["native_reference_parts"] = [
            {
                "platform_message_id": part.get("platform_message_id"),
                "platform_message_ids": list(part.get("platform_message_ids") or []),
                "reply_to_message_id": part.get("reply_to_message_id") or "",
                "reply_to_user_id": part.get("reply_to_user_id") or "",
                "reply_to_user_name": part.get("reply_to_user_name") or "",
                "mentioned_user_ids": list(part.get("mentioned_user_ids") or []),
                "mentioned_users": [dict(user) for user in (part.get("mentioned_users") or [])],
            }
            for part in parts
        ]
        latest_reply = next(
            (
                part
                for part in reversed(parts)
                if str(part.get("reply_to_message_id") or "").strip()
            ),
            None,
        )
        if latest_reply:
            for field in ("reply_to_message_id", "reply_to_user_id", "reply_to_user_name"):
                value = str(latest_reply.get(field) or "").strip()
                if value:
                    self._contexts[key][field] = value
                else:
                    self._contexts[key].pop(field, None)
        mentioned_user_ids = [
            str(user_id)
            for part in parts
            for user_id in (part.get("mentioned_user_ids") or [])
            if user_id is not None and str(user_id).strip()
        ]
        if mentioned_user_ids:
            self._contexts[key]["mentioned_user_ids"] = list(dict.fromkeys(mentioned_user_ids))
        else:
            self._contexts[key].pop("mentioned_user_ids", None)
        mentioned_users_by_id: dict[str, dict[str, str]] = {}
        for part in parts:
            for mentioned_user in (part.get("mentioned_users") or []):
                user_id = str(mentioned_user.get("user_id") or "").strip()
                if user_id and user_id not in mentioned_users_by_id:
                    mentioned_users_by_id[user_id] = dict(mentioned_user)
        if mentioned_users_by_id:
            self._contexts[key]["mentioned_users"] = list(mentioned_users_by_id.values())
        else:
            self._contexts[key].pop("mentioned_users", None)
        platform_ids = []
        for part in parts:
            part_ids = part.get("platform_message_ids") or []
            if isinstance(part_ids, (list, tuple, set)):
                platform_ids.extend(str(mid) for mid in part_ids if mid is not None)
            elif part_ids:
                platform_ids.append(str(part_ids))
        if platform_ids:
            self._contexts[key]["platform_message_ids"] = list(dict.fromkeys(platform_ids))
        reply_target = str(parts[0].get("reply_target_message_id") or "").strip()
        if reply_target:
            self._contexts[key]["reply_target_message_id"] = reply_target
        for field in (
            "chat_title",
            "chat_username",
            "group_title",
            "group_display_name",
            "group_member_count",
            "group_member_count_status",
            "group_online_count",
            "group_online_count_status",
        ):
            if field in context:
                self._contexts[key][field] = context.get(field)
        
        self._timers[key] = asyncio.create_task(self._timer_expired(key))

    async def _timer_expired(self, key: str):
        """计时器到期后触发回调。"""
        await asyncio.sleep(self.timeout)
        
        full_context = self._contexts.pop(key, None)
        self._buffers.pop(key, None) # Also clear buffer
        self._parts.pop(key, None)
        
        if full_context:
            await self.on_complete(full_context)
        
        self._timers.pop(key, None)

    def is_waiting(self, chat_id: str) -> bool:
        """检查特定聊天是否正在等待更多消息。"""
        return chat_id in self._timers or any(key.endswith(f":{chat_id}") for key in self._timers)
