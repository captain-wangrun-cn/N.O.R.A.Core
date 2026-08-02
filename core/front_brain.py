'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:30:17
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""前脑 Mixin — 即时回复 + 审查后脑结果。"""

import logging
from typing import Dict, Any, List, Optional, cast

from brain.prompts import (
    get_soul_prompt,
    render_template,
    load_identity_context,
    load_custom_prompt,
    _read_file_safe,
    WORKSPACE_SCHEDULE_FILE,
    should_inject_custom,
    get_lazy_lexicon_user_prompt_block,
    get_user_preferences_prompt_block,
    load_adapter_prompt,
)
from skills.loader import SkillLoader
from core.message_handler import group_message_content
from core.message_dedup import drop_current_user_message
from core.routing import (
    parse_front_brain_response,
    parse_front_brain_review,
    strip_timestamp_markers,
)
from core.scene_context import build_current_scene_block, should_redact_cross_place_details
import config

logger = logging.getLogger(__name__)


class FrontBrainMixin:
    """前脑即时回复 + 后脑结果审查。"""

    _BLOCK_TAG_PAIRS = [
        ("[TASK_INSTRUCTION]", "[/TASK_INSTRUCTION]"),
    ]
    _API_ERROR_HINTS = (
        "sorry, an error occurred during streaming",
        "sorry, i encountered an issue processing your request with the openai api",
        "openai api stream error",
        "gemini api stream error",
        "抱歉，处理请求时遇到问题",
    )

    def _build_group_presence_context_block(self, identity, context: Dict[str, Any]) -> str:
        """Expose trusted group-listener state without copying user content."""
        if identity.chat_type != "group":
            return ""

        mode = "unknown"
        presence_getter = getattr(self, "group_presence_for", None)
        if callable(presence_getter):
            try:
                current_mode = presence_getter(identity.runtime_key)
                mode = str(getattr(current_mode, "value", current_mode)).lower()
            except Exception:
                logger.warning(
                    "[%s] 获取群监听状态失败，前脑将按 unknown 处理。",
                    identity.runtime_key,
                    exc_info=True,
                )

        triggers = []
        if context.get("explicit_bot_mention"):
            triggers.append("用户明确 @ 你")
        if context.get("reply_to_bot"):
            triggers.append("用户回复了你的消息")
        if context.get("_group_listener_promoted"):
            triggers.append("群监听器将被动消息窗口提升给前脑")
        if not triggers:
            triggers.append("普通群聊上下文")

        batch_type = "多消息群聊批次" if context.get("group_message_batch") else "单次/聚合入站消息"

        # 定时计划开启的监听没有任何人叫过 Nora，前脑需要据此更克制。
        trigger_kind = ""
        stats_getter = getattr(getattr(self, "group_listener", None), "listening_stats", None)
        if mode == "online" and callable(stats_getter):
            try:
                trigger_kind = str((stats_getter(identity.runtime_key) or {}).get("trigger_kind") or "")
            except Exception:
                trigger_kind = ""
        listen_origin_line = ""
        if trigger_kind == "scheduled":
            listen_origin_line = (
                "- 本次监听由系统按当日计划自动开启：没有人叫你，你只是自己进来旁听。"
                "默认只听不说；除非出现真实、具体、你确实能帮上的需要，否则不要开口，也不要宣告自己来了。\n"
            )
        elif trigger_kind == "interaction":
            listen_origin_line = "- 本次监听由 @ / 回复 / 明确的持续参与意图开启。\n"

        return (
            "【群监听运行状态（可信系统元数据）】\n"
            f"- 当前群监听模式: {mode.upper()}\n"
            f"- 本轮触发方式: {'；'.join(triggers)}\n"
            f"- 本轮输入形态: {batch_type}\n"
            f"{listen_origin_line}"
            "- 这里的 ONLINE / SEMI_ONLINE 只指当前群的监听模式，不是全局 AI 状态。"
        )

    @classmethod
    def _strip_timestamp_markers(cls, text: str) -> str:
        return strip_timestamp_markers(text)

    @classmethod
    def _find_unclosed_block_tags(cls, text: str) -> List[str]:
        """检测成对标签是否缺失闭合，返回有问题的标签名列表。"""
        issues: List[str] = []
        lower_text = text.lower()
        for head, tail in cls._BLOCK_TAG_PAIRS:
            head_cnt = lower_text.count(head.lower())
            tail_cnt = lower_text.count(tail.lower())
            if head_cnt != tail_cnt:
                issues.append(head)
        return issues

    @classmethod
    def _clean_unclosed_block_tags(cls, text: str) -> str:
        """移除缺失闭合的标签头或尾，避免污染下游解析。"""
        lower_text = text.lower()
        cleaned = text
        for head, tail in cls._BLOCK_TAG_PAIRS:
            head_cnt = lower_text.count(head.lower())
            tail_cnt = lower_text.count(tail.lower())
            if head_cnt > tail_cnt:
                cleaned = cleaned.replace(head, "")
            elif tail_cnt > head_cnt:
                cleaned = cleaned.replace(tail, "")
        return cleaned

    @classmethod
    def _looks_like_api_error_reply(cls, text: str) -> bool:
        if not text:
            return False
        lower_text = text.strip().lower()
        if not lower_text:
            return False
        return any(hint in lower_text for hint in cls._API_ERROR_HINTS)

    def _build_backend_status_block(self, chat_id: str) -> str:
        """构造"后脑当前状态"上下文块。

        信息来自运行时对象（worker_status / task_queues），不是硬编码示例：
        - 当前正在处理的请求 + 任务指示
        - 已经在排队的请求 + 任务指示

        若后脑空闲且队列为空，返回空字符串（不注入），避免无谓 token。
        前脑收到这一块后，自然能识别"刚才那件事还在做"，
        从而对用户的简单承接（嗯/好的/收到）回复 SEMI_ONLINE 而非重复 NEED_BACKEND。
        """
        try:
            worker_status_map = getattr(self, "worker_status", None) or {}
            identity = self._identity_for_runtime_key(chat_id)
            busy_runtime = self.get_busy_backend_runtime(identity.memory_scope_id)
            status_key = busy_runtime or chat_id
            status = worker_status_map.get(status_key) if isinstance(worker_status_map, dict) else None
            queue = self._get_scope_queue_for_runtime(chat_id)
        except Exception:
            return ""

        busy = bool(status and getattr(status, "busy", False))
        queue_size = queue.size() if queue else 0
        if not busy and queue_size == 0:
            return ""

        lines: List[str] = ["【后脑运行时状态（仅供你判断是否需要再次触发后脑）】"]

        redact_busy = bool(busy_runtime and should_redact_cross_place_details(identity, busy_runtime))
        if busy:
            if redact_busy:
                lines.append("- 后脑正在另一个窗口执行任务。")
                lines.append(f"  来源地点: {busy_runtime}")
                lines.append("  隐私边界: 不要向当前窗口透露那边的任务内容、文件、结果或进度细节。")
            else:
                lines.append(f"- 后脑正在执行: {getattr(status, 'original_query', '')[:120]}")
                if busy_runtime and busy_runtime != chat_id:
                    lines.append(f"  来源地点: {busy_runtime}")
                instr = (getattr(status, "task_instruction", "") or "").strip()
                if instr:
                    lines.append(f"  任务指示: {instr[:240]}")
                phase = getattr(status, "phase", "") or ""
                if phase:
                    lines.append(f"  当前阶段: {phase}")

        if queue_size > 0 and queue is not None:
            lines.append(f"- 已排队任务数: {queue_size}")
            visible_items = []
            redacted_queue_count = 0
            try:
                for item in getattr(queue, "_items", []) or []:
                    item_context = item.get("context") or {}
                    item_identity = self._identity(item_context)
                    if should_redact_cross_place_details(identity, item_identity.runtime_key):
                        redacted_queue_count += 1
                    else:
                        visible_items.append(item)
            except Exception:
                visible_items = []
                redacted_queue_count = queue_size if redact_busy else 0

            if visible_items:
                lines.append(f"📬 当前窗口可见的排队任务 ({len(visible_items)} 个):")
                for i, item in enumerate(visible_items, 1):
                    item_context = item.get("context") or {}
                    text_preview = str(item.get("text") or item_context.get("text") or "")[:60]
                    instr = str(item_context.get("task_instruction", "")).strip()
                    lines.append(f"  {i}. \"{text_preview}\"")
                    if instr:
                        lines.append(f"     指示: {instr[:120]}")
            elif not redacted_queue_count:
                try:
                    summary = queue.get_queue_summary()
                except Exception:
                    summary = ""
                if summary:
                    lines.append(summary)
            if redacted_queue_count:
                lines.append(f"  另有 {redacted_queue_count} 个其它窗口的排队任务，内容已隐藏。")

        lines.append(
            "判断要点："
            "用户本轮消息若只是对你上一轮的承接、确认、附和（如简单回应、情绪表达、表情），"
            "且没有提出与上述【正在执行】或【已排队】任务**不同**的新可执行目标，"
            "就**不要**再加 [NEED_BACKEND]——后脑会把当前任务做完，重复触发只会让它把同一件事再做一遍。"
            "应当根据语境收尾或进入 [SEMI_ONLINE]。"
        )
        return "\n".join(lines)

    @staticmethod
    def _inject_routing_notes(
        history: List[Dict[str, Any]],
        db_context: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """把数据库 metadata 里的路由信息（task_instruction / needs_backend）回填到 assistant 消息文本末尾。

        目的：保证下一轮前脑能从上下文里看到"自己上一轮已经下发过哪些后脑任务指示"。
        否则前脑只会看到清洗后的纯回复（任务指示标记已被移除），无法判断"是否已经下达过"。

        实现：按时间戳/序号位置一一对齐 history 与 db_context；只增强 assistant 消息，
        且仅追加一行内部备注（用户不可见，因为它只存在于发给模型的 history 里）。
        """
        if not history:
            return history

        # 取出 db_context 里 role 为 user/assistant 的子列表，与 history 顺序对齐
        aligned_db = [m for m in db_context if m.get("role") in ("user", "assistant")]
        if len(aligned_db) != len(history):
            # 长度不一致就保守不增强，避免错位污染
            return history

        enhanced: List[Dict[str, Any]] = []
        for msg, db_msg in zip(history, aligned_db):
            if msg.get("role") != "assistant":
                enhanced.append(msg)
                continue
            md = db_msg.get("metadata") or {}
            routing = md.get("routing") or {}
            instr = str(routing.get("task_instruction", "")).strip()
            needs_backend = bool(routing.get("needs_backend"))
            if not instr and not needs_backend:
                enhanced.append(msg)
                continue
            note_lines = []
            if needs_backend:
                note_lines.append("已触发后脑接管处理。")
            if instr:
                # 截断防止超长指示淹没上下文
                note_lines.append(f"已下达后脑任务指示: {instr[:240]}")
            note = "\n".join(note_lines)
            enhanced.append({
                "role": "assistant",
                "content": f"{msg['content']}\n\n[内部备注·上一轮路由记录]\n{note}",
            })
        return enhanced

    async def _generate_front_chat_response(
        self,
        context: Dict[str, Any],
        proactive_meta: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        前脑即时回复：使用 smart 模型生成自然对话回复 + 路由判断。
        
        不涉及工具调用、RAG 检索等重操作。
        
        Returns:
            {
                "needs_backend": bool,   # 是否需要后脑接管
                "user_reply": str,       # 发给用户的清洁回复（已移除路由信号）
                "raw_response": str,     # LLM 原始输出（含信号，用于日志）
            }
        """
        identity = self._identity(context)
        chat_id = identity.runtime_key
        platform = identity.platform
        storage_id = identity.storage_id
        user_id = identity.actor_user_id
        text = context["text"]
        chat_type = identity.chat_type
        user_name = context.get("user_name", "User")
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id

        proactive_meta = proactive_meta or {}
        proactive_mode = bool(proactive_meta)

        logger.info(f"[{chat_id}] 前脑处理: '{text[:80]}' (proactive={proactive_mode})")

        # --- 构建前脑 prompt ---
        soul_prompt = get_soul_prompt()
        identity_context = load_identity_context(
            include_schedule=False,
            actor_display_name=identity.actor_display_name,
            is_owner=identity.is_owner,
            chat_type=chat_type,
            chat_title=str(context.get("chat_title") or context.get("group_title") or ""),
            group_member_count=context.get("group_member_count"),
            group_member_count_status=str(context.get("group_member_count_status") or ""),
            group_online_count=context.get("group_online_count"),
            group_online_count_status=str(context.get("group_online_count_status") or ""),
        )
        scene_context_block = build_current_scene_block(identity, context)
        group_presence_context_block = self._build_group_presence_context_block(identity, context)

        # 可选注入块默认空串：下面这些块都是按条件/try 注入，
        # 若条件不满足或注入失败必须有兜底空值，否则渲染模板时会 UnboundLocalError。
        schedule_block = ""
        custom_block = ""
        tool_intro_block = ""
        skill_intro_block = ""
        user_preferences_block = ""
        platform_prompt_block = ""

        try:
            schedule_content = _read_file_safe(WORKSPACE_SCHEDULE_FILE)
            if schedule_content:
                schedule_block = render_template(
                    'context_injection.jinja',
                    'schedule',
                    schedule_content=schedule_content,
                )
        except Exception:
            logger.warning("前脑 SCHEDULE 注入失败，已忽略。", exc_info=True)

        try:
            custom_prompt = load_custom_prompt() if should_inject_custom("smart") else ""
            if custom_prompt:
                custom_block = render_template(
                    'context_injection.jinja',
                    'custom',
                    custom_content=custom_prompt,
                )
        except Exception:
            logger.warning("前脑 CUSTOM 注入失败，已忽略。", exc_info=True)

        try:
            user_preferences_block = get_user_preferences_prompt_block()
        except Exception:
            logger.warning("前脑 Nora 偏好注入失败，已忽略。", exc_info=True)

        try:
            platform_prompt_block = load_adapter_prompt(platform)
        except Exception:
            logger.warning("前脑 adapter 平台协议注入失败，已忽略。", exc_info=True)

        try:
            tool_intros = self.tool_manager.get_tool_intros()
            if tool_intros:
                tool_desc = "\n".join([f"- {name}: {intro}" for name, intro in tool_intros.items()])
                tool_intro_block = render_template('context_injection.jinja', 'tools', tool_desc=tool_desc)
        except Exception:
            logger.warning("前脑工具简介注入失败，已忽略。", exc_info=True)

        try:
            skills = SkillLoader().scan_skills()
            if skills:
                skill_desc = "\n".join(
                    [f"- {s['name']}: {s.get('description', '').strip()}" for s in skills]
                )
                skill_intro_block = render_template('context_injection.jinja', 'skills', skill_desc=skill_desc)
        except Exception:
            logger.warning("前脑技能简介注入失败，已忽略。", exc_info=True)

        # 主动触发上下文
        proactive_context = ""
        proactive_user_message = ""
        yesterday_memory_block = ""
        if proactive_mode:
            event_type = proactive_meta.get("event_type", "proactive")
            reason = proactive_meta.get("reason", "")
            current_time = proactive_meta.get("current_time", "")
            trigger_from = proactive_meta.get("trigger_from", "")
            recent_hint = proactive_meta.get("recent_hint", "")

            meta_lines = [f"类型: {event_type}（主动触发）"]
            if current_time:
                meta_lines.append(f"当前时间: {current_time}")
            if reason:
                meta_lines.append(f"触发缘由: {reason}")
            if trigger_from:
                meta_lines.append(f"来源: {trigger_from}")
            if recent_hint:
                meta_lines.append(f"最近对话线索: {recent_hint}")
            proactive_context = "\n".join(meta_lines)

            base_reason = reason or "无特定缘由（定时关怀）"
            proactive_user_message = proactive_meta.get("user_prompt") or (
                "这是一次主动消息触发（不是用户发起）。\n"
                f"类型: {event_type}\n"
                f"当前时间: {current_time or '未知'}\n"
                f"触发缘由: {base_reason}\n"
                "请生成一条自然的主动消息，必要时说明你要去执行的后台任务或查询。"
            )

        yesterday_memory = proactive_meta.get("recent_daily_summaries", "") if proactive_mode else ""
        if yesterday_memory:
            yesterday_memory_block = render_template(
                'context_injection.jinja',
                'yesterday_memory',
                memory_content=yesterday_memory,
            )

        system_prompt = render_template(
            'front_brain.jinja',
            'system',
            soul_prompt=soul_prompt,
            identity_context=identity_context,
            user_preferences_block=user_preferences_block,
            platform_prompt_block=platform_prompt_block,
            schedule_block=schedule_block,
            custom_block=custom_block,
            tool_intro_block=tool_intro_block,
            skill_intro_block=skill_intro_block,
            proactive_context=proactive_context,
        )
        if yesterday_memory_block:
            system_prompt = system_prompt + "\n\n---\n" + yesterday_memory_block
        if scene_context_block:
            system_prompt = system_prompt + "\n\n---\n" + scene_context_block
        if group_presence_context_block:
            system_prompt = system_prompt + "\n\n---\n" + group_presence_context_block

        # 后脑忙碌或队列非空时，把"正在处理什么 + 已排队什么"作为运行时上下文块注入前脑系统提示，
        # 让前脑天然知道"刚才下达过哪些任务、现在还在做"，从而避免对用户的简单承接（嗯/好的/收到）
        # 误升级为重复后脑任务。这一块来自上下文，不是硬编码示例。
        backend_status_block = self._build_backend_status_block(chat_id)
        if backend_status_block:
            system_prompt = system_prompt + "\n\n---\n" + backend_status_block

        if proactive_mode:
            user_prompt = render_template('front_brain.jinja', 'proactive_user', proactive_user_message=proactive_user_message)
        else:
            user_prompt = render_template('front_brain.jinja', 'user', user_message=text)

        # 图片加载失败提示：用户文本里出现了 [image: ...] 等图片标记，但系统没能成功加载图片字节
        # （网络下载失败 / 路径无效 / 引用历史图片但本轮未重传 等）。
        # 必须把这一事实告诉模型，避免它凭空臆想图片内容。
        if context.get("image_load_failed"):
            user_prompt += (
                "\n\n[系统备注] 用户消息里出现了图片相关标记，但本轮系统并没有真正接收到图片数据"
                "（可能是网络下载失败、原图丢失、或用户只是引用了一张历史图片）。\n"
                "→ 你绝对不能假装看到图片或编造图片内容。\n"
                "→ 也不要硬邦邦地说 \"没读到图片，请重发原图\" 这种系统化的话；自然地表达即可，"
                "比如轻松地说 \"诶？图片好像没传过来欸\" / \"这边没看到图片哦\"，"
                "或者根据上下文判断是否调用 view_media 找历史图/视频。"
            )

        # 懒加载词库命中：仅命中词条注入 user prompt
        lazy_lexicon_block = get_lazy_lexicon_user_prompt_block(text)
        if lazy_lexicon_block:
            user_prompt = f"{user_prompt}\n\n{lazy_lexicon_block}"

        # --- 前脑也接入 RAG 记忆检索 ---
        if self.rag.enabled:
            try:
                rag_context = self.rag.get_context_string(
                    text,
                    user_id=storage_id,
                    top_k=2,
                    platform=platform,
                    chat_id=identity.platform_chat_id,
                    storage_id=storage_id,
                    chat_type=chat_type,
                    memory_scope_id=memory_scope_id,
                )
                if rag_context:
                    system_prompt = (
                        system_prompt
                        + "\n\n"
                        + render_template('context_injection.jinja', 'rag', rag_context=rag_context)
                    )
                    await self._send_debug(chat_id, f"🧠 前脑RAG命中: {len(rag_context.splitlines())} 行记忆")
            except Exception:
                logger.warning(f"[{chat_id}] 前脑 RAG 检索失败，已降级继续。", exc_info=True)

        # --- 加载对话历史 ---
        db_context = self.message_history.get_forebrain_context_messages(
            platform, storage_id, memory_scope_id=memory_scope_id, current_place_scope_id=place_scope_id
        )

        # 剔除历史里"本轮当前用户消息"那一条（它已经作为 user_prompt 单独传入）。
        # 按写库时记录的行 id 精确剔除；没有 id 记录时才退回内容比较。
        message_content = group_message_content(context, user_name, text, chat_type)
        db_context = drop_current_user_message(db_context, context, message_content)

        history = [
            {
                "role": msg["role"],
                "content": self._strip_timestamp_markers(str(msg["content"]))
            }
            for msg in db_context
            if msg["role"] in ("system", "user", "assistant")
        ]

        # 上下文增强：把前脑历史回复中携带的 routing 元信息（task_instruction / needs_backend）
        # 以"内部备注"形式回填到 assistant 消息末尾，这样下一轮前脑生成时就能看到
        # "自己上一轮已经下达过哪些任务"，避免对用户的简单承接（嗯/好的/收到）误升级为
        # 重复的后脑任务。该备注仅给模型阅读，不会发送给用户。
        history = self._inject_routing_notes(history, db_context)

        # 聚合器语义：如果上一次前脑生成被新消息打断且留下了"未发出的草稿"，
        # 把草稿作为系统备注交给模型，让它在重新生成时参考（而不是丢弃从零开始）。
        # 草稿仅给模型看，不会发送给用户。
        prior_partial = ""
        try:
            prior_partial = (self.front_brain_partial.pop(chat_id, "") or "").strip()
        except Exception:
            prior_partial = ""
        if prior_partial:
            user_prompt += (
                "\n\n[系统备注] 上一次回复在生成途中被用户的新消息打断，以下是当时已经生成但**尚未发送**给用户的草稿："
                f"\n---\n{prior_partial}\n---\n"
                "请综合这段草稿和上面的新消息重新组织本轮回复："
                "如果草稿与新消息能自然续写，可以延用草稿语气与已表达的部分；"
                "如果新消息让草稿不再合适，请直接重写。无论如何不要再向用户重复发送这段草稿原文。"
            )
        # --- 调用 smart 模型生成回复（流式收集完整文本，检测标签闭合） ---
        async def _run_with_retries(system_prompt_candidate: str):
            response_text_local = ""
            usage_data_local = None
            max_attempts = 3
            for attempt in range(max_attempts):
                response_text_local = ""
                usage_data_local = None
                # 重置本轮的实时缓冲，让外层"被打断"分支只看到本轮已生成内容
                self.front_brain_partial[chat_id] = ""
                try:
                    stream = self._chat_stream_wrapper(
                        self.llm,
                        chat_id,
                        system_prompt=system_prompt_candidate,
                        user_prompt=user_prompt,
                        history=history,
                        tools=None,  # 前脑不使用工具
                    )
                    async for raw_chunk in stream:
                        chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                        if isinstance(chunk, dict):
                            if chunk["type"] == "text":
                                response_text_local += chunk["content"]
                                # 同步到 controller 上的实时缓冲，方便被 cancel 时读取草稿
                                self.front_brain_partial[chat_id] = response_text_local
                            elif chunk["type"] == "usage":
                                usage_data_local = {
                                    "input_tokens": chunk.get("input_tokens", 0),
                                    "output_tokens": chunk.get("output_tokens", 0),
                                }
                        elif isinstance(chunk, str):
                            response_text_local += chunk
                            self.front_brain_partial[chat_id] = response_text_local
                except Exception as e:
                    logger.warning(
                        f"[{chat_id}] 前脑请求异常，第 {attempt+1}/{max_attempts} 次尝试失败，准备重试: {e}",
                        exc_info=True,
                    )
                    if attempt < max_attempts - 1:
                        continue
                    raise

                # 清理思考标签
                response_text_local = self._strip_thinking_content(response_text_local)

                # API 异常保护：空输出或 provider 错误占位文本，视为请求异常并重试
                is_empty_output = not response_text_local.strip()
                is_api_error_reply = self._looks_like_api_error_reply(response_text_local)
                if is_empty_output or is_api_error_reply:
                    logger.warning(
                        f"[{chat_id}] 前脑疑似 API 异常（empty={is_empty_output}, api_error={is_api_error_reply}），"
                        f"第 {attempt+1}/{max_attempts} 次尝试重试。"
                    )
                    if attempt < max_attempts - 1:
                        continue
                    raise RuntimeError("front brain failed after retries due to empty/api-error output")

                issues = self._find_unclosed_block_tags(response_text_local)
                if issues:
                    logger.warning(f"[{chat_id}] 前脑输出存在未闭合标签 {issues}，第 {attempt+1}/{max_attempts} 次尝试重试。")
                    if attempt < max_attempts - 1:
                        continue
                    # 最后一轮，清理未闭合标签再继续解析，避免污染用户侧
                    response_text_local = self._clean_unclosed_block_tags(response_text_local)
                break
            return response_text_local, usage_data_local

        try:
            response_text, usage_data = await _run_with_retries(system_prompt)
        except Exception:
            # 前脑失败时回退到后脑
            self.front_brain_partial.pop(chat_id, None)
            return {"needs_backend": True, "user_reply": "", "raw_response": ""}

        # 成功生成完整回复，清掉实时草稿
        self.front_brain_partial.pop(chat_id, None)

        # 记录成本
        if self.cost_tracking_enabled and self.cost_tracker and usage_data:
            provider_name = config.get_model_provider("smart")
            provider = config.get_provider_type(provider_name)
            model = config.get_model_name("smart")
            self.cost_tracker.log_usage(
                provider=provider,
                model=model,
                input_tokens=usage_data["input_tokens"],
                output_tokens=usage_data["output_tokens"],
                model_alias="smart",
                context="front_brain",
            )

        # 解析路由信号
        parsed = parse_front_brain_response(response_text)

        # 如果前脑沿用旧格式，只输出 [NEED_BACKEND] 却没有任务指示，
        # parse 层会拒绝升级。这里提醒一次，避免“看似叫了后脑但实际没触发”。
        if (
            "[NEED_BACKEND]" in str(response_text)
            and not parsed.get("task_instruction")
            and not parsed.get("needs_backend")
        ):
            logger.warning(f"[{chat_id}] 检测到 [NEED_BACKEND] 但缺少任务指示，提醒模型重试。")
            await self._send_debug(chat_id, "⚠️ 前脑输出 [NEED_BACKEND] 但缺少任务指示，将提醒并重试一次。")

            reminder_system_prompt = (
                system_prompt
                + "\n\n【重要提醒】如果你要把事情交给后脑执行，必须同时输出 "
                  "[TASK_INSTRUCTION]...[/TASK_INSTRUCTION] 和 [NEED_BACKEND]。"
                  "任务指示块要写清目标、约束和期望结果。"
                  "如果无需后脑，请去掉 [NEED_BACKEND] 并直接聊天回复。"
            )

            try:
                response_text, usage_data = await _run_with_retries(reminder_system_prompt)
                parsed = parse_front_brain_response(response_text)
            except Exception:
                logger.warning(f"[{chat_id}] 前脑缺任务指示补救重试失败，保持原流程。", exc_info=True)

        # 如果前脑输出了任务指示但缺少后脑标记，提醒模型并重试一次
        if parsed.get("task_instruction") and not parsed.get("needs_backend"):
            logger.warning(f"[{chat_id}] 检测到任务指示但无 [NEED_BACKEND] 标记，提醒模型重试。")
            await self._send_debug(chat_id, "⚠️ 前脑输出任务指示但未包含 [NEED_BACKEND]，将提醒并重试一次。")

            reminder_system_prompt = (
                system_prompt
                + "\n\n【重要提醒】如果你输出了 [TASK_INSTRUCTION]...[/TASK_INSTRUCTION]，必须同时添加 [NEED_BACKEND] 标记以触发后脑执行。"
                "如果无需后脑，请不要输出任务指示块。"
            )

            try:
                response_text, usage_data = await _run_with_retries(reminder_system_prompt)
                parsed = parse_front_brain_response(response_text)
            except Exception:
                return {"needs_backend": True, "user_reply": "", "raw_response": ""}

        # API 异常补救：若无需后脑且应回复，但出现空输出/错误占位文本，重试一次
        if (
            not parsed.get("needs_backend")
            and parsed.get("should_reply", True)
            and not str(parsed.get("user_reply", "")).strip()
            and (
                not str(response_text).strip()
                or self._looks_like_api_error_reply(str(response_text))
            )
        ):
            logger.warning(f"[{chat_id}] 前脑疑似 API 异常导致空回复，触发补救重试。")
            await self._send_debug(chat_id, "⚠️ 前脑请求疑似失败（空输出/错误占位），正在自动重试。")
            try:
                response_text, usage_data = await _run_with_retries(system_prompt)
                parsed = parse_front_brain_response(response_text)
            except Exception:
                logger.warning(f"[{chat_id}] 前脑 API 异常补救重试失败，保持原流程。", exc_info=True)
        logger.info(
            "[%s] 前脑结果: needs_backend=%s should_reply=%s need_follow=%s "
            "force_online=%s force_semi_online=%s keep_segment_open=%s reply_length=%d",
            chat_id,
            parsed["needs_backend"],
            parsed.get("should_reply"),
            parsed.get("need_follow"),
            parsed.get("force_online", False),
            parsed.get("force_semi_online", False),
            parsed.get("keep_segment_open", False),
            len(str(parsed.get("user_reply", ""))),
        )
        await self._send_debug(
            chat_id,
            f"⚡ 前脑: needs_backend={parsed['needs_backend']}\n"
            f"should_reply={parsed.get('should_reply')}\n"
            f"任务指示: {parsed.get('task_instruction', '')[:200]}\n"
            f"need_follow={parsed.get('need_follow')}\n"
            f"keep_segment_open={parsed.get('keep_segment_open')}\n"
            f"retract_message_target={parsed.get('retract_message_target', '')}\n"
            f"回复: {parsed['user_reply'][:200]}"
        )

        return {
            "needs_backend": parsed["needs_backend"],
            "user_reply": parsed["user_reply"],
            "task_instruction": parsed.get("task_instruction", ""),
            "should_reply": parsed.get("should_reply", True),
            "use_image_model": parsed.get("use_image_model", False),
            "need_follow": parsed.get("need_follow", False),
            "force_semi_online": parsed.get("force_semi_online", False),
            "force_online": parsed.get("force_online", False),
            "keep_segment_open": parsed.get("keep_segment_open", False),
            "retract_message_target": parsed.get("retract_message_target", ""),
            "route": parsed.get("route"),
            "raw_response": response_text,
        }

    async def _front_brain_review(
        self,
        context: Dict[str, Any],
        backend_result: str,
        new_user_messages: List[str],
    ) -> dict:
        """
        前脑审查后脑结果，判断是否需要继续轮询。
        
        Args:
            context: 原始消息上下文
            backend_result: 后脑的最终回复文本
            new_user_messages: 后脑执行期间用户发送的新消息
        
        Returns:
            {
                "action": "done" | "continue" | "chat",
                "user_reply": str,
            }
        """
        identity = self._identity(context)
        chat_id = identity.runtime_key
        platform = identity.platform
        storage_id = identity.storage_id
        user_id = identity.actor_user_id
        chat_type = identity.chat_type
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id

        logger.info(f"[{chat_id}] 前脑审查: 后脑结果 {len(backend_result)} 字, 新用户消息 {len(new_user_messages)} 条")

        # --- 构建审查 prompt ---
        soul_prompt = get_soul_prompt()
        identity_context = load_identity_context(
            include_schedule=False,
            actor_display_name=identity.actor_display_name,
            is_owner=identity.is_owner,
            chat_type=chat_type,
            chat_title=str(context.get("chat_title") or context.get("group_title") or ""),
            group_member_count=context.get("group_member_count"),
            group_member_count_status=str(context.get("group_member_count_status") or ""),
            group_online_count=context.get("group_online_count"),
            group_online_count_status=str(context.get("group_online_count_status") or ""),
        )
        scene_context_block = build_current_scene_block(identity, context)
        group_presence_context_block = self._build_group_presence_context_block(identity, context)
        schedule_block = ""
        custom_block = ""
        tool_intro_block = ""
        user_preferences_block = ""
        platform_prompt_block = ""
        try:
            schedule_content = _read_file_safe(WORKSPACE_SCHEDULE_FILE)
            if schedule_content:
                schedule_block = render_template(
                    'context_injection.jinja',
                    'schedule',
                    schedule_content=schedule_content,
                )
        except Exception:
            logger.warning("前脑审查 SCHEDULE 注入失败，已忽略。", exc_info=True)
        try:
            custom_prompt = load_custom_prompt() if should_inject_custom("smart") else ""
            if custom_prompt:
                custom_block = render_template(
                    'context_injection.jinja',
                    'custom',
                    custom_content=custom_prompt,
                )
        except Exception:
            logger.warning("前脑审查 CUSTOM 注入失败，已忽略。", exc_info=True)
        try:
            user_preferences_block = get_user_preferences_prompt_block()
        except Exception:
            logger.warning("前脑审查 Nora 偏好注入失败，已忽略。", exc_info=True)
        try:
            platform_prompt_block = load_adapter_prompt(platform)
        except Exception:
            logger.warning("前脑审查 adapter 平台协议注入失败，已忽略。", exc_info=True)
        try:
            tool_intros = self.tool_manager.get_tool_intros()
            if tool_intros:
                tool_desc = "\n".join([f"- {name}: {intro}" for name, intro in tool_intros.items()])
                tool_intro_block = render_template('context_injection.jinja', 'tools', tool_desc=tool_desc)
        except Exception:
            pass

        system_prompt = render_template(
            'front_brain_review.jinja',
            'system',
            soul_prompt=soul_prompt,
            identity_context=identity_context,
            user_preferences_block=user_preferences_block,
            platform_prompt_block=platform_prompt_block,
            schedule_block=schedule_block,
            custom_block=custom_block,
            tool_intro_block=tool_intro_block,
        )
        if scene_context_block:
            system_prompt = system_prompt + "\n\n---\n" + scene_context_block
        if group_presence_context_block:
            system_prompt = system_prompt + "\n\n---\n" + group_presence_context_block

        user_prompt = render_template(
            'front_brain_review.jinja',
            'user',
            backend_result=backend_result,
            new_user_messages=new_user_messages,
        )

        # 前脑审查使用完整上下文：历史压缩上下文 + 当前活跃段原文
        db_context = self.message_history.get_forebrain_context_messages(
            platform, storage_id, memory_scope_id=memory_scope_id, current_place_scope_id=place_scope_id
        )
        current_session_msgs = [
            {
                "role": msg["role"],
                "content": self._strip_timestamp_markers(str(msg["content"]))
            }
            for msg in db_context
            if msg.get("role") in ("system", "user", "assistant")
        ]
        # 同步注入历史路由备注，避免审查阶段误判任务是否已下达
        current_session_msgs = self._inject_routing_notes(current_session_msgs, db_context)

        # 将后脑报告与新用户消息显式放入上下文，便于审查模型完整感知
        backend_report = backend_result.strip()
        if backend_report:
            current_session_msgs.append({
                "role": "assistant",
                "content": f"【后脑工作报告】\n{backend_report}"
            })

        for msg in new_user_messages:
            msg = str(msg).strip()
            if msg:
                current_session_msgs.append({"role": "user", "content": msg})

        history = current_session_msgs

        async def _run_review_once(system_prompt_candidate: str):
            response_text_local = ""
            usage_data_local = None
            try:
                stream = self._chat_stream_wrapper(
                    self.llm,
                    chat_id,
                    system_prompt=system_prompt_candidate,
                    user_prompt=user_prompt,
                    history=history,
                    tools=None,
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict):
                        if chunk["type"] == "text":
                            response_text_local += chunk["content"]
                        elif chunk["type"] == "usage":
                            usage_data_local = {
                                "input_tokens": chunk.get("input_tokens", 0),
                                "output_tokens": chunk.get("output_tokens", 0),
                            }
                    elif isinstance(chunk, str):
                        response_text_local += chunk
            except Exception as e:
                logger.error(f"[{chat_id}] 前脑审查失败: {e}", exc_info=True)
                return None, None

            response_text_local = self._strip_thinking_content(response_text_local)

            if self.cost_tracking_enabled and self.cost_tracker and usage_data_local:
                provider_name = config.get_model_provider("smart")
                provider = config.get_provider_type(provider_name)
                model = config.get_model_name("smart")
                self.cost_tracker.log_usage(
                    provider=provider,
                    model=model,
                    input_tokens=usage_data_local["input_tokens"],
                    output_tokens=usage_data_local["output_tokens"],
                    model_alias="smart",
                    context="front_brain_review",
                )

            parsed_local = parse_front_brain_review(response_text_local)
            return parsed_local, response_text_local

        parsed, response_text = await _run_review_once(system_prompt)
        if not parsed:
            return {"action": "done", "user_reply": "", "route": {"platform": "", "scene_id": "", "scene_type": ""}}

        if parsed.get("task_instruction") and parsed.get("action") != "continue":
            logger.warning(f"[{chat_id}] 审查输出含任务指示但未标记继续，提醒模型确认。")
            await self._send_debug(
                chat_id,
                "⚠️ 审查输出含任务指示但未给出 [NEED_BACKEND]/continue，将提醒模型确认是否需携带指示继续后脑。"
            )

            reminder_system_prompt = (
                system_prompt
                + "\n\n【重要提醒】你给出了 [TASK_INSTRUCTION] 指示，但没有明确继续。" \
                  "请判断是否需要携带该指示让后脑继续：" \
                  "如果需要继续，务必添加 [NEED_BACKEND] 并保留任务指示；" \
                  "如果不需要继续，去掉任务指示并明确 [TASK_DONE] 或直接给出最终给用户的回复。"
            )

            parsed_retry, response_text_retry = await _run_review_once(reminder_system_prompt)
            if parsed_retry:
                parsed = parsed_retry
                response_text = response_text_retry
            else:
                # 回退：默认继续，避免任务指示丢失
                return {
                    "action": "continue",
                    "user_reply": parsed.get("user_reply", ""),
                    "task_instruction": parsed.get("task_instruction", ""),
                    "should_reply": parsed.get("should_reply", True),
                    "use_image_model": parsed.get("use_image_model", False),
                    "route": parsed.get("route"),
                }

        logger.info(
            "[%s] 前脑审查结果: action=%s should_reply=%s need_follow=%s "
            "force_online=%s force_semi_online=%s reply_length=%d",
            chat_id,
            parsed["action"],
            parsed.get("should_reply"),
            parsed.get("need_follow"),
            parsed.get("force_online", False),
            parsed.get("force_semi_online", False),
            len(str(parsed.get("user_reply", ""))),
        )
        await self._send_debug(
            chat_id,
            f"🔍 审查: action={parsed['action']}\n"
            f"should_reply={parsed.get('should_reply')}\n"
            f"任务指示: {parsed.get('task_instruction', '')[:200]}\n"
            f"回复: {parsed['user_reply'][:200]}"
        )

        return parsed
