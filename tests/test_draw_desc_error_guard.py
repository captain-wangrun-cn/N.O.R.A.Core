'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-16 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""draw_desc 被安全策略拦截时，不能把错误提示当成生图提示词。

历史现象（实测日志）：
    [OpenAI chat/draw_desc] 内容被安全策略拦截 finish_reason=prohibited_content
    draw_desc 未输出 [DRAW_PROMPT] 区块，整段文本作为提示词。
    生图开始: prompt=Error: 模型因内容安全策略拒绝生成（finish_reason=...）

根因：provider 失败时**返回一段错误文本**而不是抛异常，`_resolve_draw_prompt`
的「没包 [DRAW_PROMPT] 就整段当提示词」兜底分支于是把那句错误说明喂进了文生图模型。
"""
import asyncio

import pytest

from brain.interface import BaseLLM
from core.appearance_gen import AppearanceGenMixin


BLOCKED_TEXT = (
    "Error: 模型因内容安全策略拒绝生成（finish_reason=prohibited_content）。"
    "检查输入里是否有触发未成年人保护、暴力、色情等策略的内容，"
    "或换一个审查更宽松的端点/模型。"
)


class _FakeClient:
    def __init__(self, reply, last_error=None):
        self._reply = reply
        self.last_error = last_error

    async def chat(self, system_prompt, user_prompt, history):
        return self._reply


class _Harness(AppearanceGenMixin):
    """只跑 _resolve_draw_prompt，模板/外观读取全部打桩。"""

    def _current_place_segment_messages(self, identity):
        return []


@pytest.fixture
def harness(monkeypatch):
    import core.appearance_gen as mod

    monkeypatch.setattr(mod, "render_template", lambda *a, **kw: "rendered")
    monkeypatch.setattr(mod, "_read_file_safe", lambda *a, **kw: "")
    monkeypatch.setattr(mod.appearance_lib, "read_appearance_text", lambda: "外观描述")
    monkeypatch.setattr(mod.appearance_lib, "read_style_text", lambda: "风格")
    monkeypatch.setattr(mod.appearance_lib, "describe_manifest_for_prompt", lambda: "face.png")
    monkeypatch.setattr(mod.appearance_lib, "list_reference_files", lambda: ["face.png"])
    return _Harness()


def _run(harness, client, monkeypatch):
    monkeypatch.setattr("brain.llm.get_llm_client", lambda model_alias: client)
    return asyncio.run(harness._resolve_draw_prompt("tg:1", "自拍一张", None))


# --- 核心回归 ---

def test_blocked_error_text_is_not_used_as_prompt(harness, monkeypatch):
    """最关键的一条：拦截产生的错误文本必须 raise，不能变成 prompt。"""
    client = _FakeClient(BLOCKED_TEXT, last_error="blocked:prohibited_content")
    with pytest.raises(RuntimeError) as exc:
        _run(harness, client, monkeypatch)
    assert "draw_desc 调用失败" in str(exc.value)


def test_error_text_blocked_even_without_last_error(harness, monkeypatch):
    """last_error 没记上时，Error: 前缀是第二道闸门（provider 各写各的）。"""
    client = _FakeClient(BLOCKED_TEXT, last_error=None)
    with pytest.raises(RuntimeError):
        _run(harness, client, monkeypatch)


def test_last_error_blocks_even_when_text_looks_usable(harness, monkeypatch):
    """反过来：文本看着像正常提示词，但 last_error 有值就仍要拒。"""
    client = _FakeClient(
        "[DRAW_PROMPT]a girl smiling at the camera[/DRAW_PROMPT]",
        last_error="empty_content",
    )
    with pytest.raises(RuntimeError):
        _run(harness, client, monkeypatch)


def test_generic_provider_failure_text_is_blocked(harness, monkeypatch):
    """OpenAI 的通用失败文案同样不能当提示词。"""
    client = _FakeClient(
        "Error: Sorry, I encountered an issue processing your request.",
        last_error="exception:boom",
    )
    with pytest.raises(RuntimeError):
        _run(harness, client, monkeypatch)


# --- 正常路径不能被误伤 ---

def test_normal_prompt_block_still_works(harness, monkeypatch):
    client = _FakeClient(
        "[DRAW_PROMPT]a girl in a white sweater, soft light[/DRAW_PROMPT]\n"
        "[DRAW_REFS]face.png[/DRAW_REFS]"
    )
    prompt, refs = _run(harness, client, monkeypatch)
    assert prompt == "a girl in a white sweater, soft light"
    assert refs == ["face.png"]


def test_unwrapped_output_still_falls_back_to_whole_text(harness, monkeypatch):
    """模型没包区块但内容正常时，原有的「整段当提示词」兜底要保留。"""
    client = _FakeClient("a girl waving at the camera, warm sunlight")
    prompt, refs = _run(harness, client, monkeypatch)
    assert prompt == "a girl waving at the camera, warm sunlight"
    assert refs == []


def test_empty_reply_still_raises(harness, monkeypatch):
    client = _FakeClient("   ")
    with pytest.raises(RuntimeError) as exc:
        _run(harness, client, monkeypatch)
    assert "返回空结果" in str(exc.value)


# --- BaseLLM 侧的判别信号 ---

def test_is_error_result_recognizes_prefix():
    assert BaseLLM.is_error_result(BLOCKED_TEXT)
    assert BaseLLM.is_error_result("  Error: leading whitespace tolerated")
    assert not BaseLLM.is_error_result("a girl smiling")
    assert not BaseLLM.is_error_result("")
    assert not BaseLLM.is_error_result(None)


def test_check_finish_reason_records_last_error():
    """错误文本和 last_error 必须同时产生，否则调用方只能靠匹配字符串。"""

    class _Probe(BaseLLM):
        async def chat(self, *a, **kw):
            return ""

        async def chat_stream(self, *a, **kw):
            yield {}

    probe = _Probe()
    assert probe.last_error is None

    err = probe.check_finish_reason_and_log("prohibited_content", {}, None, context="t")
    assert err and BaseLLM.is_error_result(err)
    assert probe.last_error == "blocked:prohibited_content"

    # 正常终止不该留下错误状态
    probe._begin_call()
    assert probe.check_finish_reason_and_log("stop", {}, "hi", context="t") is None
    assert probe.last_error is None


def test_truncation_is_not_an_error():
    """截断只是不完整，正文仍可用——不能当失败处理。"""

    class _Probe(BaseLLM):
        async def chat(self, *a, **kw):
            return ""

        async def chat_stream(self, *a, **kw):
            yield {}

    probe = _Probe()
    assert probe.check_finish_reason_and_log("length", {}, "half a sentence", context="t") is None
    assert probe.last_error is None
