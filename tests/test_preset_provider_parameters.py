"""逐请求采样及末尾消息必须进入真实 SDK/REST payload，不只测试 helper。"""

from types import SimpleNamespace as NS

import pytest

from brain.providers.openai import OpenAIProvider
from brain.providers.anthropic import AnthropicProvider
from brain.providers.gemini import GeminiProvider

TAIL = [{"role": "system", "content": "尾部规则"}]


def _openai(responses=False):
    p = object.__new__(OpenAIProvider)
    p.model = "fake-model"
    p.model_alias = "smart"
    p.max_output_tokens = 128
    p.temperature = 0.7
    p.effort = None
    p.use_responses_api = responses
    p._with_retry = lambda fn, **kwargs: fn()
    return p


async def _empty_stream():
    if False:
        yield None


@pytest.mark.anyio
@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_openai_actual_payload_sampling_and_tail(responses, streaming):
    p = _openai(responses)
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        if streaming:
            return _empty_stream()
        if responses:
            return NS(output=[NS(type="message", content=[NS(type="output_text", text="ok")])])
        return NS(choices=[NS(finish_reason="stop", message=NS(content="ok", tool_calls=[]))])
    p.client = NS(chat=NS(completions=NS(create=create)), responses=NS(create=create))
    kwargs = dict(system_prompt="人设", user_prompt="当前", history=[{"role": "user", "content": "历史"}],
                  temperature=0.0, top_p=0.95, request_tail=TAIL)
    if streaming:
        _ = [chunk async for chunk in p.chat_stream(**kwargs)]
    else:
        assert await p.chat(**kwargs) == "ok"
    payload = captured[0]
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0.95
    messages = payload["input" if responses else "messages"]
    assert messages[-1]["role"] == "system"
    assert "尾部规则" in str(messages[-1]["content"])
    assert "当前" in str(messages[-2]["content"])
    assert p.temperature == 0.7


class _ContextStream:
    async def __aenter__(self):
        return _empty_stream()
    async def __aexit__(self, *args):
        return False


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_actual_payload_and_tail(streaming):
    p = object.__new__(AnthropicProvider)
    p.model, p.model_alias = "fake-model", "smart"
    p.max_output_tokens, p.temperature, p.effort = 128, None, None
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn")
    def stream(**kwargs):
        captured.append(kwargs)
        return _ContextStream()
    p.client = NS(messages=NS(create=create, stream=stream))
    kwargs = dict(system_prompt="人设", user_prompt="当前", history=[], top_p=0.8, request_tail=TAIL)
    if streaming:
        _ = [chunk async for chunk in p.chat_stream(**kwargs)]
    else:
        assert await p.chat(**kwargs) == "ok"
    assert captured[0]["top_p"] == 0.8
    assert "temperature" not in captured[0]
    assert captured[0]["messages"][-1] == {"role": "user", "content": "尾部规则"}
    assert captured[0]["messages"][-2]["content"] == "当前"


@pytest.mark.parametrize("thinking", [None, {"type": "enabled"}, {"type": "adaptive"}])
def test_anthropic_sampling_constraints(thinking):
    p = object.__new__(AnthropicProvider)
    p.temperature = None
    payload = {"thinking": thinking} if thinking else {}
    p._apply_temperature(payload, temperature=1.5, top_p=0.8)
    if thinking:
        assert "temperature" not in payload and "top_p" not in payload
    else:
        assert payload == {"temperature": 1.0}
    assert p.temperature is None


def _gemini():
    p = object.__new__(GeminiProvider)
    p.model_name, p.model_alias = "fake-model", "smart"
    p.base_url, p.api_key = "https://gemini.invalid", "test-key"
    p.temperature, p.effort, p.max_output_tokens = 0.7, None, None
    return p


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_gemini_sdk_sampling_and_tail(streaming):
    p = _gemini()
    captured = []
    class Session:
        async def send_message_async(self, content, **kwargs):
            captured.append((content, kwargs))
            if streaming:
                return _empty_stream()
            return NS(parts=[], text="ok")
    p.model = NS(start_chat=lambda **kwargs: Session())
    kwargs = dict(system_prompt="人设", user_prompt="当前", history=[], temperature=0, top_p=0.95, request_tail=TAIL)
    if streaming:
        _ = [chunk async for chunk in p.chat_stream(**kwargs)]
    else:
        assert await p.chat(**kwargs) == "ok"
    content, parameters = captured[0]
    assert content[-1] == {"text": "尾部规则"}
    assert "当前" in content[0]["text"]
    assert parameters["generation_config"] == {"temperature": 0.0, "top_p": 0.95}
    assert p.temperature == 0.7


@pytest.mark.anyio
async def test_gemini_nonstream_media_rest_actual_body():
    p = _gemini()
    bodies = []
    async def rest(body, context):
        bodies.append(body)
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]}
    p._rest_generate_content = rest
    assert await p.chat("人设", "当前", [], multimodal_videos=[{"bytes": b"test", "mime_type": "video/mp4"}],
                        temperature=0, top_p=0.9, request_tail=TAIL) == "ok"
    assert bodies[0]["generationConfig"] == {"temperature": 0.0, "topP": 0.9}
    assert bodies[0]["contents"][-1] == {"role": "user", "parts": [{"text": "尾部规则"}]}


@pytest.mark.anyio
async def test_gemini_stream_media_forwards_request_parameters():
    p = _gemini()
    captured = []
    async def rest(**kwargs):
        captured.append(kwargs)
        yield {"type": "text", "content": "ok"}
    p._video_stream_via_rest = rest
    result = [chunk async for chunk in p.chat_stream("人设", "当前", [], multimodal_videos=[{"bytes": b"test"}],
                                                    temperature=0, top_p=0.9, request_tail=TAIL)]
    assert result[0]["content"] == "ok"
    assert captured[0]["generation_config"] == {"temperature": 0.0, "topP": 0.9}
    assert captured[0]["request_tail"] == TAIL


@pytest.mark.anyio
async def test_gemini_nonstream_without_preset_keeps_original_sampling():
    p = _gemini()
    captured = []
    class Session:
        async def send_message_async(self, content, **kwargs):
            captured.append(kwargs)
            return NS(parts=[], text="ok")
    p.model = NS(start_chat=lambda **kwargs: Session())
    assert await p.chat("人设", "当前", []) == "ok"
    assert "generation_config" not in captured[0]
    bodies = []
    async def rest(body, context):
        bodies.append(body)
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]}
    p._rest_generate_content = rest
    assert await p.chat("人设", "当前", [], multimodal_images=[{"bytes": b"x"}]) == "ok"
    assert "generationConfig" not in bodies[0]


def test_gemini_system_depth_is_not_assistant_speech():
    p = _gemini()
    assert p._convert_history(TAIL)[0]["role"] == "user"
    assert p._convert_history_to_rest(TAIL)[0]["role"] == "user"


@pytest.mark.anyio
@pytest.mark.parametrize("provider_class", [OpenAIProvider, AnthropicProvider])
async def test_tail_without_current_user_never_sends_null_content(provider_class):
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        if provider_class is OpenAIProvider:
            return NS(choices=[NS(finish_reason="stop", message=NS(content="ok", tool_calls=[]))])
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn")
    if provider_class is OpenAIProvider:
        p = _openai()
        p.client = NS(chat=NS(completions=NS(create=create)))
    else:
        p = object.__new__(AnthropicProvider)
        p.model, p.model_alias, p.max_output_tokens, p.effort, p.temperature = "fake", "smart", 128, None, None
        p.client = NS(messages=NS(create=create))
    assert await p.chat("人设", None, [{"role": "user", "content": "历史"}], request_tail=TAIL) == "ok"
    assert all(message["content"] is not None for message in captured[0]["messages"])
    assert len(captured[0]["messages"]) == (3 if provider_class is OpenAIProvider else 2)
