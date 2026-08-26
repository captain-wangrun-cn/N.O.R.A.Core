"""TTS 子系统测试：registry 发现 / config_utils 层叠 / fish_audio provider / SpeechGenMixin。

不依赖真实 API key 与网络：fish_audio 的 synthesize 用 mock aiohttp session；
registry 的自定义 provider 发现用 tmp 目录里动态生成的 tts/<name>/provider.py
（直接写进真 tts/ 目录会污染仓库，这里 monkeypatch TTS_ROOT）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts import config_utils
from tts.base import BaseTTSProvider


# ---------------------------------------------------------------------------
# config_utils
# ---------------------------------------------------------------------------

def test_provider_dir_finds_builtin_fish_audio():
    pdir = config_utils.provider_dir("fish_audio")
    assert pdir is not None
    assert (pdir / "provider.py").is_file()


def test_provider_dir_rejects_traversal_names():
    # 名字里带路径分隔符 / 隐藏前缀的一律拒绝，防目录穿越
    assert config_utils.provider_dir("../adapters") is None
    assert config_utils.provider_dir(".git") is None
    assert config_utils.provider_dir("") is None
    assert config_utils.provider_dir("no_such_provider") is None


def test_list_provider_names_skips_underscore_dirs():
    names = config_utils.list_provider_names()
    assert "fish_audio" in names
    assert "_template" not in names


def test_load_provider_config_layering(tmp_path: Path):
    """config.json 覆盖 config.example.json 覆盖 defaults。"""
    pdir = tmp_path / "demo"
    pdir.mkdir()
    (pdir / "config.example.json").write_text(
        json.dumps({"api_key": "YOUR_KEY", "format": "opus", "latency": "normal"}),
        encoding="utf-8",
    )
    (pdir / "config.json").write_text(
        json.dumps({"api_key": "real-key", "latency": "balanced"}),
        encoding="utf-8",
    )

    cfg = config_utils.load_provider_config(pdir, defaults={"format": "mp3"})
    assert cfg["api_key"] == "real-key"      # config.json 覆盖 example
    assert cfg["latency"] == "balanced"      # config.json 覆盖 example
    assert cfg["format"] == "opus"           # example 覆盖 defaults


def test_load_provider_config_without_any_file(tmp_path: Path):
    pdir = tmp_path / "empty"
    pdir.mkdir()
    cfg = config_utils.load_provider_config(pdir, defaults={"api_key": "x"})
    assert cfg == {"api_key": "x"}


# ---------------------------------------------------------------------------
# registry：动态加载（monkeypatch TTS_ROOT 指向 tmp 目录）
# ---------------------------------------------------------------------------

GOOD_PROVIDER = '''
from tts.base import BaseTTSProvider

class Provider(BaseTTSProvider):
    name = "good"

    async def synthesize(self, text):
        return {"bytes": b"fake-audio", "ext": ".oga", "mime_type": "audio/ogg"}

    def get_text_guidance(self):
        return "支持 [test-emotion] 标记"
'''

BROKEN_PROVIDER = "syntax error ((( not valid python"

NO_SUBCLASS_PROVIDER = "X = 123\n"


def _make_custom_tts_root(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "custom_tts"
    root.mkdir()
    for name, content in files.items():
        pdir = root / name
        pdir.mkdir()
        (pdir / "provider.py").write_text(content, encoding="utf-8")
    return root


def test_registry_loads_custom_provider(tmp_path, monkeypatch):
    root = _make_custom_tts_root(tmp_path, {"good": GOOD_PROVIDER})
    monkeypatch.setattr(config_utils, "TTS_ROOT", root)

    from tts.registry import get_provider_class
    cls = get_provider_class("good")
    assert cls is not None
    assert cls.__name__ == "Provider"


def test_registry_skips_broken_provider(tmp_path, monkeypatch):
    root = _make_custom_tts_root(
        tmp_path,
        {"broken": BROKEN_PROVIDER, "nosub": NO_SUBCLASS_PROVIDER, "good": GOOD_PROVIDER},
    )
    monkeypatch.setattr(config_utils, "TTS_ROOT", root)

    from tts.registry import get_provider_class
    assert get_provider_class("broken") is None   # 语法错误只跳过
    assert get_provider_class("nosub") is None    # 没有子类只跳过
    assert get_provider_class("good") is not None  # 不影响其它 provider


def test_registry_build_injects_folder_config(tmp_path, monkeypatch):
    root = _make_custom_tts_root(tmp_path, {"good": GOOD_PROVIDER})
    (root / "good" / "config.example.json").write_text(
        json.dumps({"api_key": "from-example"}), encoding="utf-8"
    )
    (root / "good" / "config.json").write_text(
        json.dumps({"api_key": "from-config"}), encoding="utf-8"
    )
    monkeypatch.setattr(config_utils, "TTS_ROOT", root)

    from tts.registry import build_tts_provider
    p = build_tts_provider("good")
    assert p.cfg["api_key"] == "from-config"
    # name 与文件夹名不一致时以文件夹名为准
    assert p.name == "good"


def test_registry_build_unknown_provider_raises():
    from tts.registry import build_tts_provider
    with pytest.raises(ValueError):
        build_tts_provider("definitely_not_exists")


def test_tts_available_reflects_config(monkeypatch):
    import config as config_mod
    from tts import registry

    monkeypatch.setattr(
        config_mod, "get_tts_config", lambda: {"provider": "fish_audio"}
    )
    assert registry.tts_available() is True

    monkeypatch.setattr(config_mod, "get_tts_config", lambda: {})
    assert registry.tts_available() is False

    # 配置了不存在的 provider 也不算可用
    monkeypatch.setattr(
        config_mod, "get_tts_config", lambda: {"provider": "ghost"}
    )
    assert registry.tts_available() is False


def test_get_active_tts_guidance_returns_provider_guidance(monkeypatch):
    import config as config_mod
    from tts import registry

    monkeypatch.setattr(
        config_mod, "get_tts_config", lambda: {"provider": "fish_audio"}
    )
    guidance = registry.get_active_tts_guidance()
    assert "[happy]" in guidance

    # 未配置时返回空串不抛异常
    monkeypatch.setattr(config_mod, "get_tts_config", lambda: {})
    assert registry.get_active_tts_guidance() == ""


# ---------------------------------------------------------------------------
# fish_audio provider
# ---------------------------------------------------------------------------

def test_fish_audio_build_and_payload_defaults():
    from tts.fish_audio.provider import Provider

    p = Provider({"api_key": "sk-test"})
    assert p.model == "s2.1-pro"
    assert p.format == "opus"
    assert p.latency == "balanced"
    assert p.name == "fish_audio"
    # 指南必须提到情绪标记（会注入前脑）
    assert "[happy]" in p.get_text_guidance()


def test_fish_audio_rejects_missing_key_and_bad_format():
    from tts.fish_audio.provider import Provider

    with pytest.raises(ValueError):
        Provider({})
    with pytest.raises(ValueError):
        Provider({"api_key": "k", "format": "pcm"})


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body


class _FakePostContext:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """记录请求参数并返回预设响应。"""

    def __init__(self, resp: _FakeResponse):
        self._resp = resp
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakePostContext(self._resp)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_fish_audio_synthesize_success(monkeypatch):
    from tts.fish_audio import provider as fa

    p = fa.Provider({"api_key": "sk-test", "reference_id": "voice-123"})
    fake = _FakeSession(_FakeResponse(200, b"OPUS-AUDIO-BYTES"))
    captured: dict = {}

    real_client_session = fa.aiohttp.ClientSession

    class _SessionShim(real_client_session):  # type: ignore[misc, valid-type]
        def __new__(cls, *args, **kwargs):
            captured.update(kwargs)
            return fake

    monkeypatch.setattr(fa.aiohttp, "ClientSession", _SessionShim)

    result = asyncio.run(p.synthesize("[happy] 你好"))
    assert result["bytes"] == b"OPUS-AUDIO-BYTES"
    assert result["ext"] == ".oga"
    assert result["mime_type"] == "audio/ogg"

    call = fake.calls[0]
    assert call["url"] == "https://api.fish.audio/v1/tts"
    # 认证 Bearer + 模型经 Model header（不在 body）
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["headers"]["Model"] == "s2.1-pro"
    assert call["json"]["text"] == "[happy] 你好"
    assert call["json"]["reference_id"] == "voice-123"
    assert call["json"]["format"] == "opus"
    assert "model" not in call["json"]


def test_fish_audio_synthesize_error_raises(monkeypatch):
    from tts.fish_audio import provider as fa

    p = fa.Provider({"api_key": "sk-test"})
    err_body = json.dumps({"status": 401, "message": "Invalid API key"}).encode()
    fake = _FakeSession(_FakeResponse(401, err_body))

    real_client_session = fa.aiohttp.ClientSession

    class _SessionShim(real_client_session):  # type: ignore[misc, valid-type]
        def __new__(cls, *args, **kwargs):
            return fake

    monkeypatch.setattr(fa.aiohttp, "ClientSession", _SessionShim)

    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(p.synthesize("hi"))


# ---------------------------------------------------------------------------
# SpeechGenMixin
# ---------------------------------------------------------------------------

class _FakeSpeechController:
    """SpeechGenMixin 的宿主假体：只实现 mixin 需要的属性与方法。"""

    def __init__(self):
        from core.speech_gen import SpeechGenMixin
        # 动态挂上 mixin 方法（不真走 NoraController 的巨大 __init__）
        for name in ("start_voice_task", "cancel_voice_task", "_generate_and_send_voice"):
            setattr(self, name, getattr(SpeechGenMixin, name).__get__(self))
        self.voice_gen_tasks = {}
        self.sent: list[tuple[str, str]] = []

    async def _send_platform_message(self, key: str, text: str, **kwargs):
        self.sent.append((key, text))
        return ["1"]


class _OkProvider(BaseTTSProvider):
    name = "fake_tts"

    def __init__(self, cfg=None):
        super().__init__(cfg)

    async def synthesize(self, text: str):
        return {"bytes": b"FAKE-OGG", "ext": ".oga", "mime_type": "audio/ogg"}


def test_speech_gen_mixin_generates_and_sends(monkeypatch, tmp_path):
    from core import speech_gen
    from tts import registry

    monkeypatch.setattr(registry, "tts_available", lambda: True)
    monkeypatch.setattr(registry, "build_tts_provider", lambda: _OkProvider())
    monkeypatch.setattr(
        speech_gen, "_voice_save_dir", lambda: str(tmp_path / "voice")
    )

    async def scenario():
        ctl = _FakeSpeechController()
        ctl.start_voice_task("key1", "[happy] 你好呀")
        # 等任务真正跑完
        for t in list(ctl.voice_gen_tasks.values()):
            await t
        return ctl

    ctl = asyncio.run(scenario())

    assert len(ctl.sent) == 1
    key, text = ctl.sent[0]
    assert key == "key1"
    assert text.startswith("[voice: ")
    assert text.endswith(".oga]")
    path = text[len("[voice: "):-1]
    assert os.path.isfile(path) is True
    # 任务完成后自清理
    assert "key1" not in ctl.voice_gen_tasks


def test_speech_gen_mixin_strips_system_markers_and_truncates(monkeypatch):
    from core.speech_gen import MAX_VOICE_TEXT_CHARS, _VOICE_STRAY_MARKER_PATTERN

    # 系统标记剥掉，情绪标记保留
    text = "[happy] 你好 [SPLIT][NEED_BACKEND][reply:123] [break] 世界"
    cleaned = _VOICE_STRAY_MARKER_PATTERN.sub("", text).strip()
    assert "[happy]" in cleaned
    assert "[break]" in cleaned
    assert "SPLIT" not in cleaned
    assert "NEED_BACKEND" not in cleaned
    assert "reply" not in cleaned

    assert MAX_VOICE_TEXT_CHARS == 500


def test_speech_gen_mixin_skips_when_tts_unavailable(monkeypatch):
    from tts import registry

    monkeypatch.setattr(registry, "tts_available", lambda: False)

    ctl = _FakeSpeechController()
    ctl.start_voice_task("key1", "你好")  # 不应创建任务
    assert "key1" not in ctl.voice_gen_tasks
    assert ctl.sent == []


def test_speech_gen_mixin_cancel_replaces_old(monkeypatch):
    import asyncio as aio

    class _SlowProvider(BaseTTSProvider):
        name = "slow"

        async def synthesize(self, text: str):
            await aio.sleep(30)
            return {"bytes": b"x", "ext": ".oga", "mime_type": "audio/ogg"}

    from tts import registry

    monkeypatch.setattr(registry, "tts_available", lambda: True)
    monkeypatch.setattr(registry, "build_tts_provider", lambda: _SlowProvider())

    async def scenario():
        ctl = _FakeSpeechController()
        ctl.start_voice_task("key1", "第一条")
        old = ctl.voice_gen_tasks["key1"]
        # 新请求顶掉旧任务
        ctl.start_voice_task("key1", "第二条")
        assert ctl.voice_gen_tasks["key1"] is not old
        await aio.sleep(0.05)
        assert old.cancelled() is True or old.done()
        return ctl

    ctl = asyncio.run(scenario())
    # 清理
    ctl.cancel_voice_task("key1")


def test_controller_has_voice_gen_tasks_wired():
    """controller 已混入 SpeechGenMixin 并初始化 voice_gen_tasks（源码级断言）。"""
    import inspect
    from core.controller import NoraController
    from core.speech_gen import SpeechGenMixin

    assert SpeechGenMixin in NoraController.__mro__
    src = inspect.getsource(NoraController.__init__)
    assert "voice_gen_tasks" in src
    # shutdown 也取消语音任务
    shutdown_src = inspect.getsource(NoraController.shutdown)
    assert "cancel_voice_task" in shutdown_src
