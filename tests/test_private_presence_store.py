"""Tests for core/private_presence_store.py."""
from core import private_presence_store as pps_module
from core.private_presence_store import PrivatePresence, PrivatePresenceStore


def test_default_is_semi_online(monkeypatch, tmp_path):
    """未设置过的 scope 默认返回 SEMI_ONLINE。"""
    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store = PrivatePresenceStore()
    assert store.get("unknown-scope") == PrivatePresence.SEMI_ONLINE


def test_set_and_get_roundtrip(monkeypatch, tmp_path):
    """set 后 get 返回一致的值。"""
    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store = PrivatePresenceStore()
    store.set("scope:owner", PrivatePresence.ONLINE, reason="test")
    assert store.get("scope:owner") == PrivatePresence.ONLINE


def test_persists_to_json(monkeypatch, tmp_path):
    """落盘后重新创建 store 实例仍能读到之前的值。"""
    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store1 = PrivatePresenceStore()
    store1.set("scope:owner", PrivatePresence.ONLINE, reason="test_persist")

    # 新实例应能读到
    store2 = PrivatePresenceStore()
    assert store2.get("scope:owner") == PrivatePresence.ONLINE


def test_redundant_set_no_log(monkeypatch, tmp_path, caplog):
    """重复设置同一 mode 不产生日志。"""
    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store = PrivatePresenceStore()
    store.set("scope:owner", PrivatePresence.ONLINE, reason="first")

    with caplog.at_level("INFO", logger="core.private_presence_store"):
        store.set("scope:owner", PrivatePresence.ONLINE, reason="redundant")

    matching = [
        r for r in caplog.records
        if "[PRIVATE PRESENCE]" in r.message and "redundant" in r.message
    ]
    assert len(matching) == 0


def test_all_returns_all_scopes(monkeypatch, tmp_path):
    """all() 返回所有已设置的 scope。"""
    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store = PrivatePresenceStore()
    store.set("scope:a", PrivatePresence.ONLINE, reason="test")
    store.set("scope:b", PrivatePresence.ONLINE, reason="test")

    result = store.all()
    assert result["scope:a"] == PrivatePresence.ONLINE
    assert result["scope:b"] == PrivatePresence.ONLINE


def test_expire_stale_entries(monkeypatch, tmp_path):
    """启动过期清理：超过 6 小时的 ONLINE 记录重置为 SEMI_ONLINE。"""
    import time

    monkeypatch.setattr(
        pps_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )
    store = PrivatePresenceStore()

    # 手动写入一个过期的 ONLINE 记录
    import json, os
    state_path = os.path.join(str(tmp_path), "private_presence_state.json")
    old_time = time.time() - 7 * 3600  # 7 小时前
    data = {
        "version": 1,
        "scopes": {
            "scope:stale": {
                "mode": "online",
                "reason": "old",
                "updated_at": old_time,
            },
            "scope:fresh": {
                "mode": "online",
                "reason": "recent",
                "updated_at": time.time(),
            },
        },
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    expired = store.expire_stale_entries()
    assert expired == 1
    assert store.get("scope:stale") == PrivatePresence.SEMI_ONLINE
    assert store.get("scope:fresh") == PrivatePresence.ONLINE
