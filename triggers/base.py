'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-02 22:08:33
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import inspect
from typing import Any, Awaitable, Callable, Dict

TriggerEventHandler = Callable[["TriggerEvent"], Awaitable[None] | None]
ModelCaller = Callable[..., Awaitable[str]]


@dataclass(slots=True)
class TriggerEvent:
    """触发器事件结构。"""

    trigger_name: str
    source: str
    event_type: str
    reason: str
    payload: Dict[str, Any]
    chat_id: str | None = None


@dataclass(slots=True)
class TriggerFeatures:
    """触发器能力描述。"""

    supports_background_polling: bool = True
    supports_model_call: bool = True
    supports_custom_hooks: bool = True


class BaseTrigger(ABC):
    """外部触发器基础抽象层。"""

    def __init__(self):
        self._event_handler: TriggerEventHandler | None = None
        self._model_caller: ModelCaller | None = None
        self._is_running: bool = False

    @property
    def trigger_name(self) -> str:
        """触发器名称，子类可覆盖。"""
        return self.__class__.__name__.replace("Trigger", "").lower() or "unknown"

    @property
    def trigger_features(self) -> TriggerFeatures:
        """触发器能力，子类可覆盖。"""
        return TriggerFeatures()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def bind_model_caller(self, caller: ModelCaller) -> None:
        """绑定模型临时调用函数。"""
        self._model_caller = caller

    @abstractmethod
    def run(self, event_handler: TriggerEventHandler):
        """启动触发器并开始监听外部事件。"""

    async def startup(self) -> None:
        """生命周期：触发器启动时触发。"""
        self._is_running = True
        await self.on_startup()

    async def on_startup(self) -> None:
        """钩子：子类可覆盖。"""
        return None

    async def on_ready(self) -> None:
        """钩子：触发器完成初始化并可产出事件时触发。"""
        return None

    async def before_event(self, event: TriggerEvent) -> TriggerEvent:
        """钩子：事件分发给管理器前触发，可改写 event。"""
        return event

    async def after_event(self, event: TriggerEvent, result: Any = None) -> None:
        """钩子：事件处理后触发。"""
        return None

    async def shutdown(self) -> None:
        """生命周期：触发器关闭时触发。"""
        self._is_running = False
        await self.on_shutdown()

    async def on_shutdown(self) -> None:
        """钩子：子类可覆盖。"""
        return None

    async def health_check(self) -> Dict[str, Any]:
        """返回基础健康状态。"""
        return {
            "ok": self._is_running,
            "trigger": self.trigger_name,
            "running": self._is_running,
            "features": asdict(self.trigger_features),
        }

    async def call_model(
        self,
        *,
        model_alias: str,
        system_prompt: str,
        user_prompt: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """临时调用模型（例如 fast）执行触发判定。"""
        if not self._model_caller:
            raise RuntimeError("Model caller is not bound for trigger")
        return await self._model_caller(
            model_alias=model_alias,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history=history or [],
        )

    async def dispatch_event(self, event: TriggerEvent) -> None:
        """分发事件给管理器（含 before/after 钩子）。"""
        if not self._event_handler:
            return

        event = await self.before_event(event)
        maybe_result = self._event_handler(event)
        result = await maybe_result if inspect.isawaitable(maybe_result) else maybe_result
        await self.after_event(event, result=result)
