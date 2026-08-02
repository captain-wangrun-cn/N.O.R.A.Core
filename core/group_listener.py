from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from core.group_presence_store import GroupPresence, GroupPresenceStore

logger = logging.getLogger(__name__)


class PassiveAction(str, Enum):
    KEEP_LISTENING = "KEEP_LISTENING"
    REPLY = "REPLY"
    SEMI_ONLINE = "SEMI_ONLINE"


class AppendAction(str, Enum):
    APPEND = "APPEND"
    KEEP_PENDING = "KEEP_PENDING"


@dataclass
class GroupMessageEvent:
    runtime_key: str
    memory_scope_id: str
    place_scope_id: str
    platform: str
    platform_chat_id: str
    sequence: int
    received_at: float
    sender_id: str
    sender_name: str
    text: str
    platform_message_id: str = ""
    platform_message_ids: Tuple[str, ...] = ()
    reply_to_message_id: str = ""
    reply_to_user_id: str = ""
    reply_to_user_name: str = ""
    reply_to_text: str = ""
    explicit_bot_mention: bool = False
    reply_to_bot: bool = False
    context: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_context(cls, context: Dict[str, Any], sequence: int) -> "GroupMessageEvent":
        platform = str(context.get("platform") or "unknown")
        platform_chat_id = str(context.get("chat_id") or context.get("platform_chat_id") or "")
        raw_ids = context.get("platform_message_ids") or []
        if not isinstance(raw_ids, (list, tuple, set)):
            raw_ids = [raw_ids]
        ids = [str(value) for value in raw_ids if value is not None and str(value)]
        single_id = str(context.get("platform_message_id") or "")
        if single_id and single_id not in ids:
            ids.insert(0, single_id)
        runtime_key = str(context.get("runtime_key") or f"{platform}:{platform_chat_id}")
        return cls(
            runtime_key=runtime_key,
            memory_scope_id=str(context.get("memory_scope_id") or ""),
            place_scope_id=str(context.get("place_scope_id") or ""),
            platform=platform,
            platform_chat_id=platform_chat_id,
            sequence=sequence,
            received_at=float(context.get("received_at") or time.time()),
            sender_id=str(context.get("user_id") or ""),
            sender_name=str(context.get("user_name") or context.get("actor_display_name") or "User"),
            text=str(context.get("text") or ""),
            platform_message_id=single_id,
            platform_message_ids=tuple(ids),
            reply_to_message_id=str(context.get("reply_to_message_id") or ""),
            reply_to_user_id=str(context.get("reply_to_user_id") or ""),
            reply_to_user_name=str(context.get("reply_to_user_name") or ""),
            reply_to_text=str(context.get("reply_to_text") or ""),
            explicit_bot_mention=bool(context.get("explicit_bot_mention")),
            reply_to_bot=bool(context.get("reply_to_bot")),
            context=dict(context),
        )

    @property
    def unique_key(self) -> Tuple[str, str, str]:
        local_id = self.platform_message_id or (self.platform_message_ids[0] if self.platform_message_ids else "")
        if not local_id:
            local_id = f"runtime-seq:{self.sequence}"
        return self.platform, self.platform_chat_id, local_id

    def replied_target_event(self) -> Optional["GroupMessageEvent"]:
        """Build a deduplicatable event for a reliably available replied-to message."""
        if not self.reply_to_message_id or not self.reply_to_text:
            return None
        context = dict(self.context)
        context.update(
            {
                "text": self.reply_to_text,
                "user_id": self.reply_to_user_id,
                "user_name": self.reply_to_user_name or "User",
                "platform_message_id": self.reply_to_message_id,
                "platform_message_ids": [self.reply_to_message_id],
                "explicit_bot_mention": False,
                "reply_to_bot": False,
            }
        )
        return GroupMessageEvent(
            runtime_key=self.runtime_key,
            memory_scope_id=self.memory_scope_id,
            place_scope_id=self.place_scope_id,
            platform=self.platform,
            platform_chat_id=self.platform_chat_id,
            sequence=max(0, self.sequence - 1),
            received_at=self.received_at,
            sender_id=self.reply_to_user_id,
            sender_name=self.reply_to_user_name or "User",
            text=self.reply_to_text,
            platform_message_id=self.reply_to_message_id,
            platform_message_ids=(self.reply_to_message_id,),
            context=context,
        )


@dataclass
class GenerationCycle:
    token: int
    input_events: List[GroupMessageEvent]
    task: Optional[asyncio.Task] = None
    ordinary_interrupt_used: bool = False
    ordinary_bucket: List[GroupMessageEvent] = field(default_factory=list)
    restart_pending: bool = False
    priority_handoff: bool = False
    priority_events: List[GroupMessageEvent] = field(default_factory=list)


@dataclass
class GroupRuntimeState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_window: Deque[GroupMessageEvent] = field(default_factory=deque)
    new_since_passive_eval: int = 0
    last_received_at: float = 0.0
    # 本群进入 ONLINE 的时间；进程重启后由持久化 updated_at 兜底。
    online_since: float = 0.0
    # 本次 ONLINE 是怎么开起来的："interaction"（@/回复/明确意图）或 "scheduled"（定时计划）。
    # 定时开启意味着没人叫过 Nora，分类器应据此进一步提高自主插话门槛。
    online_trigger_kind: str = "interaction"
    # 最近一次「与 Nora 的互动」：定向消息（@/回复 Nora）或 Nora 自己开始生成。
    last_interaction_at: float = 0.0
    last_passive_at: float = 0.0
    # 连续 KEEP_LISTENING 次数 / 连续 SEMI_ONLINE 投票次数（供 prompt 与降级判定使用）。
    consecutive_keep_listening: int = 0
    consecutive_semi_online_votes: int = 0
    idle_generation: int = 0
    idle_task: Optional[asyncio.Task] = None
    passive_task: Optional[asyncio.Task] = None
    directed_handoff: bool = False
    # 定向批次已提升、但生成周期还没登记（begin_generation 之前）时到达的定向事件。
    # 这段窗口里再开一轮会造成两轮并发生成、互相看到对方已入库的消息，从而张冠李戴。
    directed_pending: List[GroupMessageEvent] = field(default_factory=list)
    generation: Optional[GenerationCycle] = None


def _split_runtime_key(runtime_key: str) -> Tuple[str, str]:
    """Split ``platform:chat_id`` so persisted records never lose platform metadata."""
    key = str(runtime_key or "")
    if ":" in key:
        platform, chat_id = key.split(":", 1)
        return platform.strip(), chat_id.strip()
    return "", key.strip()


PersistCallback = Callable[[Dict[str, Any]], Awaitable[None]]
PassiveDecisionCallback = Callable[[str, Sequence[GroupMessageEvent]], Awaitable[PassiveAction]]
AppendDecisionCallback = Callable[[str, Sequence[GroupMessageEvent], Sequence[GroupMessageEvent]], Awaitable[AppendAction]]
PromoteCallback = Callable[[str, Sequence[GroupMessageEvent], str], Awaitable[None]]
InterruptCallback = Callable[[str, Sequence[GroupMessageEvent], str, int], Awaitable[None]]


class GroupListenerManager:
    """Coordinates per-group passive listening without creating a new memory scope."""

    def __init__(
        self,
        *,
        store: GroupPresenceStore,
        persist_message: PersistCallback,
        decide_passive: PassiveDecisionCallback,
        decide_append: AppendDecisionCallback,
        promote: PromoteCallback,
        interrupt: InterruptCallback,
        batch_size: int = 2,
        idle_seconds: float = 90.0,
        max_pending: int = 20,
        enabled: bool = True,
        watchdog_interval_seconds: float = 30.0,
        no_interaction_timeout_seconds: float = 1800.0,
        idle_exit_seconds: float = 1200.0,
        max_online_seconds: float = 21600.0,
        directed_veto_seconds: float = 300.0,
        semi_online_vote_threshold: int = 2,
    ):
        self.store = store
        self.persist_message = persist_message
        self.decide_passive = decide_passive
        self.decide_append = decide_append
        self.promote = promote
        self.interrupt = interrupt
        self.batch_size = max(1, int(batch_size))
        self.idle_seconds = max(0.01, float(idle_seconds))
        self.max_pending = max(1, int(max_pending))
        self.enabled = bool(enabled)
        # 看门狗：唯一不依赖模型、也不依赖 pending 窗口非空的降级路径。
        self.watchdog_interval_seconds = max(1.0, float(watchdog_interval_seconds))
        self.no_interaction_timeout_seconds = max(0.0, float(no_interaction_timeout_seconds))
        self.idle_exit_seconds = max(0.0, float(idle_exit_seconds))
        self.max_online_seconds = max(0.0, float(max_online_seconds))
        self.directed_veto_seconds = max(0.0, float(directed_veto_seconds))
        self.semi_online_vote_threshold = max(1, int(semi_online_vote_threshold))
        self._states: Dict[str, GroupRuntimeState] = {}
        self._mode_lock = asyncio.Lock()
        self._sequence = 0
        self._generation_token = 0
        self._closed = False
        self._watchdog_task: Optional[asyncio.Task] = None

    def mode_for(self, runtime_key: str) -> GroupPresence:
        if not self.enabled:
            return GroupPresence.SEMI_ONLINE
        return self.store.get(runtime_key)

    def _event_vetoes_semi_online(self, event: GroupMessageEvent, now: float) -> bool:
        """Whether a directed event is recent enough to block a SEMI_ONLINE decision."""
        if not (event.explicit_bot_mention or event.reply_to_bot):
            return False
        if not self.directed_veto_seconds:
            return True
        received_at = float(event.received_at or 0.0)
        if not received_at:
            return True
        return (now - received_at) < self.directed_veto_seconds

    def _online_marks(self, runtime_key: str, state: GroupRuntimeState) -> Tuple[float, float]:
        """Return (online_since, last_interaction_at), seeding from persisted state.

        进程重启后运行时字段为 0，用持久化的 updated_at 兜底，
        否则看门狗会把「刚重启」误判成「刚进入 ONLINE、从未互动」。
        """
        if not state.online_since:
            persisted = 0.0
            getter = getattr(self.store, "updated_at", None)
            if callable(getter):
                try:
                    persisted = float(getter(runtime_key) or 0.0)
                except Exception:
                    persisted = 0.0
            state.online_since = persisted or time.time()
        if not state.last_interaction_at:
            state.last_interaction_at = state.online_since
        return state.online_since, state.last_interaction_at

    def _ensure_watchdog(self) -> None:
        if self._closed or not self.enabled:
            return
        if self._watchdog_task and not self._watchdog_task.done():
            return
        try:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        except RuntimeError:
            # 无运行中的事件循环（同步构造/测试环境）——下次 receive 时再试。
            self._watchdog_task = None

    def start(self) -> None:
        """Arm the demotion watchdog once the event loop is running.

        必须在启动时显式调用：重启后如果某群仍持久化为 ONLINE 但一直没有新消息，
        懒启动（receive / set_mode）永远不会触发，那条 ONLINE 就又挂住了。
        """
        self._ensure_watchdog()

    async def _watchdog_loop(self) -> None:
        """Force-demote ONLINE groups on hard timeouts, independent of the fast model.

        这是唯一不经过模型、也不要求 pending_window 非空的降级路径。此前
        「被 @ → 回复 → 群彻底安静」会把窗口清空，之后既不会再触发 fast 判断，
        也没有任何超时，ONLINE 于是永久挂着。
        """
        try:
            while not self._closed:
                await asyncio.sleep(self.watchdog_interval_seconds)
                if self._closed or not self.enabled:
                    return
                try:
                    await self._run_watchdog_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("群监听看门狗轮次异常", exc_info=True)
        except asyncio.CancelledError:
            return

    def _demote_reason(
        self,
        runtime_key: str,
        state: GroupRuntimeState,
        now: float,
        *,
        prefix: str = "watchdog",
    ) -> str:
        """Compute a hard-timeout demotion reason. Caller must hold ``state.lock``."""
        online_since, last_interaction_at = self._online_marks(runtime_key, state)
        last_activity = max(state.last_received_at, last_interaction_at)
        if self.max_online_seconds and now - online_since >= self.max_online_seconds:
            return f"{prefix}_max_online"
        if (
            self.no_interaction_timeout_seconds
            and now - last_interaction_at >= self.no_interaction_timeout_seconds
        ):
            return f"{prefix}_no_interaction"
        if self.idle_exit_seconds and now - last_activity >= self.idle_exit_seconds:
            return f"{prefix}_group_idle"
        return ""

    async def _run_watchdog_once(self) -> None:
        now = time.time()
        try:
            online_keys = [
                key for key, mode in self.store.all().items() if mode == GroupPresence.ONLINE
            ]
        except Exception:
            logger.warning("群监听看门狗读取群状态失败", exc_info=True)
            return

        for runtime_key in online_keys:
            state = self._state(runtime_key)
            async with state.lock:
                if state.generation or state.directed_handoff:
                    continue
                reason = self._demote_reason(runtime_key, state, now)
                online_since = state.online_since
                last_interaction_at = state.last_interaction_at
            if not reason:
                continue
            logger.info(
                "[%s] 群监听看门狗强制降级: reason=%s online_for=%.0fs "
                "since_interaction=%.0fs since_message=%.0fs window=%d",
                runtime_key,
                reason,
                now - online_since,
                now - last_interaction_at,
                now - state.last_received_at if state.last_received_at else -1,
                len(state.pending_window),
            )
            await self.set_mode(runtime_key, GroupPresence.SEMI_ONLINE, reason=reason)

    async def set_mode(
        self,
        runtime_key: str,
        mode: GroupPresence,
        *,
        platform: str = "",
        platform_chat_id: str = "",
        reason: str = "unspecified",
        expected_last_sequence: Optional[int] = None,
    ) -> bool:
        """Set the active group mode, demoting any previously ONLINE group."""
        mode = GroupPresence(mode)
        now = time.time()
        async with self._mode_lock:
            state = self._state(runtime_key)
            async with state.lock:
                if expected_last_sequence is not None:
                    if state.generation or state.directed_handoff or not state.pending_window:
                        return False
                    if any(event.sequence > expected_last_sequence for event in state.pending_window):
                        return False
                    # 定向事件只在「近期」内一票否决。生成期到达的 reply_to_bot 会经
                    # finish_generation 落进 pending_window，若无限期否决，它会一直
                    # 卡住降级直到被 max_pending 挤出窗口。
                    if any(
                        self._event_vetoes_semi_online(event, now)
                        for event in state.pending_window
                    ):
                        return False
                old_mode = self.mode_for(runtime_key)

            replaced = []
            if mode == GroupPresence.ONLINE:
                replaced = [
                    key
                    for key, current_mode in self.store.all().items()
                    if key != runtime_key and current_mode == GroupPresence.ONLINE
                ]

            # 持久化记录必须带平台元数据，供过期巡检与跨平台排查使用。
            key_platform, key_chat_id = _split_runtime_key(runtime_key)
            if not self.store.set(
                runtime_key,
                mode,
                platform=platform or key_platform,
                platform_chat_id=platform_chat_id or key_chat_id,
            ):
                return False

            if mode == GroupPresence.SEMI_ONLINE:
                async with state.lock:
                    state.pending_window.clear()
                    state.new_since_passive_eval = 0
                    state.consecutive_keep_listening = 0
                    state.consecutive_semi_online_votes = 0
                    state.online_since = 0.0
                    state.online_trigger_kind = "interaction"
                    state.last_interaction_at = 0.0
                    state.idle_generation += 1
                    self._cancel_task(state.idle_task)
                    state.idle_task = None
            elif mode == GroupPresence.ONLINE and old_mode != GroupPresence.ONLINE:
                async with state.lock:
                    state.online_since = now
                    # 定时计划开启的监听没有任何人叫过 Nora，分类器需要知道这一点。
                    state.online_trigger_kind = (
                        "scheduled" if reason == "scheduled_group_listen" else "interaction"
                    )
                    state.last_interaction_at = now
                    state.consecutive_keep_listening = 0
                    state.consecutive_semi_online_votes = 0

            for replaced_key in replaced:
                replaced_state = self._states.get(replaced_key)
                if replaced_state is not None:
                    async with replaced_state.lock:
                        self._clear_runtime_state(replaced_state, cancel_passive=True)
                logger.info(
                    "[GROUP PRESENCE] active group replaced: %s -> %s reason=%s",
                    replaced_key,
                    runtime_key,
                    reason,
                )

        if mode == GroupPresence.ONLINE:
            self._ensure_watchdog()

        if old_mode != mode:
            logger.info(
                "[GROUP PRESENCE] runtime=%s %s -> %s reason=%s",
                runtime_key,
                old_mode.value,
                mode.value,
                reason,
            )
        else:
            logger.debug(
                "[GROUP PRESENCE] runtime=%s unchanged=%s reason=%s",
                runtime_key,
                mode.value,
                reason,
            )
        return True

    async def receive(self, context: Dict[str, Any]) -> bool:
        """Accept an ONLINE group event. Returns False when normal handling should be used."""
        if self._closed or not self.enabled:
            return False
        runtime_key = str(context.get("runtime_key") or "")
        if not runtime_key:
            platform = str(context.get("platform") or "unknown")
            runtime_key = f"{platform}:{context.get('chat_id') or ''}"
            context = dict(context)
            context["runtime_key"] = runtime_key
        if self.mode_for(runtime_key) != GroupPresence.ONLINE:
            return False

        await self.persist_message(context)
        async with self._mode_lock:
            if self.mode_for(runtime_key) != GroupPresence.ONLINE:
                return False
            self._sequence += 1
            event = GroupMessageEvent.from_context(context, self._sequence)
            state = self._state(runtime_key)

            interrupt_payload: Optional[Tuple[List[GroupMessageEvent], str, int]] = None
            append_payload: Optional[Tuple[List[GroupMessageEvent], List[GroupMessageEvent], int]] = None
            direct_promote: Optional[List[GroupMessageEvent]] = None
            run_passive = False
            async with state.lock:
                state.last_received_at = event.received_at
                self._online_marks(runtime_key, state)
                if event.explicit_bot_mention or event.reply_to_bot:
                    # 与 Nora 的真实互动：刷新看门狗的「无互动」计时。
                    state.last_interaction_at = event.received_at
                self._reset_idle_timer(runtime_key, state)
                generation = state.generation
                if generation is not None:
                    if generation.priority_handoff or generation.restart_pending:
                        generation.priority_events.append(event)
                    elif event.explicit_bot_mention:
                        self._cancel_task(state.passive_task)
                        state.passive_task = None
                        generation.priority_events.extend(self._take_recent_pending(state, limit=5))
                        reply_target = event.replied_target_event()
                        if reply_target is not None:
                            generation.priority_events.append(reply_target)
                        generation.priority_events.append(event)
                        generation.priority_handoff = True
                        generation.restart_pending = True
                        merged = self._merge_events(generation.input_events, generation.priority_events)
                        interrupt_payload = (merged, "mention", generation.token)
                    elif generation.ordinary_interrupt_used:
                        self._append_pending(state, event)
                    else:
                        generation.ordinary_bucket.append(event)
                        if len(generation.ordinary_bucket) >= self.batch_size:
                            batch = generation.ordinary_bucket[:self.batch_size]
                            del generation.ordinary_bucket[:self.batch_size]
                            append_payload = (list(generation.input_events), batch, generation.token)
                elif event.explicit_bot_mention or event.reply_to_bot:
                    if event.explicit_bot_mention:
                        self._cancel_task(state.passive_task)
                        state.passive_task = None
                        recent_context = self._take_recent_pending(state, limit=5)
                    else:
                        recent_context = []
                    reply_target = event.replied_target_event()
                    directed_events = self._merge_events(
                        recent_context,
                        [reply_target] if reply_target is not None else [],
                        [event],
                    )
                    if state.directed_handoff:
                        # 上一条定向消息已提升但生成周期尚未登记：此刻再开一轮会出现两轮
                        # 并发生成，各自都能读到对方已入库的消息，于是「回答甲的内容、@乙」。
                        # 先寄存，等 begin_generation 把它并入同一周期的优先事件。
                        state.directed_pending.extend(directed_events)
                    else:
                        direct_promote = directed_events
                        state.directed_handoff = True
                else:
                    self._append_pending(state, event)
                    run_passive = state.new_since_passive_eval >= self.batch_size

        if interrupt_payload:
            events, reason, token = interrupt_payload
            asyncio.create_task(self.interrupt(runtime_key, events, reason, token))
        if append_payload:
            base, batch, token = append_payload
            asyncio.create_task(self._run_append_decision(runtime_key, base, batch, token))
        if direct_promote:
            logger.info(
                "[%s] 群监听定向消息直达前脑: mention=%s reply_to_bot=%s events=%d",
                runtime_key,
                event.explicit_bot_mention,
                event.reply_to_bot,
                len(direct_promote),
            )
            try:
                await self.promote(runtime_key, direct_promote, "directed_message")
            finally:
                async with state.lock:
                    if state.directed_handoff and state.generation is None:
                        state.directed_handoff = False
                    if state.directed_pending and state.generation is None:
                        # 提升没能登记生成周期（群被换掉/提升提前返回）：寄存的定向事件
                        # 退回被动窗口，宁可晚一轮判断，也不能凭空丢掉。
                        for pending_event in state.directed_pending:
                            self._append_pending(state, pending_event)
                        state.directed_pending = []
        if run_passive:
            self._ensure_passive_task(runtime_key, trigger="count")
        self._ensure_watchdog()
        return True

    async def begin_generation(
        self,
        runtime_key: str,
        events: Sequence[GroupMessageEvent],
        task: Optional[asyncio.Task] = None,
        *,
        continuation_token: Optional[int] = None,
    ) -> Optional[int]:
        async with self._mode_lock:
            if self.mode_for(runtime_key) != GroupPresence.ONLINE:
                return None
            state = self._state(runtime_key)
            async with state.lock:
                previous = state.generation
                continuation = bool(
                    previous
                    and continuation_token is not None
                    and previous.token == continuation_token
                )
                self._generation_token += 1
                cycle = GenerationCycle(
                    token=self._generation_token,
                    input_events=list(events),
                    task=task,
                    ordinary_interrupt_used=(
                        previous.ordinary_interrupt_used if continuation else False
                    ),
                )
                if continuation and previous:
                    cycle.ordinary_bucket.extend(previous.ordinary_bucket)
                if previous is not None:
                    # 旧周期里已登记、但没进入本轮输入的优先事件（打断与登记之间到达的 @）
                    # 不能随旧周期一起消失：它们已经入库，必须转成寄存事件再折进下一轮。
                    input_keys = {event.unique_key for event in cycle.input_events}
                    leftover = [
                        event
                        for event in previous.priority_events
                        if event.unique_key not in input_keys
                    ]
                    if leftover:
                        state.directed_pending.extend(leftover)
                state.generation = cycle
                state.directed_handoff = False
                self._online_marks(runtime_key, state)
                # Nora 自己开始生成也算一次互动，避免生成期间被看门狗掐掉。
                state.last_interaction_at = time.time()
                state.consecutive_keep_listening = 0
                state.consecutive_semi_online_votes = 0
                self._cancel_task(state.idle_task)
                state.idle_task = None
                return cycle.token

    async def flush_directed_pending(
        self,
        runtime_key: str,
        token: int,
    ) -> List[GroupMessageEvent]:
        """Fold @-events that arrived during promotion into the registered cycle.

        返回非空列表表示需要按 mention 优先级重启本周期：这些消息已经入库，
        必须进入同一轮输入，否则并发两轮会各自读到对方的消息而认错说话人。
        """
        state = self._state(runtime_key)
        async with state.lock:
            pending = state.directed_pending
            if not pending:
                return []
            generation = state.generation
            if generation is None or generation.token != token:
                for event in pending:
                    self._append_pending(state, event)
                state.directed_pending = []
                return []
            state.directed_pending = []
            generation.priority_events.extend(pending)
            generation.priority_handoff = True
            generation.restart_pending = True
            return self._merge_events(generation.input_events, generation.priority_events)

    async def bind_generation_task(self, runtime_key: str, token: int, task: asyncio.Task) -> bool:
        state = self._state(runtime_key)
        async with state.lock:
            if not state.generation or state.generation.token != token:
                return False
            state.generation.task = task
            return True

    async def is_generation_current(self, runtime_key: str, token: int) -> bool:
        state = self._state(runtime_key)
        async with state.lock:
            return bool(state.generation and state.generation.token == token)

    async def finish_generation(self, runtime_key: str, token: int) -> bool:
        state = self._state(runtime_key)
        run_passive = False
        async with state.lock:
            generation = state.generation
            if not generation or generation.token != token:
                return False
            for event in generation.ordinary_bucket:
                self._append_pending(state, event)
            state.generation = None
            if state.pending_window:
                run_passive = state.new_since_passive_eval >= self.batch_size
            # 窗口为空时同样重装 idle 定时器：这条路径（回复完 → 群安静）此前不装表，
            # 于是再也没有任何本地检查会跑到，只能等看门狗。
            self._reset_idle_timer(runtime_key, state)
        if run_passive:
            self._ensure_passive_task(runtime_key, trigger="post_generation")
        return True

    async def priority_restart_events(
        self,
        runtime_key: str,
        token: int,
        replacement_events: Optional[Sequence[GroupMessageEvent]] = None,
    ) -> List[GroupMessageEvent]:
        state = self._state(runtime_key)
        async with state.lock:
            generation = state.generation
            if not generation or generation.token != token:
                return []
            base = list(replacement_events or generation.input_events)
            return self._merge_events(base, generation.priority_events)

    async def mark_restart_started(
        self,
        runtime_key: str,
        old_token: int,
        new_token: int,
    ) -> bool:
        """Confirm that a replacement cycle superseded the expected old token."""
        state = self._state(runtime_key)
        async with state.lock:
            generation = state.generation
            if not generation or generation.token != new_token:
                return False
            generation.restart_pending = False
            generation.priority_handoff = False
            generation.priority_events.clear()
            return old_token != new_token

    def pending_events(self, runtime_key: str) -> List[GroupMessageEvent]:
        return list(self._state(runtime_key).pending_window)

    def listening_stats(self, runtime_key: str) -> Dict[str, Any]:
        """Timing signals for the fast classifier prompt.

        分类器此前只拿到窗口计数，无从知道这个群已经开着多久、连续多少轮没插话，
        所以「主动性不够」并非模型判断力问题，而是缺少判断依据。
        """
        state = self._state(runtime_key)
        now = time.time()
        online_since = state.online_since
        if not online_since:
            getter = getattr(self.store, "updated_at", None)
            if callable(getter):
                try:
                    online_since = float(getter(runtime_key) or 0.0)
                except Exception:
                    online_since = 0.0
        last_interaction_at = state.last_interaction_at or online_since
        last_activity = state.last_received_at or online_since
        return {
            "online_seconds": max(0.0, now - online_since) if online_since else 0.0,
            "trigger_kind": str(state.online_trigger_kind or "interaction"),
            "seconds_since_interaction": (
                max(0.0, now - last_interaction_at) if last_interaction_at else 0.0
            ),
            "seconds_since_message": max(0.0, now - last_activity) if last_activity else 0.0,
            "consecutive_keep_listening": int(state.consecutive_keep_listening),
            "consecutive_semi_online_votes": int(state.consecutive_semi_online_votes),
            "no_interaction_timeout_seconds": float(self.no_interaction_timeout_seconds),
            "idle_exit_seconds": float(self.idle_exit_seconds),
        }

    async def shutdown(self) -> None:
        self._closed = True
        tasks: List[asyncio.Task] = []
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            tasks.append(self._watchdog_task)
        self._watchdog_task = None
        for state in self._states.values():
            for task in (state.idle_task, state.passive_task):
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _state(self, runtime_key: str) -> GroupRuntimeState:
        return self._states.setdefault(runtime_key, GroupRuntimeState())

    def _append_pending(self, state: GroupRuntimeState, event: GroupMessageEvent) -> None:
        state.pending_window.append(event)
        while len(state.pending_window) > self.max_pending:
            state.pending_window.popleft()
        state.new_since_passive_eval += 1

    @staticmethod
    def _take_recent_pending(
        state: GroupRuntimeState,
        *,
        limit: int,
    ) -> List[GroupMessageEvent]:
        """Remove and return recent pending events consumed as directed context."""
        count = min(max(0, int(limit)), len(state.pending_window))
        if not count:
            return []
        events = list(state.pending_window)[-count:]
        for _ in range(count):
            state.pending_window.pop()
        state.new_since_passive_eval = min(
            max(0, state.new_since_passive_eval - count),
            len(state.pending_window),
        )
        return events

    def _reset_idle_timer(self, runtime_key: str, state: GroupRuntimeState) -> None:
        state.idle_generation += 1
        generation = state.idle_generation
        self._cancel_task(state.idle_task)
        state.idle_task = asyncio.create_task(self._idle_wait(runtime_key, generation))

    async def _idle_wait(self, runtime_key: str, generation: int) -> None:
        try:
            await asyncio.sleep(self.idle_seconds)
            state = self._state(runtime_key)
            demote_reason = ""
            async with state.lock:
                if (
                    generation != state.idle_generation
                    or state.generation
                    or state.directed_handoff
                ):
                    return
                if not state.pending_window:
                    # 窗口为空此前直接 return：这正是「被 @ → 回复 → 群彻底安静」
                    # 之后再也不会被评估、ONLINE 永久挂着的原因。改为按硬超时判定降级。
                    demote_reason = self._demote_reason(runtime_key, state, time.time())
                    if not demote_reason:
                        return
                    # 摘掉自身引用，避免 set_mode 里的 _cancel_task 取消当前任务。
                    if state.idle_task is asyncio.current_task():
                        state.idle_task = None
            if demote_reason:
                if self.mode_for(runtime_key) != GroupPresence.ONLINE:
                    return
                logger.info(
                    "[%s] 群监听空窗口静默降级: reason=%s idle_seconds=%.0f",
                    runtime_key,
                    demote_reason,
                    self.idle_seconds,
                )
                await self.set_mode(runtime_key, GroupPresence.SEMI_ONLINE, reason=demote_reason)
                return
            self._ensure_passive_task(runtime_key, trigger="idle")
        except asyncio.CancelledError:
            return

    def _ensure_passive_task(self, runtime_key: str, *, trigger: str) -> None:
        state = self._state(runtime_key)
        if state.passive_task and not state.passive_task.done():
            return
        logger.info(
            "[%s] 群监听 fast 判断开始: trigger=%s window=%d",
            runtime_key,
            trigger,
            len(state.pending_window),
        )
        state.passive_task = asyncio.create_task(self._run_passive_decision(runtime_key, trigger))

    async def _run_passive_decision(self, runtime_key: str, trigger: str) -> None:
        state = self._state(runtime_key)
        snapshot: List[GroupMessageEvent] = []
        snapshot_last_sequence = 0
        force_rerun = False
        try:
            async with state.lock:
                if state.generation or state.directed_handoff or not state.pending_window:
                    return
                snapshot = list(state.pending_window)
                snapshot_last_sequence = snapshot[-1].sequence
                state.new_since_passive_eval = 0
                state.last_passive_at = time.time()
            try:
                action = PassiveAction(await self.decide_passive(runtime_key, snapshot))
            except Exception:
                logger.warning("[%s] 群监听 fast 判断失败，保留窗口", runtime_key, exc_info=True)
                action = PassiveAction.KEEP_LISTENING

            now = time.time()
            if action == PassiveAction.SEMI_ONLINE and any(
                self._event_vetoes_semi_online(event, now) for event in snapshot
            ):
                logger.warning(
                    "[%s] 群监听拒绝近期定向窗口的 SEMI_ONLINE 决策: window=%d veto_window=%.0fs",
                    runtime_key,
                    len(snapshot),
                    self.directed_veto_seconds,
                )
                action = PassiveAction.KEEP_LISTENING

            promote_events: List[GroupMessageEvent] = []
            semi_online_candidate = False
            semi_online_expected: Optional[int] = None
            async with state.lock:
                prefix = [event for event in state.pending_window if event.sequence <= snapshot_last_sequence]
                suffix = [event for event in state.pending_window if event.sequence > snapshot_last_sequence]
                if action == PassiveAction.REPLY:
                    promote_events = prefix
                    state.pending_window = deque(suffix)
                    state.consecutive_keep_listening = 0
                    state.consecutive_semi_online_votes = 0
                elif action == PassiveAction.SEMI_ONLINE:
                    state.consecutive_keep_listening = 0
                    state.consecutive_semi_online_votes += 1
                    if not prefix:
                        pass
                    elif not suffix:
                        semi_online_candidate = True
                        semi_online_expected = snapshot_last_sequence
                    elif state.consecutive_semi_online_votes >= self.semi_online_vote_threshold:
                        # 并发后缀此前会让 SEMI_ONLINE 结论静默作废；活跃群里这是常态，
                        # 于是正确的降级判断被反复丢掉。连续多轮都投 SEMI_ONLINE 时
                        # 直接按当前窗口末尾降级，后缀随 set_mode 一起清空。
                        semi_online_candidate = True
                        semi_online_expected = state.pending_window[-1].sequence
                    else:
                        force_rerun = True
                else:
                    state.consecutive_keep_listening += 1
                    state.consecutive_semi_online_votes = 0
                if state.pending_window and not semi_online_candidate:
                    self._reset_idle_timer(runtime_key, state)
                keep_streak = state.consecutive_keep_listening
                semi_votes = state.consecutive_semi_online_votes

            logger.info(
                "[%s] 群监听 fast=%s trigger=%s window=%d concurrent_suffix=%d "
                "keep_streak=%d semi_votes=%d",
                runtime_key,
                action.value,
                trigger,
                len(snapshot),
                len(suffix),
                keep_streak,
                semi_votes,
            )
            if semi_online_candidate:
                first = snapshot[0]
                changed = await self.set_mode(
                    runtime_key,
                    GroupPresence.SEMI_ONLINE,
                    platform=first.platform,
                    platform_chat_id=first.platform_chat_id,
                    reason="fast_passive_semi_online",
                    expected_last_sequence=semi_online_expected,
                )
                if not changed:
                    logger.info(
                        "[%s] 群监听忽略过期 SEMI_ONLINE 决策，新消息或生成已出现（保留投票计数）",
                        runtime_key,
                    )
                    async with state.lock:
                        if state.pending_window:
                            self._reset_idle_timer(runtime_key, state)
            elif promote_events:
                await self.promote(runtime_key, promote_events, "passive_reply")
        finally:
            async with state.lock:
                if state.passive_task is asyncio.current_task():
                    state.passive_task = None
                rerun = bool(
                    not state.generation
                    and state.pending_window
                    and (force_rerun or state.new_since_passive_eval >= self.batch_size)
                )
            if rerun:
                self._ensure_passive_task(runtime_key, trigger="queued")

    async def _run_append_decision(
        self,
        runtime_key: str,
        base: Sequence[GroupMessageEvent],
        batch: Sequence[GroupMessageEvent],
        token: int,
    ) -> None:
        try:
            action = AppendAction(await self.decide_append(runtime_key, base, batch))
        except Exception:
            logger.warning("[%s] 群生成追加 fast 判断失败，消息转入 pending", runtime_key, exc_info=True)
            action = AppendAction.KEEP_PENDING

        state = self._state(runtime_key)
        interrupt_events: Optional[List[GroupMessageEvent]] = None
        async with state.lock:
            generation = state.generation
            if not generation or generation.token != token:
                for event in batch:
                    self._append_pending(state, event)
                return
            if action == AppendAction.APPEND and not generation.ordinary_interrupt_used:
                generation.ordinary_interrupt_used = True
                interrupt_events = self._merge_events(generation.input_events, batch)
            else:
                for event in batch:
                    self._append_pending(state, event)

        logger.info(
            "[%s] 群生成追加 fast=%s batch=%d token=%d interrupt=%s",
            runtime_key,
            action.value,
            len(batch),
            token,
            bool(interrupt_events),
        )
        if interrupt_events:
            await self.interrupt(runtime_key, interrupt_events, "ordinary_append", token)

    @staticmethod
    def _merge_events(*groups: Iterable[GroupMessageEvent]) -> List[GroupMessageEvent]:
        merged: Dict[Tuple[str, str, str], GroupMessageEvent] = {}
        for group in groups:
            for event in group:
                merged.setdefault(event.unique_key, event)
        return sorted(merged.values(), key=lambda event: event.sequence)

    def _clear_runtime_state(self, state: GroupRuntimeState, *, cancel_passive: bool) -> None:
        state.pending_window.clear()
        state.new_since_passive_eval = 0
        state.idle_generation += 1
        self._cancel_task(state.idle_task)
        state.idle_task = None
        if cancel_passive:
            self._cancel_task(state.passive_task)
            state.passive_task = None
        if state.generation and state.generation.task:
            self._cancel_task(state.generation.task)
        state.generation = None
        state.directed_handoff = False
        state.directed_pending = []

    @staticmethod
    def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()
