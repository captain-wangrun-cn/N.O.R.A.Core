import asyncio
import json
from types import SimpleNamespace

import pytest

from core import group_presence_store
from core.group_listener import (
    AppendAction,
    GroupListenerManager,
    GroupMessageEvent,
    PassiveAction,
)
from core.group_presence_store import GroupPresence, GroupPresenceStore


def _patch_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(
        group_presence_store,
        "get_workspace_manager",
        lambda: SimpleNamespace(data_dir=str(tmp_path)),
    )


def _context(index: int, *, runtime_key: str = "telegram:g1", mention: bool = False):
    platform, chat_id = runtime_key.split(":", 1)
    return {
        "runtime_key": runtime_key,
        "memory_scope_id": "relationship:owner:default",
        "place_scope_id": f"place:{runtime_key}",
        "platform": platform,
        "chat_id": chat_id,
        "chat_type": "group",
        "user_id": f"u{index}",
        "user_name": f"User {index}",
        "text": f"message {index}",
        "platform_message_id": str(index),
        "explicit_bot_mention": mention,
    }


def _event(index: int, **kwargs):
    return GroupMessageEvent.from_context(_context(index, **kwargs), index)


def test_group_presence_store_defaults_and_persists_per_group(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()

        assert store.get("telegram:g1") == GroupPresence.SEMI_ONLINE
        assert store.set("telegram:g1", GroupPresence.ONLINE, platform="telegram", platform_chat_id="g1")
        assert store.get("telegram:g1") == GroupPresence.ONLINE
        assert store.get("telegram:g2") == GroupPresence.SEMI_ONLINE

        restored = GroupPresenceStore()
        assert restored.get("telegram:g1") == GroupPresence.ONLINE



    asyncio.run(run())


def test_shutdown_cancels_idle_timer_and_rejects_new_events(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)

        async def persist(context):
            context["_message_saved"] = True

        async def decide_passive(runtime_key, events):
            return PassiveAction.KEEP_LISTENING

        async def decide_append(runtime_key, base, batch):
            return AppendAction.KEEP_PENDING

        async def promote(runtime_key, events, reason):
            return None

        async def interrupt(runtime_key, events, reason, token):
            return None

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=promote,
            interrupt=interrupt,
            idle_seconds=30,
        )
        assert await manager.receive(_context(1)) is True
        idle_task = manager._state("telegram:g1").idle_task
        assert idle_task is not None and not idle_task.done()

        await manager.shutdown()

        assert idle_task.cancelled()
        assert await manager.receive(_context(2)) is False

    asyncio.run(run())


def test_group_presence_store_recovers_from_corrupt_state(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        (tmp_path / "group_presence_state.json").write_text("not-json", encoding="utf-8")

        store = GroupPresenceStore()
        assert store.get("telegram:g1") == GroupPresence.SEMI_ONLINE
        assert store.set("telegram:g1", GroupPresence.ONLINE)
        state = json.loads((tmp_path / "group_presence_state.json").read_text(encoding="utf-8"))
        assert state["groups"]["telegram:g1"]["mode"] == "online"



    asyncio.run(run())
def test_second_message_triggers_full_retained_window(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        persisted = []
        evaluated = []
        promoted = []
        decisions = iter([PassiveAction.KEEP_LISTENING, PassiveAction.REPLY])

        async def persist(context):
            context["_message_saved"] = True
            persisted.append(context["platform_message_id"])

        async def decide_passive(runtime_key, events):
            evaluated.append([event.platform_message_id for event in events])
            return next(decisions)

        async def decide_append(runtime_key, base, batch):
            return AppendAction.KEEP_PENDING

        async def promote(runtime_key, events, reason):
            promoted.append(([event.platform_message_id for event in events], reason))

        async def interrupt(runtime_key, events, reason, token):
            raise AssertionError("unexpected interrupt")

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=promote,
            interrupt=interrupt,
            idle_seconds=30,
        )
        try:
            for index in range(1, 3):
                assert await manager.receive(_context(index))
            await asyncio.sleep(0.02)
            assert evaluated == [["1", "2"]]
            assert [event.platform_message_id for event in manager.pending_events("telegram:g1")] == ["1", "2"]

            for index in range(3, 5):
                await manager.receive(_context(index))
            await asyncio.sleep(0.02)
            assert evaluated[-1] == ["1", "2", "3", "4"]
            assert promoted == [(["1", "2", "3", "4"], "passive_reply")]
            assert persisted == ["1", "2", "3", "4"]
        finally:
            await manager.shutdown()



    asyncio.run(run())
def test_pending_window_is_bounded_without_affecting_persistence(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        persisted = []

        async def persist(context):
            persisted.append(context["platform_message_id"])

        async def decide_passive(runtime_key, events):
            return PassiveAction.KEEP_LISTENING

        async def decide_append(runtime_key, base, batch):
            return AppendAction.KEEP_PENDING

        async def noop(*args):
            return None

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=noop,
            interrupt=noop,
            batch_size=100,
            max_pending=20,
            idle_seconds=30,
        )
        try:
            for index in range(1, 26):
                await manager.receive(_context(index))
            assert len(persisted) == 25
            assert [event.platform_message_id for event in manager.pending_events("telegram:g1")] == [
                str(index) for index in range(6, 26)
            ]
        finally:
            await manager.shutdown()



    asyncio.run(run())
def test_idle_timer_evaluates_non_empty_window(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        evaluated = asyncio.Event()

        async def persist(context):
            return None

        async def decide_passive(runtime_key, events):
            assert [event.platform_message_id for event in events] == ["1"]
            evaluated.set()
            return PassiveAction.KEEP_LISTENING

        async def decide_append(runtime_key, base, batch):
            return AppendAction.KEEP_PENDING

        async def noop(*args):
            return None

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=noop,
            interrupt=noop,
            idle_seconds=0.02,
        )
        try:
            await manager.receive(_context(1))
            await asyncio.wait_for(evaluated.wait(), timeout=0.3)
        finally:
            await manager.shutdown()



    asyncio.run(run())
def test_semi_online_action_only_clears_current_group(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        store.set("telegram:g2", GroupPresence.ONLINE)

        async def persist(context):
            return None

        async def decide_passive(runtime_key, events):
            return PassiveAction.SEMI_ONLINE

        async def decide_append(runtime_key, base, batch):
            return AppendAction.KEEP_PENDING

        async def noop(*args):
            return None

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=noop,
            interrupt=noop,
            idle_seconds=30,
        )
        try:
            await manager.receive(_context(1, runtime_key="telegram:g2"))
            for index in range(1, 3):
                await manager.receive(_context(index, runtime_key="telegram:g1"))
            await asyncio.sleep(0.02)
            assert store.get("telegram:g1") == GroupPresence.SEMI_ONLINE
            assert store.get("telegram:g2") == GroupPresence.ONLINE
            assert manager.pending_events("telegram:g1") == []
            assert [event.platform_message_id for event in manager.pending_events("telegram:g2")] == ["1"]
        finally:
            await manager.shutdown()



    asyncio.run(run())
def test_generation_finish_releases_pending_messages(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        evaluated = []

        async def decide_passive(runtime_key, events):
            evaluated.append([event.platform_message_id for event in events])
            return PassiveAction.KEEP_LISTENING

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=decide_passive,
            decide_append=lambda *args: asyncio.sleep(0, result=AppendAction.KEEP_PENDING),
            promote=lambda *args: asyncio.sleep(0),
            interrupt=lambda *args: asyncio.sleep(0),
            idle_seconds=30,
        )
        try:
            token = await manager.begin_generation("telegram:g1", [_event(10)])
            for index in range(1, 3):
                await manager.receive(_context(index))
            await asyncio.sleep(0.02)
            assert evaluated == []

            assert await manager.finish_generation("telegram:g1", token)
            await asyncio.sleep(0.02)
            assert evaluated == [["1", "2"]]
            assert not await manager.finish_generation("telegram:g1", token)
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_restart_preserves_ordinary_interrupt_and_handoff_events(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=lambda *args: asyncio.sleep(0, result=PassiveAction.KEEP_LISTENING),
            decide_append=lambda *args: asyncio.sleep(0, result=AppendAction.APPEND),
            promote=lambda *args: asyncio.sleep(0),
            interrupt=lambda *args: asyncio.sleep(0),
            idle_seconds=30,
        )
        try:
            old_token = await manager.begin_generation("telegram:g1", [_event(10)])
            for index in range(1, 3):
                await manager.receive(_context(index))
            await asyncio.sleep(0.02)

            state = manager._state("telegram:g1")
            assert state.generation.ordinary_interrupt_used is True
            state.generation.priority_handoff = True
            state.generation.restart_pending = True
            await manager.receive(_context(3))

            restart_events = await manager.priority_restart_events(
                "telegram:g1", old_token, replacement_events=[_event(10), _event(1)]
            )
            assert [event.platform_message_id for event in restart_events] == ["1", "3", "10"]

            new_token = await manager.begin_generation(
                "telegram:g1", restart_events, continuation_token=old_token
            )
            assert await manager.mark_restart_started("telegram:g1", old_token, new_token)
            assert state.generation.ordinary_interrupt_used is True
            assert state.generation.priority_handoff is False
            assert state.generation.restart_pending is False
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_generation_append_interrupts_once_then_keeps_pending(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        interrupts = []

        async def persist(context):
            return None

        async def decide_passive(runtime_key, events):
            return PassiveAction.KEEP_LISTENING

        async def decide_append(runtime_key, base, batch):
            return AppendAction.APPEND

        async def promote(*args):
            return None

        async def interrupt(runtime_key, events, reason, token):
            interrupts.append(([event.platform_message_id for event in events], reason, token))

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=promote,
            interrupt=interrupt,
            idle_seconds=30,
        )
        try:
            token = await manager.begin_generation("telegram:g1", [_event(10)])
            for index in range(1, 3):
                await manager.receive(_context(index))
            await asyncio.sleep(0.02)
            assert interrupts == [(["1", "2", "10"], "ordinary_append", token)]

            for index in range(3, 5):
                await manager.receive(_context(index))
            assert [event.platform_message_id for event in manager.pending_events("telegram:g1")] == ["3", "4"]
        finally:
            await manager.shutdown()



    asyncio.run(run())
def test_priority_handoff_coalesces_following_messages(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        started = asyncio.Event()
        release = asyncio.Event()

        async def persist(context):
            return None

        async def decide_passive(runtime_key, events):
            return PassiveAction.KEEP_LISTENING

        async def decide_append(runtime_key, base, batch):
            return AppendAction.KEEP_PENDING

        async def promote(*args):
            return None

        async def interrupt(runtime_key, events, reason, token):
            started.set()
            await release.wait()

        manager = GroupListenerManager(
            store=store,
            persist_message=persist,
            decide_passive=decide_passive,
            decide_append=decide_append,
            promote=promote,
            interrupt=interrupt,
            idle_seconds=30,
        )
        try:
            token = await manager.begin_generation("telegram:g1", [_event(10)])
            await manager.receive(_context(1, mention=True))
            await asyncio.wait_for(started.wait(), timeout=0.3)
            await manager.receive(_context(2))
            await manager.receive(_context(3, mention=True))

            events = await manager.priority_restart_events("telegram:g1", token)
            assert [event.platform_message_id for event in events] == ["1", "2", "3", "10"]
            release.set()
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_idle_directed_messages_promote_without_passive_fast(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        passive_calls = []
        promoted = []

        async def decide_passive(runtime_key, events):
            passive_calls.append(list(events))
            return PassiveAction.KEEP_LISTENING

        async def promote(runtime_key, events, reason):
            promoted.append(([event.platform_message_id for event in events], reason))

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=decide_passive,
            decide_append=lambda *args: asyncio.sleep(0, result=AppendAction.KEEP_PENDING),
            promote=promote,
            interrupt=lambda *args: asyncio.sleep(0),
            idle_seconds=30,
        )
        try:
            assert await manager.receive(_context(1, mention=True))
            reply = _context(2)
            reply["reply_to_bot"] = True
            assert await manager.receive(reply)

            assert passive_calls == []
            assert promoted == [(["1"], "directed_message"), (["2"], "directed_message")]
            assert manager.pending_events("telegram:g1") == []
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_reply_to_bot_during_generation_uses_ordinary_append(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        append_batches = []
        interrupts = []

        async def decide_append(runtime_key, base, batch):
            append_batches.append([event.platform_message_id for event in batch])
            return AppendAction.KEEP_PENDING

        async def interrupt(runtime_key, events, reason, token):
            interrupts.append(reason)

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=lambda *args: asyncio.sleep(0, result=PassiveAction.KEEP_LISTENING),
            decide_append=decide_append,
            promote=lambda *args: asyncio.sleep(0),
            interrupt=interrupt,
            idle_seconds=30,
        )
        try:
            await manager.begin_generation("telegram:g1", [_event(10)])
            for index in range(1, 3):
                context = _context(index)
                context["reply_to_bot"] = index == 1
                await manager.receive(context)
            await asyncio.sleep(0.02)

            assert append_batches == [["1", "2"]]
            assert interrupts == []
            assert [event.platform_message_id for event in manager.pending_events("telegram:g1")] == [
                "1", "2"
            ]
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_stale_semi_online_decision_preserves_concurrent_suffix(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        decision_started = asyncio.Event()
        release_decision = asyncio.Event()

        async def decide_passive(runtime_key, events):
            decision_started.set()
            await release_decision.wait()
            return PassiveAction.SEMI_ONLINE

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=decide_passive,
            decide_append=lambda *args: asyncio.sleep(0, result=AppendAction.KEEP_PENDING),
            promote=lambda *args: asyncio.sleep(0),
            interrupt=lambda *args: asyncio.sleep(0),
            idle_seconds=30,
        )
        try:
            for index in range(1, 3):
                await manager.receive(_context(index))
            await asyncio.wait_for(decision_started.wait(), timeout=0.3)
            await manager.receive(_context(3))
            release_decision.set()
            await asyncio.sleep(0.02)

            assert store.get("telegram:g1") == GroupPresence.ONLINE
            assert [event.platform_message_id for event in manager.pending_events("telegram:g1")] == [
                "1", "2", "3"
            ]
        finally:
            release_decision.set()
            await manager.shutdown()

    asyncio.run(run())


def test_directed_snapshot_cannot_switch_group_semi_online(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=lambda *args: asyncio.sleep(0, result=PassiveAction.SEMI_ONLINE),
            decide_append=lambda *args: asyncio.sleep(0, result=AppendAction.KEEP_PENDING),
            promote=lambda *args: asyncio.sleep(0),
            interrupt=lambda *args: asyncio.sleep(0),
            idle_seconds=30,
        )
        try:
            state = manager._state("telegram:g1")
            directed = _event(1, mention=True)
            async with state.lock:
                manager._append_pending(state, directed)
            await manager._run_passive_decision("telegram:g1", "test")

            assert store.get("telegram:g1") == GroupPresence.ONLINE
            assert [event.platform_message_id for event in manager.pending_events("telegram:g1")] == ["1"]
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_configured_batch_size_three_delays_passive_reply(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        evaluated = []
        promoted = []

        async def decide_passive(runtime_key, events):
            evaluated.append([event.platform_message_id for event in events])
            return PassiveAction.REPLY

        async def promote(runtime_key, events, reason):
            promoted.append(([event.platform_message_id for event in events], reason))

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=decide_passive,
            decide_append=lambda *args: asyncio.sleep(0, result=AppendAction.KEEP_PENDING),
            promote=promote,
            interrupt=lambda *args: asyncio.sleep(0),
            batch_size=3,
            idle_seconds=30,
        )
        try:
            await manager.receive(_context(1))
            await manager.receive(_context(2))
            await asyncio.sleep(0.02)
            assert evaluated == []

            await manager.receive(_context(3))
            await asyncio.sleep(0.02)
            assert evaluated == [["1", "2", "3"]]
            assert promoted == [(["1", "2", "3"], "passive_reply")]
            assert store.get("telegram:g1") == GroupPresence.ONLINE
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_configured_batch_size_three_controls_generation_append(tmp_path, monkeypatch):
    async def run():
        _patch_workspace(monkeypatch, tmp_path)
        store = GroupPresenceStore()
        store.set("telegram:g1", GroupPresence.ONLINE)
        batches = []

        async def decide_append(runtime_key, base, batch):
            batches.append([event.platform_message_id for event in batch])
            return AppendAction.KEEP_PENDING

        manager = GroupListenerManager(
            store=store,
            persist_message=lambda context: asyncio.sleep(0),
            decide_passive=lambda *args: asyncio.sleep(0, result=PassiveAction.KEEP_LISTENING),
            decide_append=decide_append,
            promote=lambda *args: asyncio.sleep(0),
            interrupt=lambda *args: asyncio.sleep(0),
            batch_size=3,
            idle_seconds=30,
        )
        try:
            await manager.begin_generation("telegram:g1", [_event(10)])
            await manager.receive(_context(1))
            await manager.receive(_context(2))
            await asyncio.sleep(0.02)
            assert batches == []

            await manager.receive(_context(3))
            await asyncio.sleep(0.02)
            assert batches == [["1", "2", "3"]]
        finally:
            await manager.shutdown()

    asyncio.run(run())
