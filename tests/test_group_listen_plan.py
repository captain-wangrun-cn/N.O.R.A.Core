"""群聊定时 ONLINE 监听计划（GROUP_LISTEN.json）与调度触发的行为测试。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import group_listen_plan as glp


@pytest.fixture(autouse=True)
def _patch_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(
        glp,
        "get_workspace_manager",
        lambda: SimpleNamespace(root=str(tmp_path)),
    )


def _now(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 3, hour, minute)


def _state(entries, *, generated="2026-08-03", enabled=True):
    return {
        "enabled": enabled,
        "generated_date": generated,
        "entries": entries,
        "groups": [],
        "preferences": {},
    }


def test_plan_file_is_created_with_editable_skeleton():
    path = glp.ensure_plan_file()
    state = glp.read_plan()

    assert path.endswith(glp.PLAN_FILE_NAME)
    # Nora 自己维护的三块字段必须存在，否则她无从下手编辑
    assert state["groups"] == []
    assert "max_entries_per_day" in state["preferences"]
    assert state["enabled"] is True


def test_normalize_entries_keeps_one_group_per_time_point():
    entries = glp.normalize_entries([
        {"time": "9:05", "platform": "onebotv11", "group_id": "111"},
        {"time": "09:05", "platform": "onebotv11", "group_id": "222"},  # 同一时间点，丢弃
        {"time": "21:30", "platform": "onebotv11", "group_id": "222"},
        {"time": "bad", "platform": "onebotv11", "group_id": "333"},    # 非法时间，丢弃
        {"time": "10:00", "platform": "", "group_id": "444"},           # 缺平台，丢弃
    ])

    assert [(e["time"], e["group_id"]) for e in entries] == [("09:05", "111"), ("21:30", "222")]


def test_due_entry_respects_grace_window_and_fired_flag():
    entries = [
        {"time": "09:00", "platform": "p", "group_id": "1", "fired": True},
        {"time": "10:00", "platform": "p", "group_id": "2", "fired": False},
        {"time": "23:00", "platform": "p", "group_id": "3", "fired": False},
    ]

    # 到点、在宽限窗口内 → 触发
    index, entry = glp.due_entry(_state(entries), _now(10, 5), grace_minutes=10)
    assert index == 1 and entry["group_id"] == "2"

    # 超过宽限窗口 → 不触发（由 expire_stale_entries 收尾）
    assert glp.due_entry(_state(entries), _now(10, 40), grace_minutes=10) == (-1, None)

    # 未到点 → 不触发
    assert glp.due_entry(_state(entries), _now(9, 30), grace_minutes=10) == (-1, None)


def test_due_entry_returns_only_one_entry_when_several_are_due():
    entries = [
        {"time": "10:00", "platform": "p", "group_id": "1"},
        {"time": "10:03", "platform": "p", "group_id": "2"},
    ]
    index, entry = glp.due_entry(_state(entries), _now(10, 5), grace_minutes=10)
    assert entry["group_id"] == "2"  # 取更贴近当下的那个


def test_due_entry_skips_stale_generated_date_and_disabled_plan():
    entries = [{"time": "10:00", "platform": "p", "group_id": "1"}]
    assert glp.due_entry(_state(entries, generated="2026-08-01"), _now(10, 1)) == (-1, None)
    assert glp.due_entry(_state(entries, enabled=False), _now(10, 1)) == (-1, None)


def test_mark_entry_preserves_manually_maintained_fields():
    glp.write_plan({
        "enabled": True,
        "generated_date": "2026-08-03",
        "groups": [{"platform": "onebotv11", "group_id": "111", "name": "开发群"}],
        "preferences": {"max_entries_per_day": 2, "notes": "别打扰晨会"},
        "entries": [{"time": "10:00", "platform": "onebotv11", "group_id": "111", "fired": False}],
    })

    assert glp.mark_entry(0, result="activated")

    state = glp.read_plan()
    assert state["entries"][0]["fired"] is True
    assert state["entries"][0]["result"] == "activated"
    # Nora 手工维护的候选池与偏好不能被触发写回抹掉
    assert state["groups"][0]["group_id"] == "111"
    assert state["preferences"]["notes"] == "别打扰晨会"


def test_expire_stale_entries_marks_missed():
    glp.write_plan(_state([
        {"time": "08:00", "platform": "p", "group_id": "1", "fired": False},
        {"time": "23:00", "platform": "p", "group_id": "2", "fired": False},
    ]))

    assert glp.expire_stale_entries(_now(12, 0), grace_minutes=10) == 1
    entries = glp.read_plan()["entries"]
    assert entries[0]["result"] == "missed" and entries[0]["fired"] is True
    assert entries[1]["fired"] is False


def test_candidate_groups_filters_disabled_and_incomplete():
    state = {
        "groups": [
            {"platform": "onebotv11", "group_id": "111", "name": "A"},
            {"platform": "onebotv11", "group_id": "111"},          # 重复
            {"platform": "onebotv11", "group_id": "222", "enabled": False},
            {"platform": "", "group_id": "333"},
            {"platform": "telegram", "chat_id": "-100"},           # 兼容 chat_id 字段
        ]
    }
    groups = glp.candidate_groups(state)
    assert [(g["platform"], g["group_id"]) for g in groups] == [
        ("onebotv11", "111"),
        ("telegram", "-100"),
    ]


# ---------------------------------------------------------------------------
# 调度触发：已 ONLINE 时跳过（与私聊 proactive 语义一致）
# ---------------------------------------------------------------------------

class _FakeGroupListener:
    def __init__(self, mode):
        self.enabled = True
        self._mode = mode
        self.calls = []

    def mode_for(self, runtime_key):
        return self._mode

    async def set_mode(self, runtime_key, mode, **kwargs):
        self.calls.append((runtime_key, mode, kwargs.get("reason")))
        return True


class _Host:
    """只挂上被测方法所需的最小依赖。"""

    def __init__(self, listener):
        self.group_listener = listener

    from core.scheduler_mixin import SchedulerMixin as _Mixin
    _activate_scheduled_group_listen = _Mixin._activate_scheduled_group_listen


def test_activation_skips_group_that_is_already_online():
    from core.group_presence_store import GroupPresence

    listener = _FakeGroupListener(GroupPresence.ONLINE)
    host = _Host(listener)
    entry = {"platform": "onebotv11", "group_id": "111", "name": "开发群"}

    result = asyncio.run(host._activate_scheduled_group_listen(entry))

    assert result == "skipped_already_online"
    assert listener.calls == []


def test_activation_sets_online_with_scheduled_reason():
    from core.group_presence_store import GroupPresence

    listener = _FakeGroupListener(GroupPresence.SEMI_ONLINE)
    host = _Host(listener)
    entry = {"platform": "onebotv11", "group_id": "111"}

    result = asyncio.run(host._activate_scheduled_group_listen(entry))

    assert result == "activated"
    runtime_key, mode, reason = listener.calls[0]
    assert runtime_key == "onebotv11:111"
    assert mode == GroupPresence.ONLINE
    # reason 会被 GroupListenerManager 用来把 trigger_kind 标成 scheduled
    assert reason == "scheduled_group_listen"


def test_activation_rejects_entry_without_platform():
    from core.group_presence_store import GroupPresence

    listener = _FakeGroupListener(GroupPresence.SEMI_ONLINE)
    host = _Host(listener)

    assert asyncio.run(host._activate_scheduled_group_listen({"group_id": "111"})) == "invalid_target"
    assert listener.calls == []
