"""Google Live API WebSocket 客户端封装（rtc 子系统的 Google 抽象层）。

设计文档：docs/architecture/rtc.md §5.2。

职责：
1. setup 组装（model / systemInstruction / voice / tools / 双转写 /
   sessionResumption / contextWindowCompression / VAD）
2. 双向音频中继：上游 PCM16 二进制 → base64 realtimeInput.audio；
   下行 serverContent.modelTurn inlineData → 二进制
3. 连接轮换：监听 goAway / 主动超时 → resumption handle 换轨（P0 先收事件打日志）
4. 转写累积：inputTranscription / outputTranscription 按 turn 聚合

手写 WS 客户端（aiohttp.ClientSession.ws_connect），不依赖 google-genai SDK：
音频字节中继不需要 SDK，纯 WS 更可控。

上游事件（serverContent / toolCall / goAway ...）通过回调分发，
本模块不感知 Nora 主系统，也不感知前端协议。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

# 输出音频固定 24kHz（官方文档），输入服务端可重采样（我们统一发 16k）
OUTPUT_SAMPLE_RATE = 24000
INPUT_SAMPLE_RATE = 16000

# 单条连接存活上限（官方 ~10 分钟后 goAway/ABORTED），保守提前主动轮换
CONNECTION_ROTATE_AFTER = 8 * 60.0


@dataclass
class LiveCallbacks:
    """live_client 向上分发事件的回调集合（全部可选）。"""

    # 下行音频块（PCM16 24kHz 原始字节）
    on_audio: Optional[Callable[[bytes], Awaitable[None]]] = None
    # 转写增量：who = "user" | "assistant"
    on_transcript: Optional[Callable[[str, str], Awaitable[None]]] = None
    # 插话打断（生成被取消，前端要清播放队列）
    on_interrupted: Optional[Callable[[], Awaitable[None]]] = None
    # 连接状态变化："connected" | "rotating" | "goaway" | "closed"
    on_state: Optional[Callable[[str, str], Awaitable[None]]] = None
    # 工具调用（P3 用；P0 只打日志）
    on_tool_call: Optional[Callable[[list], Awaitable[None]]] = None


@dataclass
class LiveSessionConfig:
    """一次 Live 会话的配置（setup 消息的原料）。"""

    model: str = "gemini-3.1-flash-live-preview"
    api_key: str = ""
    system_instruction: str = ""
    voice_name: str = "Kore"
    # 说话检测静音阈值（秒）；官方默认 ~800ms，推荐 500-800ms
    vad_silence_ms: int = 600
    # 上下文压缩：触发阈值 / 滑窗大小（token）
    compression_sliding_window: int = 8000
    # 工具声明（P3；格式为 Live functionDeclarations 的 dict 列表）
    function_declarations: Optional[list] = None
    # 代理（None = 直连；如 "http://127.0.0.1:7890"）
    proxy: Optional[str] = None


@dataclass
class _TranscriptBuffer:
    """按 turn 聚合的转写缓冲。Live 的转写是增量文本块，turnComplete 时封口。"""

    user_parts: list = field(default_factory=list)
    assistant_parts: list = field(default_factory=list)

    def take(self, who: str) -> str:
        parts = self.user_parts if who == "user" else self.assistant_parts
        text = "".join(parts).strip()
        parts.clear()
        return text


class LiveClient:
    """一条（可轮换的）Google Live 连接的客户端。

    用法：
        client = LiveClient(cfg, callbacks)
        await client.connect()          # 发 setup，等 setupComplete
        await client.send_audio(pcm16_bytes)
        await client.send_video_frame(jpeg_bytes)
        await client.send_interrupt()
        ...
        await client.close()
    """

    def __init__(self, cfg: LiveSessionConfig, callbacks: Optional[LiveCallbacks] = None):
        self.cfg = cfg
        self.cb = callbacks or LiveCallbacks()
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._close_evt = asyncio.Event()
        self._setup_complete = asyncio.Event()
        self._resumption_handle: Optional[str] = None
        self._tbuf = _TranscriptBuffer()
        self._connected_at = 0.0
        self._closing = False
        # 服务端 CLOSE 码与原因（被拒时 connect() 用它报错）
        self._close_code: Optional[int] = None
        self._close_reason: str = ""
        # 统计（成本估算 / 日志用）
        self.bytes_in = 0
        self.bytes_out = 0
        # 上下文用量观测（压缩可观测性：token 骤降 = 滑窗压缩刚触发过）
        self._last_total_tokens: int = 0
        self._log_usage_at_turn: bool = True
        # 全量转写（通话总结回写的原料）：[(who, text)] 按 turn 顺序
        self._full_transcript: list = []

    # ------------------------------------------------------------------ setup

    def _build_setup(self, resumption_handle: Optional[str]) -> dict:
        cfg = self.cfg
        # v1beta WS 端点要求 model 带 models/ 前缀（裸名报 1008 "not found"）
        model = cfg.model if cfg.model.startswith("models/") else f"models/{cfg.model}"
        setup: dict = {
            "model": model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": cfg.voice_name}
                    }
                },
            },
            "inputAudioTranscription": {},  # 空对象即启用（官方约定）
            "outputAudioTranscription": {},
            "sessionResumption": (
                {"handle": resumption_handle} if resumption_handle else {}
            ),  # 必须显式带 {}，缺省会被静默忽略（官方论坛确认的坑）
            "contextWindowCompression": {
                "slidingWindow": {"targetTokens": cfg.compression_sliding_window}
            },
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    # 注意：startOfSpeechSensitivity / endOfSpeechSensitivity
                    # 在 3.1 系 Live 模型上会被 CLOSE 1007 拒绝（2026-08 实测），
                    # 只保留 padding/silence 时序参数
                    "disabled": False,
                    "prefixPaddingMs": cfg.vad_silence_ms,
                    "silenceDurationMs": cfg.vad_silence_ms,
                }
            },
        }
        if cfg.system_instruction:
            setup["systemInstruction"] = {
                "parts": [{"text": cfg.system_instruction}]
            }
        if cfg.function_declarations:
            setup["tools"] = [{"functionDeclarations": cfg.function_declarations}]
        return setup

    # ------------------------------------------------------------ connection

    async def connect(self) -> None:
        """建连 + setup + 等 setupComplete。异常向上抛。"""
        if self._ws is not None and not self._ws.closed:
            raise RuntimeError("LiveClient already connected")

        url = f"{LIVE_WS_URL}?key={self.cfg.api_key}"
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                url,
                heartbeat=20.0,
                proxy=self.cfg.proxy,
                # 下行音频块可能大；注意 aiohttp 的 0 不是"无限制"而是零容忍
                # （任何消息都超限直接关连接），必须给一个真实数值
                max_msg_size=16 * 1024 * 1024,
            )
        except Exception:
            await self._session.close()
            self._session = None
            raise

        setup = self._build_setup(self._resumption_handle)
        # 注意：_build_setup 返回的是 setup 的内容，外层必须包 {"setup": ...}；
        # 裸内容发出去服务端直接 CLOSE 1007（2026-08 实测）
        await self._ws.send_json({"setup": setup})
        self._connected_at = time.monotonic()

        self._recv_task = asyncio.create_task(self._recv_loop())
        try:
            await asyncio.wait_for(self._setup_complete.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            await self.close()
            raise RuntimeError("Live setup 超时未收到 setupComplete")

        # setupComplete 之前连接就被掐：报出关闭码，别静默成功
        if self._ws is None or self._ws.closed:
            await self.close()
            raise RuntimeError(
                f"Live setup 被服务端拒绝: code={self._close_code} "
                f"reason={self._close_reason[:200]!r}"
            )

        if self.cb.on_state:
            await self._emit(self.cb.on_state, "connected", "")
        logger.info(
            "Live 连接建立: model=%s voice=%s resumed=%s",
            self.cfg.model,
            self.cfg.voice_name,
            bool(self._resumption_handle),
        )

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed and self._setup_complete.is_set()

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._connected_at if self._connected_at else 0.0

    async def close(self) -> None:
        """主动关链。Google 侧会话随 WS 关闭而终止。"""
        self._closing = True
        self._close_evt.set()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._ws = None
        self._session = None
        logger.info(
            "Live 连接关闭: uptime=%.1fs in=%dKB out=%dKB",
            self.uptime_seconds,
            self.bytes_in // 1024,
            self.bytes_out // 1024,
        )

    # ---------------------------------------------------------------- upstream

    async def send_audio(self, pcm16: bytes) -> None:
        """上行音频：raw PCM16（任意采样率，服务端重采样；我们统一 16k mono）。

        注意：3.1 系 Live 模型已弃用 realtimeInput.mediaChunks 数组
        （CLOSE 1007 "media_chunks is deprecated"），必须用平铺的
        realtimeInput.audio 字段（2026-08 实测）。
        """
        if not self.connected:
            return
        b64 = base64.b64encode(pcm16).decode("ascii")
        await self._ws.send_json({
            "realtimeInput": {
                "audio": {
                    "data": b64,
                    "mimeType": f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                }
            }
        })
        self.bytes_out += len(pcm16)

    async def send_video_frame(self, jpeg: bytes) -> None:
        """上行视频帧：离散 JPEG（≤1 FPS 由上游控制节奏）。"""
        if not self.connected:
            return
        b64 = base64.b64encode(jpeg).decode("ascii")
        await self._ws.send_json({
            "realtimeInput": {
                "video": {
                    "data": b64,
                    "mimeType": "image/jpeg",
                }
            }
        })
        self.bytes_out += len(jpeg)

    async def send_interrupt(self) -> None:
        """客户端主动打断（用户点了打断按钮的场景）。

        Live 没有显式 interrupt 字段：发一条空 turns + turnComplete 的
        clientContent 表示"我这轮说完了"，触发服务端尽快接管生成。
        """
        if not self.connected:
            return
        await self._ws.send_json({
            "clientContent": {"turns": [], "turnComplete": True}
        })

    async def send_text(self, text: str) -> None:
        """纯文本输入（P0 冒烟用于触发首轮回复；正式通话不用）。"""
        if not self.connected:
            return
        await self._ws.send_json({
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": text}]}],
                "turnComplete": True,
            }
        })

    # -------------------------------------------------------------- downstream

    async def _recv_loop(self) -> None:
        """显式 receive 循环（不用 async for：它吞 CLOSE 帧，拒绝会被伪装成静默断链）。"""
        assert self._ws is not None
        try:
            while True:
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_json(json.loads(msg.data))
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # Live 下行以二进制帧为主（含 setupComplete），内容仍是 JSON
                    await self._handle_json(json.loads(msg.data))
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    self._close_code = msg.data
                    self._close_reason = str(msg.extra or "")
                    logger.error(
                        "Live WS 被服务端关闭: code=%s reason=%s",
                        self._close_code, self._close_reason[:200],
                    )
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("Live WS error: %s", self._ws.exception())
                    break
                else:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Live 接收循环异常: %s", e)
        finally:
            # 解除 connect() 对 setupComplete 的等待（正常完成 / 被拒都算"终止"）
            self._setup_complete.set()
            if self.cb.on_state:
                await self._emit(self.cb.on_state, "closed", "recv_loop_end")

    async def _handle_json(self, data: dict) -> None:
        # ---- setup 完成
        if "setupComplete" in data:
            self._setup_complete.set()
            return

        # ---- 会话续命令牌更新（连接轮换的原料）
        resumption = data.get("sessionResumptionUpdate")
        if resumption:
            new_handle = resumption.get("newHandle")
            if new_handle:
                self._resumption_handle = new_handle
                logger.debug("Live resumption handle 已更新（轮换原料就绪）")
            return

        # ---- 上下文压缩触发（滑窗机制：Google 侧把早期轮次压掉）
        # 压缩完成的轮会带 contextSize / usageMetadata 变小的特征；显式事件是
        # serverContent 内 turnComplete 伴随 usageMetadata骤降——这里记每次
        # usageMetadata 的 token 用量，压缩发生时（量骤降）能从日志直接看出来
        usage = data.get("usageMetadata") or (data.get("serverContent") or {}).get("usageMetadata")
        if usage and usage.get("totalTokenCount"):
            total = int(usage.get("totalTokenCount"))
            prev = self._last_total_tokens
            # 压缩检测：token 从高位明显回落（>30% 且绝对量 >2000）= 滑窗刚裁剪过
            if prev and prev > total + 2000 and prev > total * 1.3:
                logger.info(
                    "Live 滑窗压缩已触发: %d → %d token（早期轮次已被裁剪，滑窗=%d）",
                    prev, total, self.cfg.compression_sliding_window,
                )
            elif self._log_usage_at_turn:
                logger.info("Live token 用量: total=%s (滑窗=%d)",
                            usage.get("totalTokenCount"), self.cfg.compression_sliding_window)
            self._last_total_tokens = total

        # ---- 服务端预告掐链（~10 分钟连接上限）
        if "goAway" in data:
            time_left = (data["goAway"] or {}).get("timeLeft", "?")
            logger.warning("Live goAway: timeLeft=%s", time_left)
            if self.cb.on_state:
                await self._emit(self.cb.on_state, "goaway", str(time_left))
            return

        # ---- 工具调用（P3 实装；P0 打日志）
        if "toolCall" in data:
            calls = data["toolCall"].get("functionCalls", [])
            logger.info("Live toolCall: %s", [c.get("name") for c in calls])
            if self.cb.on_tool_call:
                await self._emit(self.cb.on_tool_call, calls)
            return
        if "toolCallCancellation" in data:
            ids = data["toolCallCancellation"].get("ids", [])
            logger.info("Live toolCallCancellation: %s", ids)
            return

        # ---- 音频/转写/打断（serverContent）
        content = data.get("serverContent")
        if not content:
            return

        model_turn = content.get("modelTurn") or {}
        parts = model_turn.get("parts", [])
        for part in parts:
            inline = part.get("inlineData")
            if inline and inline.get("mimeType", "").startswith("audio/"):
                audio = base64.b64decode(inline.get("data", ""))
                self.bytes_in += len(audio)
                if self.cb.on_audio:
                    await self._emit(self.cb.on_audio, audio)

        in_tr = content.get("inputTranscription")
        if in_tr and in_tr.get("text"):
            self._tbuf.user_parts.append(in_tr["text"])
            if self.cb.on_transcript:
                await self._emit(self.cb.on_transcript, "user", in_tr["text"])

        out_tr = content.get("outputTranscription")
        if out_tr and out_tr.get("text"):
            self._tbuf.assistant_parts.append(out_tr["text"])
            if self.cb.on_transcript:
                await self._emit(self.cb.on_transcript, "assistant", out_tr["text"])

        if content.get("interrupted"):
            logger.info("Live interrupted（插话打断，生成取消）")
            if self.cb.on_interrupted:
                await self._emit(self.cb.on_interrupted)

        if content.get("turnComplete"):
            # turn 收口：转写缓冲封口（聚合文本由上层决定是否取）
            user_text = self._tbuf.take("user")
            assistant_text = self._tbuf.take("assistant")
            if user_text:
                self._full_transcript.append(("user", user_text))
            if assistant_text:
                self._full_transcript.append(("assistant", assistant_text))
            if (user_text or assistant_text) and self.cb.on_state:
                await self._emit(
                    self.cb.on_state, "turn_complete",
                    json.dumps({"user": user_text, "assistant": assistant_text},
                               ensure_ascii=False),
                )

    @property
    def full_transcript(self) -> list:
        """通话至今的完整转写（按 turn 聚合的 [(who, text)] 列表）。

        通话结束时由 history_writer 消费做总结回写；close() 后仍可读。
        """
        # 收尾时可能有未封口的尾巴（用户说了半句挂断）：补进去
        tail_user = "".join(self._tbuf.user_parts).strip()
        tail_assistant = "".join(self._tbuf.assistant_parts).strip()
        result = list(self._full_transcript)
        if tail_user:
            result.append(("user", tail_user))
        if tail_assistant:
            result.append(("assistant", tail_assistant))
        return result

    async def _emit(self, cb, *args) -> None:
        try:
            await cb(*args)
        except Exception as e:  # noqa: BLE001 - 回调异常不阻断接收循环
            logger.error("Live 回调异常 %s: %s", getattr(cb, "__name__", cb), e)
