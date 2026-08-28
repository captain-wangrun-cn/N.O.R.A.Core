"""RTC 通话会话（P1）。

CallSession = 一通电话的完整生命周期（状态机）+ 时长硬上限 + 单并发注册表。
设计文档 docs/architecture/rtc.md §5（session.py 职责）。

状态：
    connecting → active → ending → closed
    connecting 阶段失败直接 closed。

并发：SessionRegistry 全局单并发（owner-only MVP）——同一时刻只允许一通电话。
mute：会话记住静音状态，server.py 据此停止上行转发（音频仍可收）。

本模块不感知 WS / Google 协议，只管状态与计时；IO 由 server.py 驱动。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Awaitable

logger = logging.getLogger(__name__)


class CallState(str, Enum):
    CONNECTING = "connecting"
    ACTIVE = "active"
    ENDING = "ending"
    CLOSED = "closed"


@dataclass
class CallSession:
    """一通电话。created_at 用 loop.time() 便于测试注入。"""

    session_id: str
    user_id: str
    platform: str
    display_name: str = ""
    state: CallState = CallState.CONNECTING
    muted: bool = False
    # 入账用统计（P2 成本跟踪消费）
    bytes_up: int = 0
    bytes_down: int = 0
    started_at: float = 0.0  # ACTIVE 时刻
    created_at: float = field(default_factory=time.monotonic)
    # 时长上限到点回调（server.py 注入：发 bye{limit} 并收链）
    _on_limit: Optional[Callable[["CallSession"], Awaitable[None]]] = None
    _limit_task: Optional[asyncio.Task] = None
    _meta: dict[str, Any] = field(default_factory=dict)

    def activate(self) -> None:
        if self.state is not CallState.CONNECTING:
            raise RuntimeError(f"activate() 要求 CONNECTING，当前 {self.state}")
        self.state = CallState.ACTIVE
        self.started_at = time.monotonic()

    def begin_ending(self) -> bool:
        """进入 ending（幂等）。已在 ending/closed 返回 False。"""
        if self.state in (CallState.ENDING, CallState.CLOSED):
            return False
        self.state = CallState.ENDING
        return True

    def close(self) -> None:
        self.state = CallState.CLOSED
        self._cancel_limit_task()

    # ------------------------------------------------------------ 时长上限

    def arm_limit_timer(self, max_seconds: int,
                        on_limit: Callable[["CallSession"], Awaitable[None]]) -> None:
        """武装时长硬上限。到点回调 on_limit（发 bye 并结束）。"""
        self._on_limit = on_limit
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.0
        remaining = max(0.1, max_seconds - elapsed)
        self._cancel_limit_task()
        self._limit_task = asyncio.get_running_loop().create_task(
            self._limit_delay(remaining)
        )

    async def _limit_delay(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self.state in (CallState.ENDING, CallState.CLOSED):
            return
        logger.info("RTC 通话到达时长上限: session=%s", self.session_id)
        if self._on_limit:
            try:
                await self._on_limit(self)
            except Exception as e:  # noqa: BLE001
                logger.error("时长上限回调异常: %s", e)

    def _cancel_limit_task(self) -> None:
        if self._limit_task and not self._limit_task.done():
            self._limit_task.cancel()
        self._limit_task = None

    # ---------------------------------------------------------------- 杂项

    @property
    def active_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0

    def snapshot(self) -> dict:
        """给 welcome / 日志用的只读快照。"""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "platform": self.platform,
            "display_name": self.display_name,
            "state": self.state.value,
            "muted": self.muted,
            "active_seconds": round(self.active_seconds, 1),
        }


class SessionRegistry:
    """单并发注册表。owner-only MVP：全局同时只允许一通。"""

    def __init__(self, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self._current: Optional[CallSession] = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> Optional[CallSession]:
        return self._current

    def busy(self) -> bool:
        s = self._current
        return s is not None and s.state in (CallState.CONNECTING, CallState.ACTIVE,
                                             CallState.ENDING)

    async def try_open(self, session: CallSession) -> tuple[bool, str]:
        """尝试占用并发槽。失败返回 (False, 占用者的描述)。"""
        async with self._lock:
            if self.busy():
                other = self._current
                return False, (other.snapshot().get("display_name") or other.user_id
                               if other else "unknown")
            self._current = session
            return True, ""

    def release(self, session: CallSession) -> None:
        if self._current is session:
            self._current = None
