"""多适配器注册表按平台 key 选适配器的单元测试（Phase B）。

不构造完整 NoraController（依赖 LLM/DB/RAG），用 __new__ 绕过 __init__，
只装配 _adapter_for_key 所需的最小状态。
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.controller import NoraController


def _fake_adapter(platform_name):
    return SimpleNamespace(platform_name=platform_name)


def _controller_with_adapters(adapters):
    ctrl = NoraController.__new__(NoraController)
    ctrl.adapters = {a.platform_name: a for a in adapters}
    ctrl.adapter = adapters[0]
    ctrl.conversation_identities = {}
    return ctrl


def test_adapter_for_key_selects_by_platform_prefix():
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])

    assert ctrl._adapter_for_key("telegram:123") is tg
    assert ctrl._adapter_for_key("onebotv11:-100") is ob


def test_adapter_for_key_unknown_platform_is_rejected():
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])

    with pytest.raises(ValueError, match="未注册目标平台适配器: web"):
        ctrl._adapter_for_key("web:xyz")


def test_adapter_for_key_single_adapter_registry():
    tg = _fake_adapter("telegram")
    ctrl = _controller_with_adapters([tg])

    assert ctrl._adapter_for_key("telegram:1") is tg
    with pytest.raises(ValueError, match="未注册目标平台适配器: onebotv11"):
        ctrl._adapter_for_key("onebotv11:2")


def test_resolve_send_target_logs_route_and_rejects_unknown_platform(caplog):
    tg = _fake_adapter("telegram")
    ctrl = _controller_with_adapters([tg])
    source = ctrl._identity_for_runtime_key("telegram:100")

    with caplog.at_level("INFO", logger="core.controller"):
        resolved = ctrl.resolve_send_target(
            {"platform": "web", "scene_id": "xyz", "scene_type": "private"},
            source,
            route_source="front_brain",
        )

    assert resolved["runtime_key"] == ""
    assert resolved["policy"] == "rejected_unknown_platform"
    assert "platform=web" in caplog.text
    assert "scene=private:xyz" in caplog.text
    assert "final_target=拒绝投递" in caplog.text
    assert "policy=rejected_unknown_platform" in caplog.text


def test_resolve_send_target_logs_missing_route_markers(caplog):
    tg = _fake_adapter("telegram")
    ctrl = _controller_with_adapters([tg])
    source = ctrl._identity_for_runtime_key("telegram:100")

    with caplog.at_level("INFO", logger="core.controller"):
        resolved = ctrl.resolve_send_target({}, source, route_source="front_brain")

    assert resolved["runtime_key"] == "telegram:100"
    assert "platform=未识别, scene=未识别" in caplog.text
    assert "policy=default_current" in caplog.text


def test_resolve_route_state_buffers_cross_chunk_prefix(monkeypatch):
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])
    source = ctrl._identity_for_runtime_key("telegram:100")
    state = {
        "key": source.runtime_key,
        "chat_type": source.chat_type,
        "redirected": False,
        "identity": source,
        "resolved": False,
    }

    ctrl.resolve_route_state("[platform:one", state, source)
    assert state["resolved"] is False
    assert state["send_text"] == ""

    ctrl.resolve_route_state("botv11][scene:private:200]你好", state, source)
    assert state["resolved"] is True
    assert state["key"] == "onebotv11:200"
    assert state["redirected"] is True
    assert state["send_text"] == "你好"


def test_resolve_route_state_rejects_marker_after_initial_text(caplog):
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])
    source = ctrl._identity_for_runtime_key("telegram:100")
    state = {
        "key": source.runtime_key,
        "chat_type": source.chat_type,
        "redirected": False,
        "identity": source,
        "resolved": False,
    }

    with caplog.at_level("INFO", logger="core.controller"):
        ctrl.resolve_route_state("正文[platform:onebotv11][scene:private:200]后续", state, source)

    assert state["resolved"] is True
    assert state["key"] == "telegram:100"
    assert state["redirected"] is False
    assert state["send_text"] == "正文后续"
    assert "policy=rejected_late_route" in caplog.text



def test_resolve_route_state_rejects_late_marker(caplog):
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])
    source = ctrl._identity_for_runtime_key("telegram:100")
    state = {
        "key": source.runtime_key,
        "chat_type": source.chat_type,
        "redirected": False,
        "identity": source,
        "resolved": False,
    }

    ctrl.resolve_route_state("正文已经开始", state, source)
    with caplog.at_level("INFO", logger="core.controller"):
        ctrl.resolve_route_state("[platform:onebotv11][scene:private:200]后续", state, source)

    assert state["key"] == "telegram:100"
    assert state["send_text"] == "后续"
    assert "policy=rejected_late_route" in caplog.text


def test_resolve_route_state_redirect_clears_source_reply_semantics():
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])
    source = ctrl._identity_for_runtime_key("telegram:100")
    state = {
        "key": source.runtime_key,
        "chat_type": source.chat_type,
        "redirected": False,
        "identity": source,
        "resolved": False,
    }

    ctrl.resolve_route_state("[platform:onebotv11][scene:group:700]结果", state, source)

    assert state["redirected"] is True
    assert state["key"] == "onebotv11:700"
    assert state["chat_type"] == "group"


def test_send_platform_message_logs_recognized_controls(caplog):
    sent = []

    class _Adapter:
        platform_name = "telegram"
        platform_features = SimpleNamespace(supports_message_controls=True)

        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))
            return ["1"]

    ctrl = _controller_with_adapters([_Adapter()])
    with caplog.at_level("INFO", logger="core.controller"):
        asyncio.run(
            ctrl._send_platform_message(
                "telegram:group:100",
                "[reply:777][at:321]你好",
                chat_type="group",
            )
        )

    assert "消息控制标记: reply=777, at=['321']" in caplog.text
    assert sent[0][1] == "[reply:777][at:321]你好"


def test_send_platform_message_logs_missing_controls(caplog):
    class _Adapter:
        platform_name = "telegram"
        platform_features = SimpleNamespace(supports_message_controls=True)

        async def send_message(self, chat_id, text, **kwargs):
            return ["1"]

    ctrl = _controller_with_adapters([_Adapter()])
    with caplog.at_level("INFO", logger="core.controller"):
        asyncio.run(ctrl._send_platform_message("telegram:100", "普通消息"))

    assert "消息控制标记: reply=未识别, at=未识别" in caplog.text
