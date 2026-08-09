"""Civitai provider 的请求构造与响应解析。

不打真实网络：aiohttp 的 session 被替换成假对象，只校验
「我们发出去的 JSON」和「我们如何解析回来的 JSON」是否符合
Civitai orchestration 文档的契约。
"""

import asyncio
import base64
import functools
import json
from typing import Any, Dict, List

import pytest


def asyncio_test(fn):
    """本仓库没装 pytest-asyncio，自己把协程跑起来。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _install_fake_config(monkeypatch, model: str, engine=None):
    import config

    monkeypatch.setattr(config, "get_model_provider", lambda alias="draw": "civitai_main")
    monkeypatch.setattr(config, "get_api_key", lambda provider=None: "test-token")
    monkeypatch.setattr(config, "get_base_url", lambda provider=None: "https://orchestration.civitai.com")
    monkeypatch.setattr(config, "get_model_name", lambda alias="draw": model)
    monkeypatch.setattr(
        config, "get_provider_option",
        lambda provider=None, key="": engine if key == "engine" else None,
    )


def _ref(data: bytes = b"\x89PNG\r\n\x1a\nfake", mime: str = "image/png") -> Dict[str, Any]:
    return {
        "mime_type": mime,
        "bytes": data,
        "base64": base64.b64encode(data).decode("utf-8"),
    }


class _FakeResponse:
    def __init__(self, status=200, payload=None, body=b"", headers=None):
        self.status = status
        self._payload = payload
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return json.dumps(self._payload) if self._payload is not None else ""

    async def json(self):
        return self._payload

    async def read(self):
        return self._body


class _FakeSession:
    """记录所有请求，按预设脚本依次返回响应。"""

    def __init__(self, responses: List[_FakeResponse]):
        self._responses = list(responses)
        self.posts: List[Dict[str, Any]] = []
        self.gets: List[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)

    def get(self, url, headers=None):
        self.gets.append(url)
        return self._responses.pop(0)


def _patch_session(monkeypatch, session: _FakeSession):
    from brain.providers import civitai

    monkeypatch.setattr(
        civitai.aiohttp, "ClientSession", lambda *a, **kw: session
    )


def _succeeded(url="https://img.civitai.com/signed.jpeg"):
    return {
        "status": "succeeded",
        "steps": [{
            "name": "0",
            "$type": "imageGen",
            "status": "succeeded",
            "output": {"images": [{"id": "blob_x", "url": url}]},
        }],
    }


@asyncio_test
async def test_air_model_builds_editimage_request(monkeypatch):
    """AIR 模型 → engine=sdcpp + ecosystem 从 urn 推断 + operation=editImage。"""
    _install_fake_config(monkeypatch, "urn:air:sdxl:checkpoint:civitai:101055@128078")
    from brain.providers.civitai import CivitaiProvider

    session = _FakeSession([
        _FakeResponse(200, _succeeded()),
        _FakeResponse(200, body=b"IMAGEBYTES", headers={"Content-Type": "image/jpeg"}),
    ])
    _patch_session(monkeypatch, session)

    provider = CivitaiProvider(model_alias="draw")
    result = await provider.generate_image("cinematic portrait", reference_images=[_ref()])

    body = session.posts[0]["json"]
    step_input = body["steps"][0]["input"]

    assert body["steps"][0]["$type"] == "imageGen"
    assert step_input["operation"] == "editImage"
    assert step_input["engine"] == "sdcpp"
    assert step_input["ecosystem"] == "sdxl"
    assert step_input["model"] == "urn:air:sdxl:checkpoint:civitai:101055@128078"
    assert step_input["prompt"] == "cinematic portrait"
    # 参考图必须以 data URI 进 images
    assert step_input["images"][0].startswith("data:image/png;base64,")
    # 文档明确：clipSkip 在 sdxl 上会 400，任何情况下都不该出现
    assert "clipSkip" not in step_input
    assert session.posts[0]["headers"]["Authorization"] == "Bearer test-token"
    assert "wait=" in session.posts[0]["url"]

    assert result["bytes"] == b"IMAGEBYTES"
    assert result["mime_type"] == "image/jpeg"


@asyncio_test
async def test_hosted_model_uses_model_name_as_engine(monkeypatch):
    """托管模型（grok）没有 AIR urn，engine 用模型串本身，且不带 ecosystem。"""
    _install_fake_config(monkeypatch, "grok")
    from brain.providers.civitai import CivitaiProvider

    session = _FakeSession([
        _FakeResponse(200, _succeeded()),
        _FakeResponse(200, body=b"X", headers={"Content-Type": "image/png"}),
    ])
    _patch_session(monkeypatch, session)

    provider = CivitaiProvider(model_alias="draw")
    await provider.generate_image("make it winter", reference_images=[_ref()])

    step_input = session.posts[0]["json"]["steps"][0]["input"]
    assert step_input["engine"] == "grok"
    assert "ecosystem" not in step_input
    assert step_input["operation"] == "editImage"


@asyncio_test
async def test_missing_reference_images_raises(monkeypatch):
    """强制图生图：没有参考图直接报错，绝不静默降级成文生图。"""
    _install_fake_config(monkeypatch, "urn:air:sdxl:checkpoint:civitai:1@2")
    from brain.providers.civitai import CivitaiProvider

    session = _FakeSession([])
    _patch_session(monkeypatch, session)

    provider = CivitaiProvider(model_alias="draw")
    with pytest.raises(RuntimeError, match="图生图"):
        await provider.generate_image("anything", reference_images=None)

    # 一个请求都不该发出去
    assert session.posts == []


@asyncio_test
async def test_reference_images_truncated_to_three(monkeypatch):
    """超过 3 张会被 Civitai 判 400 images maxItems，本地先截断。"""
    _install_fake_config(monkeypatch, "grok")
    from brain.providers.civitai import CivitaiProvider

    session = _FakeSession([
        _FakeResponse(200, _succeeded()),
        _FakeResponse(200, body=b"X", headers={"Content-Type": "image/png"}),
    ])
    _patch_session(monkeypatch, session)

    provider = CivitaiProvider(model_alias="draw")
    await provider.generate_image("x", reference_images=[_ref() for _ in range(5)])

    assert len(session.posts[0]["json"]["steps"][0]["input"]["images"]) == 3


@asyncio_test
async def test_202_then_polls_until_succeeded(monkeypatch):
    """202 表示没跑完，按 workflow id 轮询到终态。"""
    _install_fake_config(monkeypatch, "grok")
    from brain.providers.civitai import CivitaiProvider

    session = _FakeSession([
        _FakeResponse(202, {"id": "wf_abc", "status": "processing"}),
        _FakeResponse(200, {"id": "wf_abc", "status": "processing"}),
        _FakeResponse(200, _succeeded()),
        _FakeResponse(200, body=b"DONE", headers={"Content-Type": "image/png"}),
    ])
    _patch_session(monkeypatch, session)

    provider = CivitaiProvider(model_alias="draw")
    result = await provider.generate_image("x", reference_images=[_ref()])

    # 两次轮询 + 最后一次下图
    polls = [u for u in session.gets if "/v2/consumer/workflows/" in u]
    assert len(polls) == 2
    assert polls[0].endswith("/v2/consumer/workflows/wf_abc?wait=60")
    assert session.gets[-1] == "https://img.civitai.com/signed.jpeg"
    assert result["bytes"] == b"DONE"


@asyncio_test
async def test_failed_workflow_raises(monkeypatch):
    _install_fake_config(monkeypatch, "grok")
    from brain.providers.civitai import CivitaiProvider

    session = _FakeSession([
        _FakeResponse(200, {"id": "wf_x", "status": "failed"}),
    ])
    _patch_session(monkeypatch, session)

    provider = CivitaiProvider(model_alias="draw")
    with pytest.raises(RuntimeError, match="未成功"):
        await provider.generate_image("x", reference_images=[_ref()])


@asyncio_test
async def test_chat_not_supported(monkeypatch):
    """Civitai 不是对话端点，绑错别名要明确报错。"""
    _install_fake_config(monkeypatch, "grok")
    from brain.providers.civitai import CivitaiProvider

    provider = CivitaiProvider(model_alias="draw")
    with pytest.raises(NotImplementedError):
        await provider.chat("s", "u", [])

