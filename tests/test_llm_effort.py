'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-16 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""推理强度（effort）配置与各家 provider 映射的测试。

三家 API 的字段完全不通用，映射写错的一侧通常**不报错**、只是被静默忽略，
所以这里逐家断言实际写进 payload 的键名和取值。
"""
import config
import pytest


# --- config 层：两级回退与规整 ---

def test_effort_falls_back_from_alias_to_global(monkeypatch):
    monkeypatch.setattr(
        config,
        "_config",
        {"llm": {"effort": "medium", "effort_by_alias": {"smart": "high", "fast": "none"}}},
    )
    assert config.get_llm_effort("smart") == "high"
    assert config.get_llm_effort("fast") == "none"
    # 未列出的 alias 回落全局
    assert config.get_llm_effort("coder") == "medium"


def test_effort_absent_returns_none(monkeypatch):
    """未配置必须返回 None —— 代表"不传该字段"，而不是某个默认档位。"""
    monkeypatch.setattr(config, "_config", {"llm": {}})
    assert config.get_llm_effort("smart") is None
    assert config.get_llm_effort_budget_tokens("smart") is None


def test_effort_invalid_value_is_ignored(monkeypatch):
    monkeypatch.setattr(config, "_config", {"llm": {"effort_by_alias": {"smart": "ultra"}}})
    assert config.get_llm_effort("smart") is None


def test_effort_aliases_normalize_to_none(monkeypatch):
    monkeypatch.setattr(config, "_config", {"llm": {"effort_by_alias": {"fast": "OFF"}}})
    assert config.get_llm_effort("fast") == "none"


def test_effort_none_budget_is_zero_not_none(monkeypatch):
    """none 档要能和"未配置"区分开：0 表示显式关闭思考，None 表示不传字段。"""
    monkeypatch.setattr(config, "_config", {"llm": {"effort_by_alias": {"fast": "none"}}})
    assert config.get_llm_effort_budget_tokens("fast") == 0


def test_effort_budget_scales_with_level(monkeypatch):
    monkeypatch.setattr(config, "_config", {"llm": {"effort": "low"}})
    low = config.get_llm_effort_budget_tokens("smart")
    monkeypatch.setattr(config, "_config", {"llm": {"effort": "high"}})
    high = config.get_llm_effort_budget_tokens("smart")
    assert 0 < low < high


def test_set_llm_effort_by_alias_writes_and_clears(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "_config", {"llm": {"models": {"smart": "m"}}})
    monkeypatch.setattr(config, "save_config", lambda cfg: saved.update(cfg))

    result = config.set_llm_effort_by_alias("smart", "high")
    assert result == {"smart": "high"}
    assert saved["llm"]["effort_by_alias"] == {"smart": "high"}
    # 已有配置不该被顺手丢掉
    assert saved["llm"]["models"] == {"smart": "m"}

    # 清除该别名后 effort_by_alias 应整体消失，而不是留一个空 dict
    monkeypatch.setattr(config, "_config", saved)
    assert config.set_llm_effort_by_alias("smart", None) == {}
    assert "effort_by_alias" not in saved["llm"]


# --- provider 层：各家字段名与取值 ---

def _openai_provider(monkeypatch, effort):
    from brain.providers import openai as openai_mod

    monkeypatch.setattr(openai_mod.config, "get_model_provider", lambda alias: "p")
    monkeypatch.setattr(openai_mod.config, "get_api_key", lambda p=None: "k")
    monkeypatch.setattr(openai_mod.config, "get_base_url", lambda p=None: None)
    monkeypatch.setattr(openai_mod.config, "get_llm_user_agent", lambda p=None: None)
    monkeypatch.setattr(openai_mod.config, "get_provider_option", lambda p=None, key="": None)
    monkeypatch.setattr(openai_mod.config, "get_model_name", lambda alias: "gpt-x")
    monkeypatch.setattr(openai_mod.config, "get_llm_max_output_tokens", lambda alias: 768)
    monkeypatch.setattr(openai_mod.config, "get_llm_effort", lambda alias: effort)
    return openai_mod.OpenAIProvider(model_alias="smart")


def test_openai_uses_reasoning_effort_string(monkeypatch):
    provider = _openai_provider(monkeypatch, "high")
    assert provider._apply_effort({}) == {"reasoning_effort": "high"}


def test_openai_passes_none_level_through(monkeypatch):
    """none 是合法档位（关闭思考），不能被真值判断吞掉。"""
    provider = _openai_provider(monkeypatch, "none")
    assert provider._apply_effort({}) == {"reasoning_effort": "none"}


def test_openai_omits_field_when_unconfigured(monkeypatch):
    provider = _openai_provider(monkeypatch, None)
    assert provider._apply_effort({}) == {}


def test_effort_helpers_tolerate_missing_attribute():
    """effort 是纯可选字段：绕过 __init__ 构造的实例不该因为少它就崩。

    `tests/test_openai_tools.py` 就用 `object.__new__(OpenAIProvider)` 装配 provider，
    历史上 `_apply_effort` 直接读 `self.effort` 让那条测试 AttributeError。
    """
    from brain.providers.openai import OpenAIProvider

    bare = object.__new__(OpenAIProvider)
    assert bare._apply_effort({"model": "m"}) == {"model": "m"}

    from brain.providers.gemini import GeminiProvider

    bare_gemini = object.__new__(GeminiProvider)
    assert bare_gemini._effort_generation_config() == {}
    assert bare_gemini._effort_generation_config(camel_case=True) == {}


@pytest.mark.parametrize(
    "message",
    [
        "unrecognized parameter: reasoning_effort",
        "Unsupported parameter 'reasoning_effort' for this model",
        "reasoning is not supported by this model",
        "invalid request: reasoning effort unsupported",
    ],
)
def test_effort_rejection_detected_across_wordings(message):
    """各家兼容端点的措辞差别很大，识别要宽松——漏判会让整个别名不可用。"""
    from brain.providers.openai import OpenAIProvider

    assert OpenAIProvider._is_effort_rejection(RuntimeError(message))


def test_effort_rejection_ignores_unrelated_errors():
    from brain.providers.openai import OpenAIProvider

    assert not OpenAIProvider._is_effort_rejection(RuntimeError("rate limit exceeded"))
    assert not OpenAIProvider._is_effort_rejection(RuntimeError("connection reset"))


def _gemini_provider(monkeypatch, effort, budget):
    from brain.providers import gemini as gemini_mod

    monkeypatch.setattr(gemini_mod.config, "get_model_provider", lambda alias: "p")
    monkeypatch.setattr(gemini_mod.config, "get_api_key", lambda p=None: "k")
    monkeypatch.setattr(gemini_mod.config, "get_base_url", lambda p=None: None)
    monkeypatch.setattr(gemini_mod.config, "get_model_name", lambda alias: "gemini-x")
    monkeypatch.setattr(gemini_mod.config, "get_llm_max_output_tokens", lambda alias: 768)
    monkeypatch.setattr(gemini_mod.config, "get_llm_effort", lambda alias: effort)
    monkeypatch.setattr(gemini_mod.config, "get_llm_effort_budget_tokens", lambda alias: budget)
    monkeypatch.setattr(gemini_mod.genai, "configure", lambda **kw: None)
    monkeypatch.setattr(gemini_mod.genai, "GenerativeModel", lambda *a, **kw: object())
    return gemini_mod.GeminiProvider(model_alias="smart")


def test_gemini_sdk_uses_snake_case_thinking_config(monkeypatch):
    provider = _gemini_provider(monkeypatch, "high", 24576)
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_budget": 24576}
    }


def test_gemini_rest_uses_camel_case_thinking_config(monkeypatch):
    """REST body 必须 camelCase —— 写成 snake_case 会被 API 静默忽略。"""
    provider = _gemini_provider(monkeypatch, "high", 24576)
    assert provider._effort_generation_config(camel_case=True) == {
        "thinkingConfig": {"thinkingBudget": 24576}
    }


def test_gemini_none_level_sets_zero_budget(monkeypatch):
    provider = _gemini_provider(monkeypatch, "none", 0)
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_budget": 0}
    }


def test_gemini_omits_config_when_unconfigured(monkeypatch):
    provider = _gemini_provider(monkeypatch, None, None)
    assert provider._effort_generation_config() == {}
    assert provider._effort_generation_config(camel_case=True) == {}


def _anthropic_provider(monkeypatch, effort, budget, max_tokens=8192):
    anthropic_mod = pytest.importorskip("brain.providers.anthropic")

    monkeypatch.setattr(anthropic_mod.config, "get_model_provider", lambda alias: "p")
    monkeypatch.setattr(anthropic_mod.config, "get_api_key", lambda p=None: "k")
    monkeypatch.setattr(anthropic_mod.config, "get_base_url", lambda p=None: None)
    monkeypatch.setattr(anthropic_mod.config, "get_model_name", lambda alias: "claude-x")
    monkeypatch.setattr(anthropic_mod.config, "get_llm_max_output_tokens", lambda alias: max_tokens)
    monkeypatch.setattr(anthropic_mod.config, "get_llm_effort", lambda alias: effort)
    monkeypatch.setattr(anthropic_mod.config, "get_llm_effort_budget_tokens", lambda alias: budget)
    monkeypatch.setattr(anthropic_mod, "AsyncAnthropic", lambda **kw: object())
    return anthropic_mod.AnthropicProvider(model_alias="smart")


def test_anthropic_enables_thinking_with_budget(monkeypatch):
    provider = _anthropic_provider(monkeypatch, "medium", 8192, max_tokens=32000)
    payload = provider._apply_effort({"max_tokens": 32000})
    assert payload["thinking"]["type"] == "enabled"
    assert payload["thinking"]["budget_tokens"] == 8192


def test_anthropic_budget_stays_below_max_tokens(monkeypatch):
    """budget_tokens 必须小于 max_tokens，否则 Anthropic 直接报错。"""
    provider = _anthropic_provider(monkeypatch, "high", 24576, max_tokens=4096)
    payload = provider._apply_effort({"max_tokens": 4096})
    assert payload["thinking"]["budget_tokens"] < 4096


def test_anthropic_none_level_disables_thinking(monkeypatch):
    provider = _anthropic_provider(monkeypatch, "none", 0)
    assert provider._apply_effort({"max_tokens": 8192})["thinking"] == {"type": "disabled"}


def test_anthropic_omits_thinking_when_unconfigured(monkeypatch):
    provider = _anthropic_provider(monkeypatch, None, None)
    assert "thinking" not in provider._apply_effort({"max_tokens": 8192})


def test_anthropic_skips_thinking_when_max_tokens_too_small(monkeypatch):
    """max_tokens 塞不进任何合法预算时宁可不开思考，也不能发出非法请求。"""
    provider = _anthropic_provider(monkeypatch, "high", 24576, max_tokens=1024)
    assert "thinking" not in provider._apply_effort({"max_tokens": 1024})
