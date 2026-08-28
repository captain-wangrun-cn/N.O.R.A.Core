"""RTC TTS 下行链路：Nora 台词走 TTS provider 合成（voice_source=tts）。

架构（rtc.md 语音来源配置）：
- Live native audio 模型只出音频不出纯文本——"台词"从 outputAudioTranscription
  的转写取（增量块 + turn 收口）
- **流式优先**：provider 实现 open_live_session()（如 fish_audio 的
  WebSocket TTS live）→ 转写增量一到就 feed_text，音频块即时下发前端
  ——首字延迟几百毫秒，接近 Live 内置语音的体验
- **兼容回落**：provider 没实现 open_live_session（返回 None）→ 回落到
  Live 原生音频（本通话继续用模型内置音色），日志说明原因。理由：
  synthesize() 的产物是带容器的音频（.oga/.mp3）且采样率不定，前端
  PCM 播放管线吃不了；逐句单次合成的延迟体验也太差——宁可声色不变。
  想接自研 provider 的开发者实现 open_live_session（约定见 tts/base.py，
  输出 PCM16 24k 单声道块）即可获得 RTC 通话语音。

打断语义：前端收到 interrupted 清播放队列；live 会话的新一轮 feed_text
自然开始新句子（服务端 condition_on_previous_chunks 保音色连贯）。
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class TtsDownlink:
    """voice_source=tts 时的下行音频器。

    用法（server.py）：
        dl = TtsDownlink(send_chunk)      # send_chunk: async fn(bytes)
        await dl.feed_transcript(text)    # Live 转写增量（assistant 侧）
        await dl.end_turn()               # turn 收口（flush 让剩余出声）
        await dl.close()                  # 通话结束
    """

    def __init__(self, send_chunk: Callable[[bytes], Awaitable[None]]):
        self._send_chunk = send_chunk
        self._provider = None
        self._provider_tried = False
        self._live = None  # open_live_session 的会话（None=未建立/不可用）
        self._unavailable = False  # 流式会话建立不了 → 本通话回落 Live 音频

    @property
    def unavailable(self) -> bool:
        """True = TTS 不可用，调用方（server）应照常下发 Live 原生音频。"""
        return self._unavailable

    def _get_provider(self):
        """懒加载 TTS provider（配置了才建；失败记日志只试一次）。"""
        if self._provider is not None:
            return self._provider
        if self._provider_tried:
            return None
        self._provider_tried = True
        try:
            from tts.registry import build_tts_provider

            p = build_tts_provider()
            if p is None:
                logger.warning("RTC TTS 模式但未配置 tts provider，回落 Live 音频")
                return None
            self._provider = p
            return self._provider
        except Exception as e:  # noqa: BLE001
            logger.warning("RTC TTS provider 加载失败，回落 Live 音频: %s", e)
            return None

    async def _ensure_live(self) -> bool:
        """尝试建立流式会话。True=live 模式可用。"""
        if self._live is not None:
            return True
        if self._unavailable:
            return False
        provider = self._get_provider()
        if provider is None:
            self._unavailable = True
            return False
        try:
            session = provider.open_live_session(self._send_chunk)
        except Exception as e:  # noqa: BLE001
            logger.warning("RTC open_live_session 异常（回落 Live 音频）: %s", e)
            session = None
        if session is None:
            logger.warning(
                "RTC TTS provider %s 未实现流式会话（open_live_session），"
                "本通话回落 Live 内置语音；想接自研 provider 见 tts/base.py 约定",
                provider.name,
            )
            self._unavailable = True
            return False
        self._live = session
        logger.info("RTC TTS 流式会话就绪: provider=%s", provider.name)
        return True

    async def feed_transcript(self, text: str) -> None:
        """Live 的 assistant 转写增量到达 → 喂流式会话（不可用则忽略）。"""
        text = str(text or "").strip()
        if not text or self._unavailable:
            return
        if await self._ensure_live():
            await self._live.feed_text(text)

    async def end_turn(self) -> None:
        """turn 收口：flush 让剩余缓冲全部出声（不可用则忽略）。"""
        if self._live is not None:
            await self._live.end_turn()

    async def close(self) -> None:
        """通话结束：关流式会话（幂等）。"""
        if self._live is not None:
            try:
                await self._live.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("RTC TTS live 会话关闭异常: %s", e)
            self._live = None
