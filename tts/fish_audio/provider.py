'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-27 00:00:00
Copyright © WR（captain-wangrun-cn）All rights reserved

Fish Audio TTS provider。

API 契约（https://api.fish.audio/openapi.json + docs.fish.audio，2026-08 实测确认）：

    POST {base_url}/v1/tts
    Authorization: Bearer <api_key>
    Model: <model>                    ← 模型经 header 选择，不在 body 里
    Content-Type: application/json

    body: {
      "text": ...,                   # 必填
      "reference_id": ...,           # 声音模型 id（fish.audio 网页创建；GET /model?self=true 可列出）
      "format": "opus",              # mp3 / wav / pcm / opus
      "mp3_bitrate": 128,            # 仅 mp3
      "latency": "balanced",         # normal 质量更好 / balanced 更快 / low 最快
      "normalize": true              # 展开数字/日期，朗读更自然
    }

    200 → 音频二进制流；错误 → JSON {"status": int, "message": str}

S2 系模型（s2.1-pro / s2-pro）支持方括号情绪/副语言标记（写进文本里，
模型不会念出来），完整速查见 get_text_guidance()。
'''
from __future__ import annotations

import json
import logging
from typing import Any, Dict

import aiohttp

from tts.base import BaseTTSProvider

logger = logging.getLogger(__name__)

# format → (落盘扩展名, mime)。opus 用 .oga（Ogg 容器）而不是 .opus：
# Telegram 的语音条（send_voice）吃 ogg/opus 容器扩展名；.opus 不在它的
# voice 扩展集里，会退化成 document。
_EXT_BY_FORMAT = {
    "opus": (".oga", "audio/ogg"),
    "mp3": (".mp3", "audio/mpeg"),
    "wav": (".wav", "audio/wav"),
}

_DEFAULT_BASE_URL = "https://api.fish.audio"
_REQUEST_TIMEOUT_SECONDS = 120


class Provider(BaseTTSProvider):
    """Fish Audio 语音合成。"""

    name = "fish_audio"

    def __init__(self, cfg: Dict[str, Any] | None = None):
        super().__init__(cfg)
        self.api_key = str(self.cfg.get("api_key", "") or "").strip()
        self.reference_id = str(self.cfg.get("reference_id", "") or "").strip()
        self.model = str(self.cfg.get("model", "") or "").strip() or "s2.1-pro"
        self.base_url = str(self.cfg.get("base_url", "") or "").strip().rstrip("/") or _DEFAULT_BASE_URL
        self.latency = str(self.cfg.get("latency", "") or "").strip() or "balanced"
        self.format = str(self.cfg.get("format", "") or "").strip().lower() or "opus"
        self.normalize = bool(self.cfg.get("normalize", True))
        try:
            self.mp3_bitrate = int(self.cfg.get("mp3_bitrate", 128))
        except (TypeError, ValueError):
            self.mp3_bitrate = 128
        # 留空则 trust_env 继承全局 network.proxy 写进环境变量的代理
        self.proxy = str(self.cfg.get("proxy", "") or "").strip() or None

        if self.format not in _EXT_BY_FORMAT:
            raise ValueError(
                f"fish_audio 不支持的 format '{self.format}'（可选: {', '.join(_EXT_BY_FORMAT)}；"
                "pcm 无音频容器、无法直接作为语音消息发送）"
            )
        if not self.api_key:
            raise ValueError("fish_audio 缺少 api_key（编辑 tts/fish_audio/config.json）")

    def get_text_guidance(self) -> str:
        return """当前语音引擎支持情绪与副语言标记（写在语音文本里，不会被念出来）：

- 句首情绪：[happy] [sad] [angry] [excited] [calm] [nervous] [surprised] [embarrassed] [proud] [relaxed] [grateful] [curious] [sarcastic] [hopeful] [disappointed] [regretful] [lonely] [bored] [determined] 等；也支持自然语言描述如 [warm and happy]、强度修饰如 [slightly sad] / [very excited]
- 语气/音效（可放任意位置）：[whispering] [shouting] [screaming] [soft tone] [emphasis] [laughing] [chuckling] [sobbing] [sighing] [gasping] [yawning] [clear throat]
- 停顿：[break] 短停顿、[long-break] 长停顿；口语填充词 um / uh / 嗯 / 啊 也可直接写
- 每句建议最多组合 3 种标记；情绪可以按句子递进变化
- 示例：[happy] 今天天气真好！[laughing] 哈哈，[break] 你快出来看看～"""

    async def synthesize(self, text: str) -> Dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise RuntimeError("语音文本为空")

        body: Dict[str, Any] = {
            "text": text,
            "format": self.format,
            "latency": self.latency,
            "normalize": self.normalize,
        }
        if self.reference_id:
            body["reference_id"] = self.reference_id
        if self.format == "mp3":
            body["mp3_bitrate"] = self.mp3_bitrate

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Model": self.model,
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(
                    f"{self.base_url}/v1/tts",
                    json=body,
                    headers=headers,
                    proxy=self.proxy,
                ) as resp:
                    if resp.status != 200:
                        # 错误响应是 JSON {"status", "message"}；但也可能是纯文本，都兜住
                        raw = await resp.read()
                        message = ""
                        try:
                            err_json = json.loads(raw.decode("utf-8", errors="replace"))
                            if isinstance(err_json, dict):
                                message = str(err_json.get("message", ""))
                        except (json.JSONDecodeError, ValueError):
                            pass
                        message = message or raw.decode("utf-8", errors="replace")[:200]
                        raise RuntimeError(
                            f"Fish Audio TTS 失败 HTTP {resp.status}: {message}"
                        )
                    audio = await resp.read()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Fish Audio TTS 网络错误: {e}") from e

        if not audio:
            raise RuntimeError("Fish Audio TTS 返回空音频")

        ext, mime = _EXT_BY_FORMAT[self.format]
        logger.info(
            "fish_audio 合成完成: %d bytes (model=%s ref=%s format=%s)",
            len(audio), self.model, self.reference_id or "默认", self.format,
        )
        return {"bytes": audio, "ext": ext, "mime_type": mime}
