'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-06 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""形象生图 Mixin — 前脑 [DRAW:...] 旁路生图 + 异步追发。

链路：前脑标记 → draw_desc 出提示词并挑参考图 → draw 出图 → 落盘 + 入库 + 追发。
文字先发、图片后到，不进后脑工具循环。
"""
# mypy: disable-error-code=attr-defined
# pyright: reportAttributeAccessIssue=false

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from brain import appearance as appearance_lib
from brain.interface import BaseLLM
from brain.prompts import (
    WORKSPACE_SCHEDULE_FILE,
    WORKSPACE_SOUL_FILE,
    _read_file_safe,
    render_template,
)

logger = logging.getLogger(__name__)

_DRAW_PROMPT_PATTERN = re.compile(r"\[DRAW_PROMPT\](.*?)\[/DRAW_PROMPT\]", re.IGNORECASE | re.DOTALL)
_DRAW_REFS_PATTERN = re.compile(r"\[DRAW_REFS\](.*?)\[/DRAW_REFS\]", re.IGNORECASE | re.DOTALL)
# 生图要求与提示词都不该带这些标记，漏进去会污染提示词或被下游误解析。
_STRAY_MARKER_PATTERN = re.compile(
    r"\[(?:SPLIT(?::[0-9.]+)?|NEED_BACKEND|NEED_FOLLOW|NO_REPLY|DRAW:[^\]]*|/?TASK_INSTRUCTION|"
    r"reply:[^\]]*|at:[^\]]*|poke:[^\]]*)\]",
    re.IGNORECASE,
)
# 生图要求上限：前脑不该往标记里塞长文，超了截断以免污染提示词。
MAX_DRAW_REQUEST_CHARS = 400
# 当前消息段最多带几条进 draw_desc：只用于理解"再来一张"这类指代。
DRAW_SEGMENT_MESSAGE_LIMIT = 8

class AppearanceGenMixin:
    """前脑 [DRAW:...] 触发的旁路生图与异步追发。"""

    def start_appearance_image_task(
        self,
        draw_key: str,
        draw_request: str,
        identity: Any = None,
    ) -> None:
        """
        启动生图后台任务。per-runtime_key 只保留最新一张：
        用户连发两条都带 [DRAW:] 时前一张会被丢弃，避免刷图（与 _followup_timers 语义一致）。
        """
        draw_request = _STRAY_MARKER_PATTERN.sub("", str(draw_request or "")).strip()
        if not draw_request:
            return
        if len(draw_request) > MAX_DRAW_REQUEST_CHARS:
            logger.warning(
                "[%s] 生图要求过长（%d 字符），已截断到 %d。",
                draw_key, len(draw_request), MAX_DRAW_REQUEST_CHARS,
            )
            draw_request = draw_request[:MAX_DRAW_REQUEST_CHARS]

        if not appearance_lib.draw_models_configured():
            logger.warning(
                "[%s] 前脑输出了 [DRAW:] 但未配置 draw / draw_desc 模型，跳过生图。", draw_key
            )
            return

        old = self.image_gen_tasks.get(draw_key)
        if old and not old.done():
            logger.info("[%s] 取消上一张未完成的生图任务，只保留最新请求。", draw_key)
            old.cancel()

        self.image_gen_tasks[draw_key] = asyncio.create_task(
            self._generate_and_send_appearance_image(draw_key, draw_request, identity)
        )

    def cancel_appearance_image_task(self, draw_key: str) -> bool:
        """取消某个窗口未完成的生图任务（/stop、清空会话等场景）。"""
        task = self.image_gen_tasks.get(draw_key)
        if task and not task.done():
            task.cancel()
            logger.info("[%s] 已取消未完成的生图任务。", draw_key)
            return True
        self.image_gen_tasks.pop(draw_key, None)
        return False

    def _build_draw_segment_hint(self, identity: Any) -> str:
        """当前未封闭消息段的尾部若干条，用于理解"再来一张"这类指代。"""
        if identity is None:
            return ""
        try:
            msgs = self._current_place_segment_messages(identity) or []
        except Exception:
            logger.debug("构建生图对话片段失败，按空处理。", exc_info=True)
            return ""

        lines: List[str] = []
        for m in msgs[-DRAW_SEGMENT_MESSAGE_LIMIT:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = str(m.get("content", "")).strip()
            if not content:
                continue
            if len(content) > 200:
                content = content[:200] + "…"
            lines.append(f"{'用户' if role == 'user' else 'AI'}: {content}")
        return "\n".join(lines)

    async def _resolve_draw_prompt(self, draw_key: str, draw_request: str, identity: Any):
        """调 draw_desc 出提示词并挑参考图。返回 (prompt, ref_filenames)。"""
        from brain.llm import get_llm_client
        import config

        appearance_text = appearance_lib.read_appearance_text()
        if not appearance_text:
            logger.warning(
                "[%s] APPEARANCE.md 为空，生图将只依据要求与参考图（形象可能漂移）。", draw_key
            )

        # 目标生图模型决定提示词写法：自然语言（nano-banana/Grok 系）还是标签串（SD 系）。
        prompt_style = config.get_draw_prompt_style()

        system_prompt = render_template(
            "draw_desc.jinja",
            "draw_desc_system",
            appearance=appearance_text,
            soul=_read_file_safe(WORKSPACE_SOUL_FILE, max_chars=4000),
            ref_manifest=appearance_lib.describe_manifest_for_prompt(),
            prompt_style=prompt_style,
            style=appearance_lib.read_style_text(),
        )
        user_prompt = render_template(
            "draw_desc.jinja",
            "draw_desc_user",
            user_request=draw_request,
            current_time=appearance_lib.now_local().strftime("%Y-%m-%d %H:%M %A"),
            schedule=_read_file_safe(WORKSPACE_SCHEDULE_FILE, max_chars=3000),
            current_segment=self._build_draw_segment_hint(identity),
            prompt_style=prompt_style,
        )
        if not system_prompt or not user_prompt:
            raise RuntimeError("draw_desc.jinja 渲染失败")

        desc_client = get_llm_client(model_alias="draw_desc")
        # 不带对话历史：历史里满是 Nora 的说话方式，会把这一轮拽回对话口吻。
        raw = await desc_client.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history=[],
        )
        raw = str(raw or "").strip()
        if not raw:
            raise RuntimeError("draw_desc 返回空结果")

        # provider 失败时返回的是「一段错误文本」而不是抛异常，落到下面的
        # [DRAW_PROMPT] 缺失兜底分支就会被整段当成提示词，把 "Error: 模型因内容
        # 安全策略拒绝生成…" 喂进文生图模型。必须在兜底之前拦掉。
        last_error = getattr(desc_client, "last_error", None)
        if last_error or BaseLLM.is_error_result(raw):
            raise RuntimeError(
                f"draw_desc 调用失败（{last_error or 'error_result'}），不拿错误文本当提示词：{raw[:200]}"
            )

        prompt_match = _DRAW_PROMPT_PATTERN.search(raw)
        if prompt_match:
            prompt = prompt_match.group(1).strip()
        else:
            # 模型没按格式包区块时，整段当提示词用——比直接失败更有用。
            logger.warning("[%s] draw_desc 未输出 [DRAW_PROMPT] 区块，整段文本作为提示词。", draw_key)
            prompt = raw
        prompt = _STRAY_MARKER_PATTERN.sub("", prompt).strip()
        if not prompt:
            raise RuntimeError("draw_desc 产出的提示词为空")

        ref_names: List[str] = []
        refs_match = _DRAW_REFS_PATTERN.search(raw)
        if refs_match:
            existing = set(appearance_lib.list_reference_files())
            for token in re.split(r"[,\n;]+", refs_match.group(1)):
                name = token.strip().strip("`\"'")
                if not name:
                    continue
                if name in existing:
                    ref_names.append(name)
                else:
                    logger.warning("[%s] draw_desc 挑了不存在的参考图 '%s'，已丢弃。", draw_key, name)

        return prompt, ref_names

    async def _generate_and_send_appearance_image(
        self,
        runtime_key: str,
        user_request: str,
        identity: Any = None,
    ) -> None:
        """
        读 APPEARANCE.md + manifest → draw_desc 出提示词并挑参考图 → 加载参考图
        → draw 生图 → 落盘 + 入库 + 追发。

        失败只记日志，不向用户追发任何安慰或报错文本（用户等不到图就是等不到）。
        """
        from brain.llm import get_llm_client

        try:
            prompt, ref_names = await self._resolve_draw_prompt(runtime_key, user_request, identity)
            reference_images = appearance_lib.load_reference_images(ref_names)
            if not reference_images:
                logger.warning(
                    "[%s] 本次生图没有可用参考图，形象一致性会下降（先用 "
                    "generate_appearance_reference 生成参考图）。", runtime_key
                )

            logger.info(
                "[%s] 生图开始: refs=%s prompt=%s",
                runtime_key,
                ref_names or "无",
                prompt.replace("\n", " "),
            )

            draw_client = get_llm_client(model_alias="draw")
            result = await draw_client.generate_image(
                prompt, reference_images=reference_images or None
            )
            image_bytes = (result or {}).get("bytes")
            if not image_bytes:
                raise RuntimeError("draw 模型返回空图片数据")

            image_id = appearance_lib.new_generated_id()
            abs_path = appearance_lib.save_generated_image(
                image_bytes,
                (result or {}).get("mime_type") or "image/png",
                image_id=image_id,
            )
            logger.info("[%s] 生图完成，已落盘: %s", runtime_key, abs_path)

            # 入库失败不阻断追发。
            store = getattr(self, "appearance_store", None)
            if store is not None:
                store.save_generated(
                    image_id=image_id,
                    file_path=abs_path,
                    request=user_request,
                    draw_prompt=prompt,
                )

            await self._send_platform_message(runtime_key, f"[image: {abs_path}]")
            logger.info("[%s] 生图已追发给用户。", runtime_key)

        except asyncio.CancelledError:
            # 被新的生图请求取消是正常路径，不当成失败。
            logger.info("[%s] 生图任务被取消。", runtime_key)
            raise
        except Exception as e:
            logger.error(
                "[%s] 生图失败（要求: %s）: %s",
                runtime_key,
                str(user_request)[:80],
                e,
                exc_info=True,
            )
        finally:
            current = self.image_gen_tasks.get(runtime_key)
            if current is asyncio.current_task():
                self.image_gen_tasks.pop(runtime_key, None)

