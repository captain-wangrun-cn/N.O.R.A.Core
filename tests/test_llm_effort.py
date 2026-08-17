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


def test_ultracode_is_not_a_valid_tier(monkeypatch):
    """ultracode 是 Claude Code 的编排关键词，不是任何 API 的档位值。

    留着它只会让人以为能配；配了之后各家都会 400 或静默忽略。
    """
    assert "ultracode" not in config.EFFORT_LEVELS
    monkeypatch.setattr(config, "_config", {"llm": {"effort": "ultracode"}})
    assert config.get_llm_effort("smart") is None


def test_effort_levels_cover_openai_official_enum():
    """档位表跟 OpenAI 官方 reasoning 文档的枚举对齐（auto 是我们自己加的第 8 档）。"""
    official = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    assert official.issubset(set(config.EFFORT_LEVELS))
    assert config.EFFORT_AUTO in config.EFFORT_LEVELS


@pytest.mark.parametrize("raw", ["adaptive", "dynamic", "default", "AUTO"])
def test_auto_synonyms_normalize_to_auto(raw):
    """三家的"自动"各叫各的名字，按哪家的叫法写都不该失效。"""
    assert config.normalize_effort(raw) == config.EFFORT_AUTO


def test_auto_budget_is_negative_sentinel(monkeypatch):
    """auto 的 -1 是 Gemini 的 dynamic 哨兵，不是"负预算"。"""
    monkeypatch.setattr(config, "_config", {"llm": {"effort": "auto"}})
    assert config.get_llm_effort_budget_tokens("smart") == -1


@pytest.mark.parametrize(
    "effort,expected",
    [("none", "MINIMAL"), ("minimal", "MINIMAL"), ("low", "LOW"),
     ("medium", "MEDIUM"), ("high", "HIGH"), ("xhigh", "HIGH"), ("max", "HIGH")],
)
def test_thinking_level_mapping(effort, expected):
    assert config.get_effort_thinking_level(effort) == expected


def test_thinking_level_absent_for_auto_and_unset():
    """auto/未配置在 Gemini 3.x 上是"不传字段"，不能落成某个具体档。"""
    assert config.get_effort_thinking_level(config.EFFORT_AUTO) is None
    assert config.get_effort_thinking_level(None) is None


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


@pytest.mark.parametrize("effort", ["high", "xhigh", "max", "minimal", "none"])
def test_openai_responses_api_nests_effort(monkeypatch, effort):
    """Responses API 要嵌套 reasoning={"effort":...}；平铺 reasoning_effort 会 400。

    `_filter_supported_kwargs` 救不了这个——`reasoning` 是合法顶层参数名，
    它不知道里面该放什么，所以平铺字段会原样发出去然后被拒。
    """
    provider = _openai_provider(monkeypatch, effort)
    payload = provider._apply_effort({}, responses_api=True)
    assert payload == {"reasoning": {"effort": effort}}
    assert "reasoning_effort" not in payload


def test_openai_auto_omits_field_on_both_apis(monkeypatch):
    """auto 是本项目自己的档，OpenAI 没这个值；语义就是"用端点默认"= 不传。"""
    provider = _openai_provider(monkeypatch, "auto")
    assert provider._apply_effort({}) == {}
    assert provider._apply_effort({}, responses_api=True) == {}


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_openai_passes_top_tiers_through(monkeypatch, effort):
    """xhigh/max 是 OpenAI 官方枚举里的值，不该在本地被拦掉或降档。"""
    provider = _openai_provider(monkeypatch, effort)
    assert provider._apply_effort({}) == {"reasoning_effort": effort}


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


@pytest.mark.parametrize(
    "message",
    [
        # 档位是 model-dependent 的：老模型收到 xhigh/max 是「值不支持」，
        # 报错里可能压根不提字段名，只列合法值。漏判就是整个别名不可用。
        "Unsupported value: 'xhigh' is not supported with this model. "
        "Supported values are: 'low', 'medium', and 'high'.",
        "Invalid value: 'max'. Supported values are: 'low', 'medium', 'high'",
        "Invalid reasoning_effort: minimal. Valid values are: high, low, medium, none",
    ],
)
def test_effort_value_rejection_detected(message):
    from brain.providers.openai import OpenAIProvider

    assert OpenAIProvider._is_effort_rejection(RuntimeError(message))


def _gemini_provider(monkeypatch, effort, budget, model_name="gemini-2.5-flash"):
    from brain.providers import gemini as gemini_mod

    monkeypatch.setattr(gemini_mod.config, "get_model_provider", lambda alias: "p")
    monkeypatch.setattr(gemini_mod.config, "get_api_key", lambda p=None: "k")
    monkeypatch.setattr(gemini_mod.config, "get_base_url", lambda p=None: None)
    monkeypatch.setattr(gemini_mod.config, "get_model_name", lambda alias: model_name)
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


# --- Gemini 3.x：thinkingLevel 枚举，不是 thinkingBudget -------------------
# 两个代次的字段互斥：给 3.x 发 budget、给 2.5 发 level，两个方向都是 400。
# 生产上 draw_desc 用的 gemini-3.6-flash 就属于 3.x。

@pytest.mark.parametrize(
    "model_name",
    ["gemini-3.6-flash", "gemini-3-pro-preview", "gemini-3.1-flash-lite", "gemini-4.0-pro"],
)
def test_gemini_3x_uses_thinking_level(monkeypatch, model_name):
    provider = _gemini_provider(monkeypatch, "high", 24576, model_name=model_name)
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_level": "HIGH"}
    }


@pytest.mark.parametrize(
    "model_name", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro-latest"]
)
def test_gemini_legacy_uses_thinking_budget(monkeypatch, model_name):
    provider = _gemini_provider(monkeypatch, "high", 24576, model_name=model_name)
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_budget": 24576}
    }


def test_gemini_3x_rest_uses_camel_case_level(monkeypatch):
    provider = _gemini_provider(monkeypatch, "medium", 8192, model_name="gemini-3.6-flash")
    assert provider._effort_generation_config(camel_case=True) == {
        "thinkingConfig": {"thinkingLevel": "MEDIUM"}
    }


@pytest.mark.parametrize("effort,expected", [("xhigh", "HIGH"), ("max", "HIGH")])
def test_gemini_3x_clamps_xhigh_and_max_to_high(monkeypatch, effort, expected):
    """Gemini 只有 4 档，xhigh/max 一起降到 HIGH（OpenRouter 官方也这么降）。"""
    provider = _gemini_provider(monkeypatch, effort, 32768, model_name="gemini-3.6-flash")
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_level": expected}
    }


def test_gemini_3x_none_maps_to_minimal_not_zero(monkeypatch):
    """3.x 关不掉思考，none 只能降到最低档 MINIMAL；发 budget=0 会 400。"""
    provider = _gemini_provider(monkeypatch, "none", 0, model_name="gemini-3.6-flash")
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_level": "MINIMAL"}
    }


def test_gemini_3x_auto_omits_field(monkeypatch):
    """3.x 没有 dynamic 预算这个旋钮，auto = 不传，用模型自己的默认档。"""
    provider = _gemini_provider(monkeypatch, "auto", -1, model_name="gemini-3.6-flash")
    assert provider._effort_generation_config() == {}


def test_gemini_legacy_auto_uses_dynamic_sentinel(monkeypatch):
    """2.5 的"自动"是 thinkingBudget=-1（dynamic thinking）。"""
    provider = _gemini_provider(monkeypatch, "auto", -1, model_name="gemini-2.5-pro")
    assert provider._effort_generation_config() == {
        "thinking_config": {"thinking_budget": -1}
    }


def test_gemini_omits_config_when_unconfigured(monkeypatch):
    provider = _gemini_provider(monkeypatch, None, None)
    assert provider._effort_generation_config() == {}
    assert provider._effort_generation_config(camel_case=True) == {}


def _anthropic_provider(monkeypatch, effort, budget, max_tokens=8192, model_name="claude-sonnet-4-5"):
    anthropic_mod = pytest.importorskip("brain.providers.anthropic")

    monkeypatch.setattr(anthropic_mod.config, "get_model_provider", lambda alias: "p")
    monkeypatch.setattr(anthropic_mod.config, "get_api_key", lambda p=None: "k")
    monkeypatch.setattr(anthropic_mod.config, "get_base_url", lambda p=None: None)
    monkeypatch.setattr(anthropic_mod.config, "get_model_name", lambda alias: model_name)
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


# --- Anthropic 4.6+：output_config.effort，不是 thinking.budget_tokens ------
# 2026 架构变更：effort 档位字符串在顶层 output_config 里，thinking 只管模式。
# Opus 4.7+ 收到 {"type":"enabled"} 会回
# `thinking.type.enabled is not supported for this model` —— 是 400 不是降级。

@pytest.mark.parametrize(
    "model_name",
    [
        "claude-opus-4-6", "claude-opus-4-7-20260115", "claude-opus-4-8",
        "claude-sonnet-4-6", "claude-sonnet-5", "claude-opus-5",
    ],
)
def test_anthropic_new_models_use_output_config_effort(monkeypatch, model_name):
    provider = _anthropic_provider(monkeypatch, "high", 24576, model_name=model_name)
    payload = provider._apply_effort({"max_tokens": 8192})
    assert payload["output_config"] == {"effort": "high"}
    # 关键：绝不能同时发旧的 thinking.budget_tokens，那是 400 的来源
    assert "thinking" not in payload


@pytest.mark.parametrize(
    "model_name", ["claude-sonnet-4-5", "claude-3-7-sonnet", "claude-haiku-4-5"]
)
def test_anthropic_legacy_models_use_budget_tokens(monkeypatch, model_name):
    provider = _anthropic_provider(monkeypatch, "high", 24576, max_tokens=32000, model_name=model_name)
    payload = provider._apply_effort({"max_tokens": 32000})
    assert payload["thinking"]["type"] == "enabled"
    assert "output_config" not in payload


def test_anthropic_new_model_maps_minimal_to_low(monkeypatch):
    """Anthropic 没有 minimal 档，最低是 low。"""
    provider = _anthropic_provider(monkeypatch, "minimal", 1024, model_name="claude-opus-5")
    assert provider._apply_effort({"max_tokens": 8192})["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_anthropic_new_model_passes_top_tiers_through(monkeypatch, effort):
    """xhigh/max 是 Anthropic 原生档，不该被降档。"""
    provider = _anthropic_provider(monkeypatch, effort, 32768, model_name="claude-opus-5")
    assert provider._apply_effort({"max_tokens": 8192})["output_config"] == {"effort": effort}


def test_anthropic_new_model_auto_uses_adaptive(monkeypatch):
    """auto = 让模型自己决定，正是 adaptive 模式；且不该钉死 effort 档。"""
    provider = _anthropic_provider(monkeypatch, "auto", -1, model_name="claude-opus-4-8")
    payload = provider._apply_effort({"max_tokens": 8192})
    assert payload["thinking"] == {"type": "adaptive"}
    assert "output_config" not in payload


def test_anthropic_legacy_auto_omits_fields(monkeypatch):
    """旧模型没有 adaptive，auto 档的 -1 哨兵绝不能塞进 budget_tokens。"""
    provider = _anthropic_provider(monkeypatch, "auto", -1, model_name="claude-sonnet-4-5")
    payload = provider._apply_effort({"max_tokens": 8192})
    assert "thinking" not in payload
    assert "output_config" not in payload


@pytest.mark.parametrize("model_name", ["claude-fable-5", "claude-mythos-5"])
def test_anthropic_unclosable_models_get_low_instead_of_disabled(monkeypatch, model_name):
    """fable-5 / mythos-5 连 disabled 都收不了（400），none 只能降到最低档。"""
    provider = _anthropic_provider(monkeypatch, "none", 0, model_name=model_name)
    payload = provider._apply_effort({"max_tokens": 8192})
    assert payload["output_config"] == {"effort": "low"}
    assert "thinking" not in payload


def test_anthropic_strip_effort_fields_removes_both(monkeypatch):
    """代次判断是靠模型名猜的，猜错要能摘掉字段重试——否则整个别名不可用。"""
    provider = _anthropic_provider(monkeypatch, "high", 24576, model_name="claude-opus-5")
    payload = provider._apply_effort({"max_tokens": 8192})
    err = RuntimeError("output_config.effort: unsupported for this model")
    assert provider._strip_effort_fields(payload, err, "chat") is True
    assert "output_config" not in payload and "thinking" not in payload


def test_anthropic_strip_effort_fields_ignores_unrelated_errors(monkeypatch):
    provider = _anthropic_provider(monkeypatch, "high", 24576, model_name="claude-opus-5")
    payload = provider._apply_effort({"max_tokens": 8192})
    assert provider._strip_effort_fields(payload, RuntimeError("rate limit"), "chat") is False
    # 不相关的错误不该顺手把配置摘掉
    assert "output_config" in payload
