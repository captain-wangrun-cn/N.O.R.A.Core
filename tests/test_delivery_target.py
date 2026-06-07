"""Phase 4 — 主动消息与最近活跃端测试。

覆盖:
  - delivery_target_store: 仅私聊端记录、群聊不更新、回退解析。
  - ProactiveScheduler._resolve_delivery_target: proactive 重定向到最近活跃端、
    alarm 不重定向、回退 default_chat_id。
  - TriggerManager._resolve_delivery_target: 显式 chat_id 优先、否则最近活跃端、回退 default。
"""

import asyncio
from types import SimpleNamespace

import pytest

from core import delivery_target_store
from core.conversation_identity import (
    build_conversation_identity,
    conversation_target_dict,
)
from core.scheduler import ProactiveScheduler, ScheduledEvent
from triggers.base import TriggerEvent
from triggers.manager import TriggerManager


# ----------------------------------------------------------------------
# delivery_target_store
# ----------------------------------------------------------------------

def _patch_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(
        delivery_target_store,
        "get_workspace_manager",
        lambda: SimpleNamespace(data_dir=str(tmp_path)),
    )


def _private_target(chat_id="123", platform="telegram", user_id="u-123"):
    identity = build_conversation_identity(
        {
            "platform": platform,
            "chat_id": chat_id,
            "user_id": user_id,
            "chat_type": "private",
        }
    )
    return conversation_target_dict(identity)


def _group_target(chat_id="-100", platform="telegram", user_id="u-999"):
    identity = build_conversation_identity(
        {
            "platform": platform,
            "chat_id": chat_id,
            "user_id": user_id,
            "chat_type": "group",
        }
    )
    return conversation_target_dict(identity)


def test_update_records_private_endpoint(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    assert delivery_target_store.update_last_active_target(_private_target()) is True

    target = delivery_target_store.load_last_active_target()
    assert target["last_active_runtime_key"] == "telegram:123"
    assert target["last_active_platform"] == "telegram"
    assert target["last_active_platform_chat_id"] == "123"
    assert "last_active_at" in target
    assert delivery_target_store.load_last_active_runtime_key() == "telegram:123"


def test_group_activity_does_not_update(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    # 先记录一个私聊端
    delivery_target_store.update_last_active_target(_private_target(chat_id="111"))
    # 群聊活跃不应改变投递目标
    assert delivery_target_store.update_last_active_target(_group_target()) is False
    assert delivery_target_store.load_last_active_runtime_key() == "telegram:111"


def test_last_active_overwrites_to_newest_private(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    delivery_target_store.update_last_active_target(_private_target(chat_id="111"))
    delivery_target_store.update_last_active_target(
        _private_target(chat_id="222", platform="web", user_id="u-222")
    )
    assert delivery_target_store.load_last_active_runtime_key() == "web:222"


def test_resolve_delivery_runtime_key_fallback(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    # 无记录 → 回退到 fallback
    assert delivery_target_store.resolve_delivery_runtime_key("telegram:fallback") == "telegram:fallback"
    # 有记录 → 用最近活跃端
    delivery_target_store.update_last_active_target(_private_target(chat_id="123"))
    assert delivery_target_store.resolve_delivery_runtime_key("telegram:fallback") == "telegram:123"


def test_load_empty_when_no_file(tmp_path, monkeypatch):
    _patch_workspace(monkeypatch, tmp_path)
    assert delivery_target_store.load_last_active_target() == {}
    assert delivery_target_store.load_last_active_runtime_key() == ""


# ----------------------------------------------------------------------
# ProactiveScheduler._resolve_delivery_target
# ----------------------------------------------------------------------

def _make_scheduler(tmp_path, resolver=None):
    return ProactiveScheduler(
        cache_dir=str(tmp_path / "cache"),
        timezone_str="Asia/Shanghai",
        resolve_delivery_callback=resolver,
    )


def test_scheduler_proactive_redirects_to_last_active(tmp_path):
    sched = _make_scheduler(tmp_path, resolver=lambda fb: "web:last-active")
    sched.default_chat_id = "telegram:default"
    ev = ScheduledEvent(trigger_time=None, event_type="proactive", chat_id="", event_id="ev1")
    assert sched._resolve_delivery_target(ev) == "web:last-active"


def test_scheduler_proactive_falls_back_when_resolver_empty(tmp_path):
    sched = _make_scheduler(tmp_path, resolver=lambda fb: "")
    sched.default_chat_id = "telegram:default"
    ev = ScheduledEvent(trigger_time=None, event_type="proactive", chat_id="", event_id="ev1")
    assert sched._resolve_delivery_target(ev) == "telegram:default"


def test_scheduler_alarm_never_redirects(tmp_path):
    sched = _make_scheduler(tmp_path, resolver=lambda fb: "web:last-active")
    sched.default_chat_id = "telegram:default"
    ev = ScheduledEvent(trigger_time=None, event_type="alarm", chat_id="telegram:alarm-target", event_id="ev2")
    # 闹钟保留显式目标，绝不重定向
    assert sched._resolve_delivery_target(ev) == "telegram:alarm-target"


def test_scheduler_no_resolver_uses_default(tmp_path):
    sched = _make_scheduler(tmp_path, resolver=None)
    sched.default_chat_id = "telegram:default"
    ev = ScheduledEvent(trigger_time=None, event_type="proactive", chat_id="", event_id="ev1")
    assert sched._resolve_delivery_target(ev) == "telegram:default"


# ----------------------------------------------------------------------
# TriggerManager._resolve_delivery_target
# ----------------------------------------------------------------------

def _noop_notify(*args, **kwargs):
    async def _inner():
        return None
    return _inner()


def _trigger_event(chat_id=None):
    return TriggerEvent(
        trigger_name="email",
        source="test",
        event_type="email",
        reason="new mail",
        payload={},
        chat_id=chat_id,
    )


def test_trigger_redirects_to_last_active():
    mgr = TriggerManager(
        notify_callback=_noop_notify,
        default_chat_id="telegram:default",
        resolve_delivery_callback=lambda fb: "web:last-active",
    )
    assert mgr._resolve_delivery_target(_trigger_event()) == "web:last-active"


def test_trigger_explicit_chat_id_wins():
    mgr = TriggerManager(
        notify_callback=_noop_notify,
        default_chat_id="telegram:default",
        resolve_delivery_callback=lambda fb: "web:last-active",
    )
    # 事件自带显式 chat_id → 不重定向
    assert mgr._resolve_delivery_target(_trigger_event(chat_id="telegram:explicit")) == "telegram:explicit"


def test_trigger_falls_back_to_default():
    mgr = TriggerManager(
        notify_callback=_noop_notify,
        default_chat_id="telegram:default",
        resolve_delivery_callback=lambda fb: "",
    )
    assert mgr._resolve_delivery_target(_trigger_event()) == "telegram:default"


def test_trigger_no_resolver_uses_default():
    mgr = TriggerManager(
        notify_callback=_noop_notify,
        default_chat_id="telegram:default",
    )
    assert mgr._resolve_delivery_target(_trigger_event()) == "telegram:default"


def test_trigger_handle_event_dispatches_resolved_target():
    captured = {}

    async def _capture(chat_id, reason, event_type):
        captured["chat_id"] = chat_id
        captured["reason"] = reason
        captured["event_type"] = event_type

    mgr = TriggerManager(
        notify_callback=_capture,
        default_chat_id="telegram:default",
        resolve_delivery_callback=lambda fb: "web:last-active",
    )
    asyncio.run(mgr._handle_event(_trigger_event()))
    assert captured["chat_id"] == "web:last-active"
    assert captured["event_type"] == "email"
