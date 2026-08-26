'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-27 00:00:00
Copyright © WR（captain-wangrun-cn）All rights reserved

语音合成 Mixin — 前脑 [VOICE]...[/VOICE] 旁路 TTS + 异步追发。

链路：前脑标记 → tts/<provider> 合成 → 落盘 → [voice: path] 追发。
文字先发、语音后到，不进后脑工具循环。与 [DRAW:] 旁路完全同构。
'''
# mypy: disable-error-code=attr-defined
# pyright: reportAttributeAccessIssue=false

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# 语音文本里不该带这些系统标记（流控/路由/消息控制），漏进去会被平台误解析
# 或污染朗读；但 provider 自己的情绪方括号标记（[happy] [break] 等）要保留，
# 所以这里逐个点名剥离，不能用宽泛的"剥掉所有方括号"逻辑。
_VOICE_STRAY_MARKER_PATTERN = re.compile(
    r"\[(?:SPLIT(?::[0-9.]+)?|NEED_BACKEND|NEED_FOLLOW|NO_REPLY|SEMI_ONLINE|ONLINE|"
    r"TASK_DONE|/?TASK_INSTRUCTION|/?VOICE|DRAW:[^\]]*|reply:[^\]]*|at:[^\]]*|poke:[^\]]*)\]",
    re.IGNORECASE,
)
# 语音文本上限：前脑不该往块里塞长文，超了截断以免合成超长音频。
MAX_VOICE_TEXT_CHARS = 500


def _voice_save_dir() -> str:
    """语音落盘根目录：workspace/data/voice/（按日期分子目录）。"""
    from brain.prompts import WORKSPACE_DATA_DIR

    root = os.path.join(WORKSPACE_DATA_DIR, "voice")
    os.makedirs(root, exist_ok=True)
    return root


class SpeechGenMixin:
    """前脑 [VOICE] 触发的旁路语音合成与异步追发。"""

    def start_voice_task(
        self,
        voice_key: str,
        voice_text: str,
        identity: Any = None,
    ) -> None:
        """
        启动语音合成后台任务。per-runtime_key 只保留最新一条：
        用户连发两条都带 [VOICE] 时前一条会被丢弃，避免刷语音（与 image_gen_tasks 语义一致）。
        """
        from tts.registry import tts_available

        voice_text = _VOICE_STRAY_MARKER_PATTERN.sub("", str(voice_text or "")).strip()
        if not voice_text:
            return
        if len(voice_text) > MAX_VOICE_TEXT_CHARS:
            logger.warning(
                "[%s] 语音文本过长（%d 字符），已截断到 %d。",
                voice_key, len(voice_text), MAX_VOICE_TEXT_CHARS,
            )
            voice_text = voice_text[:MAX_VOICE_TEXT_CHARS]

        if not tts_available():
            logger.warning(
                "[%s] 前脑输出了 [VOICE] 但 TTS provider 未配置/不可用，跳过合成。", voice_key
            )
            return

        old = self.voice_gen_tasks.get(voice_key)
        if old and not old.done():
            logger.info("[%s] 取消上一条未完成的语音任务，只保留最新请求。", voice_key)
            old.cancel()

        self.voice_gen_tasks[voice_key] = asyncio.create_task(
            self._generate_and_send_voice(voice_key, voice_text)
        )

    def cancel_voice_task(self, voice_key: str) -> bool:
        """取消某个窗口未完成的语音任务（/stop、shutdown 等场景）。"""
        task = self.voice_gen_tasks.get(voice_key)
        if task and not task.done():
            task.cancel()
            logger.info("[%s] 已取消未完成的语音任务。", voice_key)
            return True
        self.voice_gen_tasks.pop(voice_key, None)
        return False

    async def _generate_and_send_voice(self, runtime_key: str, voice_text: str) -> None:
        """
        tts provider 合成 → 落盘 workspace/data/voice/YYYY-MM-DD/ → [voice: path] 追发。

        失败只记日志，不向用户追发任何安慰或报错文本（对齐 [DRAW:] 的约定：
        用户等不到语音就是等不到）。
        """
        from tts.registry import build_tts_provider

        try:
            provider = build_tts_provider()

            logger.info(
                "[%s] 检测到 [VOICE] 标记，语音文本: %s",
                runtime_key, voice_text.replace("\n", " "),
            )
            logger.info(
                "[%s] 语音合成开始: provider=%s",
                runtime_key, provider.name,
            )
            result = await provider.synthesize(voice_text)
            audio_bytes = (result or {}).get("bytes")
            if not audio_bytes:
                raise RuntimeError("TTS provider 返回空音频数据")
            ext = str((result or {}).get("ext") or ".oga")
            ext = ext if ext.startswith(".") else f".{ext}"

            day_dir = os.path.join(_voice_save_dir(), datetime.now().strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            voice_id = f"voice_{uuid.uuid4().hex[:8]}"
            abs_path = os.path.join(day_dir, f"{voice_id}{ext}")
            with open(abs_path, "wb") as f:
                f.write(audio_bytes)
            logger.info("[%s] 语音合成完成，已落盘: %s", runtime_key, abs_path)

            await self._send_platform_message(runtime_key, f"[voice: {abs_path}]")
            logger.info("[%s] 语音已追发给用户。", runtime_key)

        except asyncio.CancelledError:
            # 被新的语音请求取消是正常路径，不当成失败。
            logger.info("[%s] 语音任务被取消。", runtime_key)
            raise
        except Exception as e:
            logger.error(
                "[%s] 语音合成失败（文本: %s）: %s",
                runtime_key,
                str(voice_text)[:80],
                e,
                exc_info=True,
            )
        finally:
            current = self.voice_gen_tasks.get(runtime_key)
            if current is asyncio.current_task():
                self.voice_gen_tasks.pop(runtime_key, None)
