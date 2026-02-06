from abc import ABC, abstractmethod
from typing import Callable, Any

class BaseAdapter(ABC):
    """定义所有平台适配器必须实现的接口。"""

    @abstractmethod
    def run(self, message_handler: Callable[[str, str], Any]):
        """启动适配器并开始监听消息。"""
        pass

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> str:
        """发送一条完整的消息。"""
        pass

    @abstractmethod
    async def start_typing(self, chat_id: str):
        """向用户显示“正在输入...”状态。"""
        pass

    @abstractmethod
    async def stop_typing(self, chat_id: str):
        """停止“正在输入...”状态 (如果平台支持)。"""
        pass
