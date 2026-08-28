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

import asyncio
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
- 示例：[happy] 今天天气真好！[laughing] 哈哈，[break] 你快出来看看～

指定某个词语的发音（仅中/英/日，用音素标记，不会被念出来）：

用 <|phoneme_start|>发音<|phoneme_end|> 替换那个词。只在多音字、易读错的
名字、需要特殊读法时用，别滥用：

- 中文：小写拼音+声调数字（1-4，轻声 5），一个字一对标记；标点留在标记外。
  例：重庆 → <|phoneme_start|>chong2<|phoneme_end|><|phoneme_start|>qing4<|phoneme_end|>
  例：只标一个字：请把这个字读作 <|phoneme_start|>hang2<|phoneme_end|>
- 英文：CMU Arpabet 音标（大写、空格分隔、元音带重读数字 0/1/2），一对标记只包一个词。
  例：The read endpoint → The <|phoneme_start|>R IY1 D<|phoneme_end|> endpoint
  例：SQL → <|phoneme_start|>EH1 S KY UW1 EH1 L<|phoneme_end|>
- 日文：OpenJTalk 风格罗马音，每个元音后紧跟音高数字（0 低 1 高），短词/短语一对标记。
  例：箸が（筷子）→ <|phoneme_start|>ha1shi0ga0<|phoneme_end|>（ha0shi1ga0 是"橋が"桥）
- 注意：中文标点、括号、空格都留在标记外；只包需要控制的那一个词/字"""

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

    # ------------------------------------------------------------------
    # RTC 实时通话：WebSocket 流式会话（wss://api.fish.audio/v1/tts/live）
    # ------------------------------------------------------------------

    def open_live_session(self, on_audio):
        """给 RTC 通话的流式会话：增量喂转写、音频块即时回调。

        用 Fish Audio 的 WebSocket TTS live 端点（MessagePack 事件协议）：
        start → text(增量) → flush/end → audio(块) → finish。
        转写碎片一到就喂（服务端按 chunk_length 自动切句合成），首字延迟
        可以压到几百毫秒——比"整句结束再 synthesize"低一个量级。
        """
        return _FishLiveSession(self, on_audio)


class _FishLiveSession:
    """fish_audio 的 RTC 流式会话实现（base.BaseTTSLiveSession 约定）。"""

    def __init__(self, provider: "Provider", on_audio):
        self._p = provider
        self._on_audio = on_audio
        self._ws = None
        self._session = None
        self._recv_task = None
        self._closed = False
        self._audio_bytes = 0

    async def _ensure_connected(self) -> bool:
        """惰性建连（首个 feed_text 才连；连不上回落由调用方处理）。"""
        if self._closed:
            return False
        if self._ws is not None and not self._ws.closed:
            return True
        import msgpack

        headers = {
            "Authorization": f"Bearer {self._p.api_key}",
            "Model": self._p.model,
        }
        url = self._p.base_url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{url}/v1/tts/live"
        timeout = aiohttp.ClientTimeout(total=None, connect=15)
        self._session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        try:
            self._ws = await self._session.ws_connect(
                url, headers=headers, proxy=self._p.proxy, max_msg_size=8 * 1024 * 1024,
            )
        except Exception as e:
            logger.error("fish_audio live WS 连接失败: %s", e)
            await self._session.close()
            self._session = None
            return False

        # start 事件：request 复用 HTTP TTS 参数；format 用 pcm（RTC 前端
        # 播放管线按 PCM16 24k 解析——Live 音频也是 24k，前端零改动兼容）
        start_req = {
            "text": "",
            "format": "pcm",
            "sample_rate": 24000,
            "latency": "balanced",
            "normalize": self._p.normalize,
            "chunk_length": 100,  # 最小可切句长度：尽早出声（0-100）
        }
        if self._p.reference_id:
            start_req["reference_id"] = self._p.reference_id
        await self._ws.send_bytes(msgpack.packb({"event": "start", "request": start_req}))

        # 接收循环：audio 事件 → on_audio 回调；finish → 收尾
        async def _recv():
            try:
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msgpack.unpackb(msg.data, raw=False)
                        if data.get("event") == "audio" and data.get("audio"):
                            audio = data["audio"]
                            if isinstance(audio, str):
                                import base64 as _b64
                                audio = _b64.b64decode(audio)
                            self._audio_bytes += len(audio)
                            await self._on_audio(audio)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                      aiohttp.WSMsgType.ERROR):
                        break
            except Exception as e:  # noqa: BLE001 - 接收循环异常只记日志
                if not self._closed:
                    logger.error("fish_audio live WS 接收异常: %s", e)

        self._recv_task = asyncio.get_running_loop().create_task(_recv())
        logger.info("fish_audio live 会话已建立 (model=%s ref=%s)",
                    self._p.model, self._p.reference_id or "默认")
        return True

    async def feed_text(self, text: str) -> None:
        if not text or self._closed:
            return
        if not await self._ensure_connected():
            return
        import msgpack

        try:
            await self._ws.send_bytes(msgpack.packb({"event": "text", "text": text}))
        except Exception as e:  # noqa: BLE001
            logger.warning("fish_audio live feed_text 失败: %s", e)

    async def flush(self) -> None:
        """句子边界：让服务端立即合成缓冲（降首字延迟）。"""
        if self._ws is None or self._ws.closed or self._closed:
            return
        import msgpack

        try:
            await self._ws.send_bytes(msgpack.packb({"event": "flush"}))
        except Exception as e:  # noqa: BLE001
            logger.warning("fish_audio live flush 失败: %s", e)

    async def end_turn(self) -> None:
        """本轮说完：flush 让剩余缓冲全部出声（连接保持给下一轮用）。"""
        await self.flush()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._ws is not None and not self._ws.closed:
                import msgpack
                await self._ws.send_bytes(msgpack.packb({"event": "stop"}))
        except Exception:  # noqa: BLE001
            pass
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._session is not None and not self._session.closed:
            await self._session.close()
        logger.info("fish_audio live 会话已关闭 (audio=%dKB)", self._audio_bytes // 1024)
