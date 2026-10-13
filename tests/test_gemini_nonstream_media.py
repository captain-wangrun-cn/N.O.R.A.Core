"""带媒体的非流式回合必须绕开 SDK，走 REST `:generateContent`。

背景（2026-10-13 生产事故）：`telegram:6112866979` 开着 `/set_stream`，视频轮因此走
`_chat_stream_wrapper` 的非流式分支 → `GeminiProvider.chat()`。而 `chat()` 当年只实现了
SDK 路径，`multimodal_videos` 参数收下后**从未使用**，于是撞进 `_video_stream_via_rest`
注释里早已点名的那个组合（SDK + 自定义 endpoint + 视频 inline_data 卡死）：
实测同一份 961KB mp4，REST 12.9s 返回，`chat()` 180s 不返回；线上等 SDK 重试超时
整整 600 秒，且 `chat()` 不吐 chunk，日志从 `Turn 1 模型: video` 起全静默。

流式路径早就绕开了 SDK，只有非流式漏了——**只修一边等于没修**，`/set_stream` 是 per-chat 的。
本文件锁住两边都走 REST，别让 `chat()` 退回 SDK。
"""

import inspect

from brain.providers.gemini import GeminiProvider


def _provider() -> GeminiProvider:
    """绕过 __init__（会真的去 configure SDK 客户端），只造一个够用的实例。"""
    p = object.__new__(GeminiProvider)
    p.model_alias = "video"
    p.model_name = "video-model-under-test"
    p.base_url = "https://gemini.invalid"
    p.api_key = "sk-test"
    p.effort = None
    p.temperature = None
    p.max_output_tokens = None
    p.last_error = None
    p.last_tool_calls = []
    p.last_usage = None
    return p


def test_chat_with_media_goes_through_rest_not_sdk():
    """源码级断言：`chat()` 命中媒体时在碰 SDK 之前就 return 到 REST 分支。"""
    src = inspect.getsource(GeminiProvider.chat)
    guard = "if multimodal_videos or multimodal_images:"
    assert guard in src, "chat() 缺少多模态 REST 前置分支，带视频会退回 SDK 卡死"
    assert "return await self._media_chat_via_rest(" in src
    # 前置分支必须排在 start_chat 之前——晚一步就已经进了 SDK。
    assert src.index(guard) < src.index("start_chat(")


def test_both_stream_and_nonstream_paths_bypass_sdk():
    """流式与非流式两条路都必须绕开 SDK，且都不带 tools 进 REST（视频轮 tools=[]）。"""
    stream_src = inspect.getsource(GeminiProvider._video_stream_via_rest)
    nonstream_src = inspect.getsource(GeminiProvider._media_chat_via_rest)
    for src in (stream_src, nonstream_src):
        assert "send_message_async" not in src
        assert ":generateContent" in src or "streamGenerateContent" in src


def test_media_parts_encodes_mime_and_base64():
    """payload 的 mime_type 优先（按魔数判出来的真实类型），缺字段才回落默认值。"""
    p = _provider()
    parts = p._media_parts(
        "描述",
        multimodal_images=[{"bytes": b"\xff\xd8\xff", "mime_type": "image/jpeg"}],
        multimodal_videos=[{"base64": "AAAA", "mime_type": "video/quicktime"}],
    )
    assert parts[0] == {"text": "描述"}
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert parts[2]["inlineData"]["mimeType"] == "video/quicktime"
    assert parts[2]["inlineData"]["data"] == "AAAA"

    # 缺 mime_type 时的兜底
    fallback = p._media_parts("", multimodal_videos=[{"bytes": b"x"}])
    assert fallback[0]["inlineData"]["mimeType"] == "video/mp4"


def test_media_parts_skips_payloads_without_bytes():
    p = _provider()
    parts = p._media_parts("hi", multimodal_videos=[{}, {"mime_type": "video/mp4"}])
    assert parts == [{"text": "hi"}]


def test_safety_settings_are_not_defined_inside_conditionals():
    """`chat()` 的 `if tools:` 分支曾引用方法内**从未定义**的 safety_settings。

    一旦带工具走非流式就是 NameError，被方法尾部的宽 except 吞掉，对外只表现
    为一句"处理请求时遇到了问题"。长期没炸只是因为视频轮固定 no_tools=True。
    收成模块级 helper 后，方法内不许再出现局部定义的 safety_settings。
    """
    src = inspect.getsource(GeminiProvider.chat_stream)
    assert "safety_settings = {" not in src, "safety_settings 又变回局部变量了"
    assert "_gemini_safety_settings()" in src or "self.model" in src


def test_rest_safety_settings_shape():
    from brain.providers.gemini import _gemini_rest_safety_settings

    settings = _gemini_rest_safety_settings()
    assert all(s["threshold"] == "BLOCK_NONE" for s in settings)
    assert {s["category"] for s in settings} == {
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    }
