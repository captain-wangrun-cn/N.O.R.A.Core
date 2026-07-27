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
    idle_generation: int = 0
    idle_task: Optional[asyncio.Task] = None
    passive_task: Optional[asyncio.Task] = None
    directed_handoff: bool = False
    generation: Optional[GenerationCycle] = None


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
        self._states: Dict[str, GroupRuntimeState] = {}
        self._sequence = 0
        self._generation_token = 0
        self._closed = False

    def mode_for(self, runtime_key: str) -> GroupPresence:
        if not self.enabled:
            return GroupPresence.SEMI_ONLINE
        return self.store.get(runtime_key)

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
        """Set one group's mode, optionally only if no newer event has arrived."""
        mode = GroupPresence(mode)
        state = self._state(runtime_key)
        async with state.lock:
            if expected_last_sequence is not None:
                if state.generation or state.directed_handoff or not state.pending_window:
                    return False
                if any(
                    event.sequence > expected_last_sequence
                    or event.explicit_bot_mention
                    or event.reply_to_bot
                    for event in state.pending_window
                ):
                    return False
            old_mode = self.mode_for(runtime_key)
            self.store.set(
                runtime_key,
                mode,
                platform=platform,
                platform_chat_id=platform_chat_id,
            )
            if mode == GroupPresence.SEMI_ONLINE:
                state.pending_window.clear()
                state.new_since_passive_eval = 0
                state.idle_generation += 1
                self._cancel_task(state.idle_task)
                state.idle_task = None
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
        self._sequence += 1
        event = GroupMessageEvent.from_context(context, self._sequence)
        state = self._state(runtime_key)

        interrupt_payload: Optional[Tuple[List[GroupMessageEvent], str, int]] = None
        append_payload: Optional[Tuple[List[GroupMessageEvent], List[GroupMessageEvent], int]] = None
        direct_promote: Optional[List[GroupMessageEvent]] = None
        run_passive = False
        async with state.lock:
            state.last_received_at = event.received_at
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
                direct_promote = self._merge_events(
                    recent_context,
                    [reply_target] if reply_target is not None else [],
                    [event],
                )
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
        if run_passive:
            self._ensure_passive_task(runtime_key, trigger="count")
        return True

    async def begin_generation(
        self,
        runtime_key: str,
        events: Sequence[GroupMessageEvent],
        task: Optional[asyncio.Task] = None,
        *,
        continuation_token: Optional[int] = None,
    ) -> int:
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
            state.generation = cycle
            state.directed_handoff = False
            self._cancel_task(state.idle_task)
            state.idle_task = None
            return cycle.token

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

    async def shutdown(self) -> None:
        self._closed = True
        tasks: List[asyncio.Task] = []
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
            async with state.lock:
                if (
                    generation != state.idle_generation
                    or state.generation
                    or state.directed_handoff
                    or not state.pending_window
                ):
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
        try:
            async with state.lock:
                if state.generation or state.directed_handoff or not state.pending_window:
                    return
                snapshot = list(state.pending_window)
                snapshot_last_sequence = snapshot[-1].sequence
                state.new_since_passive_eval = 0
            try:
                action = PassiveAction(await self.decide_passive(runtime_key, snapshot))
            except Exception:
                logger.warning("[%s] 群监听 fast 判断失败，保留窗口", runtime_key, exc_info=True)
                action = PassiveAction.KEEP_LISTENING

            if action == PassiveAction.SEMI_ONLINE and any(
                event.explicit_bot_mention or event.reply_to_bot for event in snapshot
            ):
                logger.warning(
                    "[%s] 群监听拒绝定向窗口的 SEMI_ONLINE 决策: window=%d",
                    runtime_key,
                    len(snapshot),
                )
                action = PassiveAction.KEEP_LISTENING

            promote_events: List[GroupMessageEvent] = []
            semi_online_candidate = False
            async with state.lock:
                prefix = [event for event in state.pending_window if event.sequence <= snapshot_last_sequence]
                suffix = [event for event in state.pending_window if event.sequence > snapshot_last_sequence]
                if action == PassiveAction.REPLY:
                    promote_events = prefix
                    state.pending_window = deque(suffix)
                elif action == PassiveAction.SEMI_ONLINE:
                    semi_online_candidate = bool(prefix) and not suffix
                if state.pending_window and not semi_online_candidate:
                    self._reset_idle_timer(runtime_key, state)

            logger.info(
                "[%s] 群监听 fast=%s trigger=%s window=%d concurrent_suffix=%d",
                runtime_key,
                action.value,
                trigger,
                len(snapshot),
                len(suffix),
            )
            if semi_online_candidate:
                first = snapshot[0]
                changed = await self.set_mode(
                    runtime_key,
                    GroupPresence.SEMI_ONLINE,
                    platform=first.platform,
                    platform_chat_id=first.platform_chat_id,
                    reason="fast_passive_semi_online",
                    expected_last_sequence=snapshot_last_sequence,
                )
                if not changed:
                    logger.info(
                        "[%s] 群监听忽略过期 SEMI_ONLINE 决策，新消息或生成已出现",
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
                    and state.new_since_passive_eval >= self.batch_size
                    and state.pending_window
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

    @staticmethod
    def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()
