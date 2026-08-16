'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-16 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""/effort 命令与 inline keyboard 的测试。

重点覆盖三件历史上容易漏的事：
1. CommandHandler 注册（最近一次提交就是在补 /undo 的注册）；
2. CallbackQueryHandler 的前缀路由（新前缀不注册 = 按钮点了没反应）；
3. "跟随全局" 与 "none" 是两种不同状态，不能混为一谈。
"""
import inspect

import config
import pytest

from adapters.telegram.main import TelegramAdapter


class _DummyAdapter:
    _EFFORT_CHOICES = TelegramAdapter._EFFORT_CHOICES
    _build_effort_alias_menu = TelegramAdapter._build_effort_alias_menu
    _build_effort_level_menu = TelegramAdapter._build_effort_level_menu


# --- 注册完整性 ---

def test_effort_command_handler_is_registered():
    """漏注册的话 /effort 完全没反应，且不会有任何报错。"""
    source = inspect.getsource(TelegramAdapter._configure_application)
    assert "CommandHandler('effort'" in source


def test_effort_callback_prefixes_are_routed():
    """前缀没接进 _handle_callback，按钮就只会静默落到兜底分支。"""
    source = inspect.getsource(TelegramAdapter._handle_callback)
    assert 'startswith("effort_pick:")' in source
    assert 'startswith("effort_set:")' in source


def test_effort_callback_prefix_does_not_collide():
    """新前缀不能是既有前缀的前缀，否则会被先匹配的分支截走。"""
    source = inspect.getsource(TelegramAdapter._handle_callback)
    existing = [
        "debug_cleanup_confirm:", "model_pick:", "model_clear:", "model_set:",
        "model_page:", "model_provider:", "debug_cleanup:", "custom_scope:",
        "nora_prefs:", "set_stream:",
    ]
    for prefix in existing:
        assert prefix in source, f"既有前缀 {prefix} 意外消失"
        assert not prefix.startswith("effort_pick:")
        assert not prefix.startswith("effort_set:")


def test_effort_command_exists_on_adapter():
    assert callable(getattr(TelegramAdapter, "_effort_command", None))
    assert callable(getattr(TelegramAdapter, "_handle_effort_pick_callback", None))
    assert callable(getattr(TelegramAdapter, "_handle_effort_set_callback", None))


# --- 菜单渲染 ---

def _menu_labels(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


def _menu_callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_alias_menu_only_lists_configured_models(monkeypatch):
    """给没配模型的别名设 effort 没有意义，不该出现在菜单里。"""
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"llm": {"models": {"smart": "m1", "fast": "m2"}}},
    )
    _, markup = _DummyAdapter()._build_effort_alias_menu()
    callbacks = _menu_callbacks(markup)
    assert "effort_pick:smart" in callbacks
    assert "effort_pick:fast" in callbacks
    assert "effort_pick:coder" not in callbacks


def test_alias_menu_marks_global_fallback_distinctly(monkeypatch):
    """跟随全局、显式覆盖、完全未设置，三种状态在标签上必须能区分开。"""
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "llm": {
                "models": {"smart": "m1", "fast": "m2"},
                "effort": "medium",
                "effort_by_alias": {"smart": "high"},
            }
        },
    )
    _, markup = _DummyAdapter()._build_effort_alias_menu()
    labels = _menu_labels(markup)
    assert any("smart: high" in x for x in labels)
    assert any("fast: medium(全局)" in x for x in labels)


def test_alias_menu_shows_default_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(
        config, "get_config", lambda: {"llm": {"models": {"smart": "m1"}}}
    )
    _, markup = _DummyAdapter()._build_effort_alias_menu()
    assert any("smart: 默认" in x for x in _menu_labels(markup))


def test_alias_menu_survives_no_configured_models(monkeypatch):
    """一个模型都没配时也不能抛异常（否则 /effort 直接报错）。"""
    monkeypatch.setattr(config, "get_config", lambda: {"llm": {}})
    text, markup = _DummyAdapter()._build_effort_alias_menu()
    assert text
    assert _menu_callbacks(markup)


def test_level_menu_offers_all_levels_plus_inherit(monkeypatch):
    monkeypatch.setattr(config, "get_config", lambda: {"llm": {"effort": "low"}})
    _, markup = _DummyAdapter()._build_effort_level_menu("smart")
    callbacks = _menu_callbacks(markup)
    for level in ("none", "minimal", "low", "medium", "high"):
        assert f"effort_set:smart:{level}" in callbacks
    assert "effort_set:smart:inherit" in callbacks
    assert "effort_pick:back" in callbacks


def test_level_menu_checks_current_level(monkeypatch):
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"llm": {"effort_by_alias": {"smart": "high"}}},
    )
    _, markup = _DummyAdapter()._build_effort_level_menu("smart")
    checked = [x for x in _menu_labels(markup) if x.startswith("✅ ")]
    assert checked == ["✅ high"]


def test_level_menu_checks_inherit_when_no_override(monkeypatch):
    """没有别名级覆盖时，勾选的应是"跟随全局"而不是任何具体档位。"""
    monkeypatch.setattr(config, "get_config", lambda: {"llm": {"effort": "medium"}})
    _, markup = _DummyAdapter()._build_effort_level_menu("smart")
    checked = [x for x in _menu_labels(markup) if x.startswith("✅ ")]
    assert len(checked) == 1
    assert "跟随全局" in checked[0]


def test_level_menu_none_is_not_treated_as_inherit(monkeypatch):
    """none（显式关闭思考）和 inherit（不覆盖）是两种状态，勾选不能串。"""
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {"llm": {"effort": "high", "effort_by_alias": {"fast": "none"}}},
    )
    _, markup = _DummyAdapter()._build_effort_level_menu("fast")
    checked = [x for x in _menu_labels(markup) if x.startswith("✅ ")]
    assert checked == ["✅ none"]
