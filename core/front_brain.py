'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:30:17
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""前脑 Mixin — 即时回复 + 审查后脑结果。"""

import logging
import re
from typing import Dict, Any, List, Optional, cast

from brain.prompts import (
    get_soul_prompt,
    render_template,
    load_identity_context,
    load_custom_prompt,
    _read_file_safe,
    WORKSPACE_SCHEDULE_FILE,
    should_inject_custom,
)
from skills.loader import SkillLoader
from core.routing import parse_front_brain_response, parse_front_brain_review
import config

logger = logging.getLogger(__name__)


class FrontBrainMixin:
    """前脑即时回复 + 后脑结果审查。"""

    _TIMESTAMP_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*")
    _BLOCK_TAG_PAIRS = [
        ("[TASK_INSTRUCTION]", "[/TASK_INSTRUCTION]"),
    ]

    @classmethod
    def _strip_timestamp_markers(cls, text: str) -> str:
        if not text:
            return ""
        return cls._TIMESTAMP_PATTERN.sub("", text).strip()

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
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")
        storage_id = chat_id if chat_type != "private" else user_id

        proactive_meta = proactive_meta or {}
        proactive_mode = bool(proactive_meta)

        logger.info(f"[{chat_id}] 前脑处理: '{text[:80]}' (proactive={proactive_mode})")

        # --- 构建前脑 prompt ---
        soul_prompt = get_soul_prompt()
        identity_context = load_identity_context(include_schedule=False)
        schedule_block = ""
        custom_block = ""
        tool_intro_block = ""
        skill_intro_block = ""

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
            from brain.tools import TOOL_INTROS
            if TOOL_INTROS:
                tool_desc = "\n".join([f"- {name}: {intro}" for name, intro in TOOL_INTROS.items()])
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

        system_prompt = render_template(
            'front_brain.jinja',
            'system',
            soul_prompt=soul_prompt,
            identity_context=identity_context,
            schedule_block=schedule_block,
            custom_block=custom_block,
            tool_intro_block=tool_intro_block,
            skill_intro_block=skill_intro_block,
            proactive_context=proactive_context,
        )
        if proactive_mode:
            user_prompt = render_template('front_brain.jinja', 'proactive_user', proactive_user_message=proactive_user_message)
        else:
            user_prompt = render_template('front_brain.jinja', 'user', user_message=text)

        # --- 前脑也接入 RAG 记忆检索 ---
        if self.rag.enabled:
            try:
                rag_context = self.rag.get_context_string(text, user_id=storage_id, top_k=2)
                if rag_context:
                    system_prompt = (
                        system_prompt
                        + "\n\n"
                        + render_template('context_injection.jinja', 'rag', rag_context=rag_context)
                    )
                    await self._send_debug(chat_id, f"🧠 前脑RAG命中: {len(rag_context.splitlines())} 行记忆")
            except Exception:
                logger.warning(f"[{chat_id}] 前脑 RAG 检索失败，已降级继续。", exc_info=True)

        # --- 加载对话历史（轻量，仅用于上下文连贯） ---
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        # 只取最近的消息，前脑不需要太多历史
        if len(db_context) > 20:
            db_context = db_context[-20:]
        
        # 去掉最后一条 user 消息（避免和 user_prompt 重复）
        message_content = f"{user_name}: {text}" if chat_type != "private" else text
        if db_context:
            last_msg = db_context[-1]
            last_content = self._strip_timestamp_markers(str(last_msg.get("content", "")))
            if last_msg.get("role") == "user" and last_content == message_content:
                db_context = db_context[:-1]

        history = [
            {
                "role": msg["role"],
                "content": self._strip_timestamp_markers(str(msg["content"]))
            }
            for msg in db_context
            if msg["role"] in ("user", "assistant")
        ]

        # --- 调用 smart 模型生成回复（流式收集完整文本，检测标签闭合） ---
        response_text = ""
        usage_data = None
        max_attempts = 2
        for attempt in range(max_attempts):
            response_text = ""
            usage_data = None
            try:
                retry_system_prompt = system_prompt
                if attempt > 0:
                    retry_system_prompt += "\n\n【重试】确保所有成对标签完整闭合，特别是 [TASK_INSTRUCTION] ... [/TASK_INSTRUCTION]。不要输出缺失闭合的标签。"

                stream = self._chat_stream_wrapper(
                    self.llm,
                    chat_id,
                    system_prompt=retry_system_prompt,
                    user_prompt=user_prompt,
                    history=history,
                    tools=None,  # 前脑不使用工具
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict):
                        if chunk["type"] == "text":
                            response_text += chunk["content"]
                        elif chunk["type"] == "usage":
                            usage_data = {
                                "input_tokens": chunk.get("input_tokens", 0),
                                "output_tokens": chunk.get("output_tokens", 0),
                            }
                    elif isinstance(chunk, str):
                        response_text += chunk
            except Exception as e:
                logger.error(f"[{chat_id}] 前脑生成失败: {e}", exc_info=True)
                # 前脑失败时回退到后脑
                return {"needs_backend": True, "user_reply": "", "raw_response": ""}

            # 清理思考标签
            response_text = self._strip_thinking_content(response_text)

            issues = self._find_unclosed_block_tags(response_text)
            if issues:
                logger.warning(f"[{chat_id}] 前脑输出存在未闭合标签 {issues}，第 {attempt+1}/{max_attempts} 次尝试重试。")
                if attempt < max_attempts - 1:
                    continue
                # 最后一轮，清理未闭合标签再继续解析，避免污染用户侧
                response_text = self._clean_unclosed_block_tags(response_text)
            break

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
        logger.info(
            f"[{chat_id}] 前脑结果: needs_backend={parsed['needs_backend']}, "
            f"should_reply={parsed.get('should_reply')}, "
            f"reply='{parsed['user_reply'][:80]}...'"
        )
        await self._send_debug(
            chat_id,
            f"⚡ 前脑: needs_backend={parsed['needs_backend']}\n"
            f"should_reply={parsed.get('should_reply')}\n"
            f"任务指示: {parsed.get('task_instruction', '')[:200]}\n"
            f"回复: {parsed['user_reply'][:200]}"
        )

        return {
            "needs_backend": parsed["needs_backend"],
            "user_reply": parsed["user_reply"],
            "task_instruction": parsed.get("task_instruction", ""),
            "should_reply": parsed.get("should_reply", True),
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
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        chat_type = context.get("chat_type", "private")
        storage_id = chat_id if chat_type != "private" else user_id

        logger.info(f"[{chat_id}] 前脑审查: 后脑结果 {len(backend_result)} 字, 新用户消息 {len(new_user_messages)} 条")

        # --- 构建审查 prompt ---
        soul_prompt = get_soul_prompt()
        identity_context = load_identity_context(include_schedule=False)
        schedule_block = ""
        custom_block = ""
        tool_intro_block = ""
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
            from brain.tools import TOOL_INTROS
            if TOOL_INTROS:
                tool_desc = "\n".join([f"- {name}: {intro}" for name, intro in TOOL_INTROS.items()])
                tool_intro_block = render_template('context_injection.jinja', 'tools', tool_desc=tool_desc)
        except Exception:
            pass

        system_prompt = render_template(
            'front_brain_review.jinja',
            'system',
            soul_prompt=soul_prompt,
            identity_context=identity_context,
            schedule_block=schedule_block,
            custom_block=custom_block,
            tool_intro_block=tool_intro_block,
        )
        
        user_prompt = render_template(
            'front_brain_review.jinja',
            'user',
            backend_result=backend_result,
            new_user_messages=new_user_messages,
        )

        # 仅取当前对话段的上下文；过长时再截断
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        current_session_msgs = [
            {
                "role": msg["role"],
                "content": self._strip_timestamp_markers(str(msg["content"]))
            }
            for msg in db_context
            if msg.get("role") in ("user", "assistant")
        ]

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

        # 若当前消息段过长（>40），仅保留最近 40 条
        if len(current_session_msgs) > 40:
            current_session_msgs = current_session_msgs[-40:]

        history = current_session_msgs

        # 调用 smart 模型审查
        response_text = ""
        usage_data = None
        try:
            stream = self._chat_stream_wrapper(
                self.llm,
                chat_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=history,
                tools=None,
            )
            async for raw_chunk in stream:
                chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                if isinstance(chunk, dict):
                    if chunk["type"] == "text":
                        response_text += chunk["content"]
                    elif chunk["type"] == "usage":
                        usage_data = {
                            "input_tokens": chunk.get("input_tokens", 0),
                            "output_tokens": chunk.get("output_tokens", 0),
                        }
                elif isinstance(chunk, str):
                    response_text += chunk
        except Exception as e:
            logger.error(f"[{chat_id}] 前脑审查失败: {e}", exc_info=True)
            return {"action": "done", "user_reply": ""}

        # 清理思考标签
        response_text = self._strip_thinking_content(response_text)

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
                context="front_brain_review",
            )

        # 解析审查结果
        parsed = parse_front_brain_review(response_text)
        logger.info(
            f"[{chat_id}] 前脑审查结果: action={parsed['action']}, "
            f"should_reply={parsed.get('should_reply')}, "
            f"reply='{parsed['user_reply'][:80]}...'"
        )
        await self._send_debug(
            chat_id,
            f"🔍 审查: action={parsed['action']}\n"
            f"should_reply={parsed.get('should_reply')}\n"
            f"任务指示: {parsed.get('task_instruction', '')[:200]}\n"
            f"回复: {parsed['user_reply'][:200]}"
        )

        return parsed
