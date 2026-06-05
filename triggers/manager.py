'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-02 22:08:41
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .base import BaseTrigger, TriggerEvent

logger = logging.getLogger(__name__)

TriggerNotifyCallback = Callable[[str, str, str], Awaitable[None]]


class TriggerManager:
    """Trigger 子系统管理器。"""

    def __init__(
        self,
        *,
        notify_callback: TriggerNotifyCallback,
        default_chat_id: str = "",
        default_chat_target: Optional[Dict[str, Any]] = None,
    ):
        self._notify_callback = notify_callback
        self.default_chat_id = default_chat_id
        self.default_chat_target = dict(default_chat_target or {})
        self._triggers: List[BaseTrigger] = []
        self._is_running = False

    def register(self, trigger: BaseTrigger) -> None:
        trigger.bind_model_caller(self._call_model)
        self._triggers.append(trigger)

    async def _call_model(
        self,
        *,
        model_alias: str,
        system_prompt: str,
        user_prompt: str,
        history: list[dict[str, str]],
    ) -> str:
        from brain.llm import get_llm_client

        llm = get_llm_client(model_alias=model_alias)
        return await llm.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history=history,
            tools=[],
        )

    async def startup(self) -> None:
        if self._is_running:
            return

        if not self._triggers:
            logger.warning("TriggerManager 启动但没有可用触发器（请检查 enabled 配置）")

        for trigger in self._triggers:
            await trigger.startup()
            trigger.run(self._handle_event)
            await trigger.on_ready()
            logger.info(f"Trigger 已就绪: {trigger.trigger_name}")

        self._is_running = True
        logger.info(f"TriggerManager 已启动，已注册 {len(self._triggers)} 个触发器")

    async def shutdown(self) -> None:
        if not self._is_running:
            return

        for trigger in self._triggers:
            await trigger.shutdown()

        self._is_running = False
        logger.info("TriggerManager 已停止")

    async def _handle_event(self, event: TriggerEvent) -> None:
        chat_id = event.chat_id or self.default_chat_id
        if not chat_id:
            logger.warning(
                f"[{event.trigger_name}] 丢弃事件：未配置 chat_id。event_type={event.event_type}"
            )
            return

        reason = event.reason.strip() if event.reason else f"trigger event: {event.event_type}"
        logger.info(f"[{event.trigger_name}] 分发事件: event_type={event.event_type}, chat_id={chat_id}")
        await self._notify_callback(chat_id, reason, event.event_type)

    async def health_check(self) -> Dict[str, Any]:
        details = []
        for trigger in self._triggers:
            details.append(await trigger.health_check())
        return {
            "ok": self._is_running,
            "running": self._is_running,
            "trigger_count": len(self._triggers),
            "details": details,
        }
