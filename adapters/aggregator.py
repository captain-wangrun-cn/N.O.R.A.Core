import asyncio
from collections import deque
from typing import Callable, Any, Dict, Deque

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
        platform_ids = [str(part.get("platform_message_id")) for part in parts if part.get("platform_message_id")]
        if platform_ids:
            self._contexts[key]["platform_message_ids"] = platform_ids
        
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
