import asyncio

from core import group_presence_store
from core.group_listener import AppendAction, GroupListenerManager, PassiveAction
from core.group_presence_store import GroupPresence, GroupPresenceStore
from core.message_handler import _update_message_entry_presence
from core.scheduler import AIPresence, get_ai_presence, set_ai_presence
from core.private_presence_store import PrivatePresence, PrivatePresenceStore


def test_group_message_entry_keeps_global_presence_semi_online():
    """群聊消息入口不改变全局 AIPresence（仍保持 SEMI_ONLINE）。"""
    previous = get_ai_presence()
    set_ai_presence(AIPresence.SEMI_ONLINE, reason="test_setup")
    try:
        state = _update_message_entry_presence("telegram:group-1", "group")

        assert state["presence"] == AIPresence.SEMI_ONLINE.value
        assert get_ai_presence() == AIPresence.SEMI_ONLINE
    finally:
        set_ai_presence(previous, reason="test_cleanup")


def test_private_message_entry_writes_private_presence(tmp_path, monkeypatch):
    """私聊消息入口应写入 PrivatePresenceStore（而不是全局 AIPresence）。"""
    import core.private_presence_store as pps_module

    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store = PrivatePresenceStore()
    assert store.get("scope:owner") == PrivatePresence.SEMI_ONLINE

    state = _update_message_entry_presence(
        "telegram:user-1",
        "private",
        private_presence=store,
        memory_scope_id="scope:owner",
    )

    assert store.get("scope:owner") == PrivatePresence.ONLINE
    # 全局 AIPresence 不应被改变
    assert state["presence"] == get_ai_presence().value


def test_group_presence_log_has_scope_reason_and_ignores_duplicate_info(tmp_path, monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(
            group_presence_store,
            "get_workspace_manager",
            lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
        )
        store = GroupPresenceStore()

        async def persist(context):
            return None

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
        )
        try:
            with caplog.at_level("INFO", logger="core.group_listener"):
                await manager.set_mode(
                    "onebotv11:10001",
                    GroupPresence.ONLINE,
                    reason="front_marker_online",
                )
                await manager.set_mode(
                    "onebotv11:10001",
                    GroupPresence.ONLINE,
                    reason="front_marker_online",
                )
        finally:
            await manager.shutdown()

    asyncio.run(run())

    matching = [
        record.message
        for record in caplog.records
        if "[GROUP PRESENCE]" in record.message
    ]
    assert len(matching) == 1
    assert "runtime=onebotv11:10001" in matching[0]
    assert "semi_online -> online" in matching[0]
    assert "reason=front_marker_online" in matching[0]
