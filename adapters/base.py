from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Dict


MessageHandler = Callable[[Dict[str, Any]], Awaitable[Any] | Any]


@dataclass(slots=True)
class PlatformFeatures:
    """平台能力描述。"""

    supports_edit_message: bool = False
    supports_delete_message: bool = False
    supports_reactions: bool = False
    supports_typing_indicator: bool = True
    supports_rich_media: bool = False
    supports_reply: bool = True
    supports_threads: bool = False
    supports_buttons: bool = False
    max_message_length: int | None = None


class BaseAdapter(ABC):
    """平台适配器基础抽象层。"""

    def __init__(self):
        self.current_chat_id: str | None = None
        self._message_handler: MessageHandler | None = None
        self._is_running: bool = False

    @property
    def platform_name(self) -> str:
        """平台名称，子类可覆盖。"""
        return self.__class__.__name__.replace("Adapter", "").lower() or "unknown"

    @property
    def platform_features(self) -> PlatformFeatures:
        """平台能力，子类可覆盖。"""
        return PlatformFeatures()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @abstractmethod
    def run(self, message_handler: MessageHandler):
        """启动适配器并开始监听消息。"""

    async def startup(self) -> None:
        """生命周期：适配器启动时触发。"""
        self._is_running = True
        await self.on_startup()

    async def on_startup(self) -> None:
        """钩子：子类可覆盖。"""
        return None

    async def on_ready(self) -> None:
        """钩子：适配器完成初始化并可收发消息时触发。"""
        return None

    async def before_message(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """钩子：消息分发给控制器前触发，可改写 context。"""
        return context

    async def after_message(self, context: Dict[str, Any], result: Any = None) -> None:
        """钩子：消息处理后触发。"""
        return None

    async def shutdown(self) -> None:
        """生命周期：适配器关闭时触发。"""
        self._is_running = False
        await self.on_shutdown()

    async def on_shutdown(self) -> None:
        """钩子：子类可覆盖。"""
        return None

    async def health_check(self) -> Dict[str, Any]:
        """返回基础健康状态。"""
        return {
            "ok": self._is_running,
            "platform": self.platform_name,
            "running": self._is_running,
            "features": asdict(self.platform_features),
        }

    @abstractmethod
    async def send_message(self, chat_id: str, text: str, **kwargs) -> str:
        """发送一条完整消息，返回平台消息 ID。"""

    @abstractmethod
    async def start_typing(self, chat_id: str):
        """显示“正在输入”状态。"""

    @abstractmethod
    async def stop_typing(self, chat_id: str):
        """停止“正在输入”状态（若平台支持）。"""

    async def edit_message(self, chat_id: str, message_id: str, new_text: str, **kwargs) -> bool:
        """编辑消息；默认不支持。"""
        return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """删除消息；默认不支持。"""
        return False
