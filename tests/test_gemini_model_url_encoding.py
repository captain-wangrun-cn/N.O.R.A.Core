"""Gemini REST 路径上的模型名编码。

背景（2026-10-13 生产事故）：视频轮走的是 `_video_stream_via_rest` 拼出来的 REST URL，
模型名用 `urllib.parse.quote(self.model_name)` 编码。`quote` 默认 `safe="/"`，
模型别名里一旦带命名空间斜杠（`antigravity/gemini-3.1-pro-low`），斜杠会原样留在
URL 路径上，自建网关按路径段解析后路由到完全不同的供应商，报
`unknown provider for model gemini-pro-agent` —— 日志看起来像模型名配错，
实际是编码漏了。本文件锁住"斜杠必须被编码"这条，别改回 `quote(name)`。
"""

import urllib.parse

from brain.providers.gemini import _encode_model_for_url


def test_slash_in_model_name_is_encoded():
    assert _encode_model_for_url("antigravity/gemini-3.1-pro-low") == (
        "antigravity%2Fgemini-3.1-pro-low"
    )


def test_plain_model_name_is_untouched():
    assert _encode_model_for_url("gemini-3.8-flash-high") == "gemini-3.8-flash-high"


def test_cjk_and_brackets_are_encoded():
    encoded = _encode_model_for_url("[满血E]gemini-3.1-pro-preview")
    assert "/" not in encoded
    assert urllib.parse.unquote(encoded) == "[满血E]gemini-3.1-pro-preview"


def test_empty_model_name_does_not_raise():
    assert _encode_model_for_url("") == ""
    assert _encode_model_for_url(None) == ""
