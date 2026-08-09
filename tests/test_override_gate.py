"""一次性放行闸门测试。覆盖标志位生命周期、窗口绑定、TTL 与主人闸门降级。

这些用例锁的是 core/override_gate.py 文档里那几条"改动时不要退回去"的约束：
放行只覆盖一次、跨窗口不生效、解析器缺失时拒绝而不是降级放行。
"""

import time

import core.override_gate as gate
from core.override_gate import (
    OVERRIDE_FLAG_KEY,
    OVERRIDE_PLACEHOLDER_TEXT,
    build_override_block,
    check_owner_permission,
    clear_override,
    grant_override,
    override_active,
)


class _Identity:
    def __init__(self, is_owner):
        self.is_owner = is_owner


def test_grant_then_active():
    session = {}
    grant_override(session, "telegram:111")
    assert override_active(session, "telegram:111") is True


def test_clean_session_not_active():
    assert override_active({}, "telegram:111") is False


def test_override_active_does_not_consume():
    session = {}
    grant_override(session, "telegram:111")
    # 只读不消费：连读两次都应为 True，消费只发生在 clear_override
    assert override_active(session, "telegram:111") is True
    assert override_active(session, "telegram:111") is True


def test_clear_consumes_once():
    session = {}
    grant_override(session, "telegram:111")
    assert clear_override(session, "telegram:111") is True
    # 消费后失效，再清一次返回 False
    assert clear_override(session, "telegram:111") is False
    assert override_active(session, "telegram:111") is False


def test_flag_bound_to_runtime_key():
    session = {}
    grant_override(session, "telegram:111")
    # 换一个窗口读不到，且会就地丢弃，避免私聊放行漏到群聊
    assert override_active(session, "telegram:222") is False
    assert OVERRIDE_FLAG_KEY not in session


def test_ttl_expiry(monkeypatch):
    session = {}
    grant_override(session, "telegram:111")
    later = time.time() + gate.OVERRIDE_TTL_SECONDS + 1
    monkeypatch.setattr(gate.time, "time", lambda: later)
    # 超时自动失效，防"武装后重新生成从未跑起来"导致标志长期滞留
    assert override_active(session, "telegram:111") is False
    assert OVERRIDE_FLAG_KEY not in session


def test_non_dict_session_is_safe():
    assert override_active(None, "telegram:111") is False
    assert clear_override(None, "telegram:111") is False
    grant_override(None, "telegram:111")  # 不应抛异常


def test_owner_gate_denies_without_resolver(monkeypatch):
    monkeypatch.setattr(gate, "owner_resolution_available", lambda: False)
    allowed, reason = check_owner_permission(_Identity(True))
    # 解析器缺失时拒绝而非降级放行（与投递端闸门相反），且必须给出原因
    assert allowed is False
    assert reason


def test_owner_gate_denies_non_owner(monkeypatch):
    monkeypatch.setattr(gate, "owner_resolution_available", lambda: True)
    allowed, reason = check_owner_permission(_Identity(False))
    assert allowed is False
    assert reason


def test_owner_gate_denies_missing_identity(monkeypatch):
    monkeypatch.setattr(gate, "owner_resolution_available", lambda: True)
    allowed, _ = check_owner_permission(None)
    assert allowed is False


def test_owner_gate_allows_owner(monkeypatch):
    monkeypatch.setattr(gate, "owner_resolution_available", lambda: True)
    allowed, reason = check_owner_permission(_Identity(True))
    assert allowed is True
    assert reason == ""


def test_override_block_states_scope_and_non_baseline():
    block = build_override_block()
    # 一次性、不改写、不把这次措辞当语气基线：缺任一条就会出现顺从漂移或软性拒绝
    assert "仅此一次" in block
    assert "不要改写" in block
    assert "不代表你平时的说话方式" in block


def test_placeholder_text_non_empty():
    # 入库正文不能为空，否则触发 COMMON_PITFALLS 5.7 的空消息静默
    assert OVERRIDE_PLACEHOLDER_TEXT.strip()
