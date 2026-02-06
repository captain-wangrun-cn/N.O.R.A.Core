import asyncio
from typing import Callable, Any, Dict

class MessageAggregator:
    """消息聚合器，用于合并用户的连续输入。"""

    def __init__(self, timeout: float, on_complete: Callable[[str, str], Any]):
        self.timeout = timeout
        self.on_complete = on_complete
        self._buffers: Dict[str, str] = {}
        self._timers: Dict[str, asyncio.Task] = {}

    async def add_message(self, chat_id: str, text: str):
        """添加一条新消息到缓冲区。"""
        if chat_id in self._timers:
            self._timers[chat_id].cancel()

        current_buffer = self._buffers.get(chat_id, "")
        self._buffers[chat_id] = f"{current_buffer}\n{text}".strip()
        
        self._timers[chat_id] = asyncio.create_task(self._timer_expired(chat_id))

    async def _timer_expired(self, chat_id: str):
        """计时器到期后触发回调。"""
        await asyncio.sleep(self.timeout)
        
        full_message = self._buffers.pop(chat_id, "")
        if full_message:
            await self.on_complete(chat_id, full_message)
        
        self._timers.pop(chat_id, None)

    def is_waiting(self, chat_id: str) -> bool:
        """检查特定聊天是否正在等待更多消息。"""
        return chat_id in self._timers
