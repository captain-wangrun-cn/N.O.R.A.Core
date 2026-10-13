"""Gemini REST 路径上的模型名编码。

背景（2026-10-13 排查）：REST 路径拼 URL 时模型名用 `urllib.parse.quote(self.model_name)`
编码。`quote` 默认 `safe="/"`，模型别名里一旦带命名空间斜杠，斜杠会原样留在 URL 路径上，
自建网关按路径段解析后可能路由到完全不同的供应商，报 `unknown provider for ...` ——
日志看起来像模型名配错，实际是编码漏了。本文件锁住"斜杠必须被编码"这条，别改回 `quote(name)`。

注意：这只对"带命名空间前缀"的别名成立；普通的中文/方括号别名编码与否都能路由。
别把这条当成"视频轮卡死"那类问题的根因——排查时一度这么误判过。
"""

import urllib.parse

from brain.providers.gemini import _encode_model_for_url


def test_slash_in_model_name_is_encoded():
    assert _encode_model_for_url("vendor/model-variant-low") == (
        "vendor%2Fmodel-variant-low"
    )


def test_plain_model_name_is_untouched():
    assert _encode_model_for_url("gemini-pro-preview") == "gemini-pro-preview"


def test_cjk_and_brackets_survive_roundtrip():
    encoded = _encode_model_for_url("[通道A]gemini-pro-preview")
    assert "/" not in encoded
    assert urllib.parse.unquote(encoded) == "[通道A]gemini-pro-preview"


def test_empty_model_name_does_not_raise():
    assert _encode_model_for_url("") == ""
    assert _encode_model_for_url(None) == ""
