"""多适配器注册表按平台 key 选适配器的单元测试（Phase B）。

不构造完整 NoraController（依赖 LLM/DB/RAG），用 __new__ 绕过 __init__，
只装配 _adapter_for_key 所需的最小状态。
"""

from types import SimpleNamespace

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


def test_adapter_for_key_unknown_platform_falls_back_to_primary():
    tg = _fake_adapter("telegram")
    ob = _fake_adapter("onebotv11")
    ctrl = _controller_with_adapters([tg, ob])  # primary = telegram

    # 未注册平台 → 回退 primary
    assert ctrl._adapter_for_key("web:xyz") is tg


def test_adapter_for_key_single_adapter_registry():
    tg = _fake_adapter("telegram")
    ctrl = _controller_with_adapters([tg])

    assert ctrl._adapter_for_key("telegram:1") is tg
    # 单适配器时任何平台都回退到它
    assert ctrl._adapter_for_key("onebotv11:2") is tg
