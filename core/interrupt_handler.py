'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:29:23
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""中断处理 Mixin — 忙碌时的意图检测 + 后端打断。"""

import asyncio
import logging
import re
from typing import Dict, Any, Optional

from brain.prompts import (
    get_soul_prompt,
    render_template,
    load_custom_prompt,
    should_inject_custom,
    load_identity_context,
)
from core.routing import strip_timestamp_markers
from core.scene_context import build_current_scene_block, should_redact_cross_place_details
from core.worker_status import WorkerStatus

logger = logging.getLogger(__name__)


class InterruptHandlerMixin:
    """后端忙碌时的用户意图检测与任务打断。"""

    @classmethod
    def _strip_timestamp_markers(cls, text: str) -> str:
        return strip_timestamp_markers(text)

    async def _detect_interrupt_intent_and_reply(
        self,
        chat_id: str,
        text: str,
        status: Optional[WorkerStatus],
        backend_runtime: str = "",
    ) -> Dict[str, str]:
        """
        用 smart 模型智能判断用户意图并生成回复。
        
        Returns:
            {
                "action": "stop" | "change" | "queue",  # 要执行的操作
                "reply": "...",                          # 给用户的回复文案
            }
        """
        identity = None
        try:
            identity = self._identity_for_runtime_key(chat_id)
        except Exception:
            identity = None

        redact_progress = bool(
            identity
            and backend_runtime
            and should_redact_cross_place_details(identity, backend_runtime)
        )
        if redact_progress:
            progress = (
                f"后脑正在另一个窗口执行任务（来源地点: {backend_runtime}）。"
                "当前窗口只能知道其它窗口忙碌，不能看到那边的任务内容、文件、结果或进度细节。"
            )
        else:
            progress = (
                status.get_summary()
                if status
                else "后脑当前没有正在执行的任务，但共享后脑队列里已有等待处理的任务。"
            )
        
        # 获取当前队列摘要，让 AI 看到已排队的任务
        queue = self._get_scope_queue_for_runtime(chat_id)
        queue_summary = queue.get_queue_summary() if queue else ""
        if identity and queue:
            try:
                redacted = 0
                visible_lines = []
                for item in getattr(queue, "_items", []) or []:
                    item_context = item.get("context") or {}
                    item_identity = self._identity(item_context)
                    if should_redact_cross_place_details(identity, item_identity.runtime_key):
                        redacted += 1
                        continue
                    text_preview = str(item.get("text") or item_context.get("text") or "")[:60]
                    instr = str(item_context.get("task_instruction") or "").strip()
                    visible_lines.append(f"- {text_preview}")
                    if instr:
                        visible_lines.append(f"  指示: {instr[:120]}")
                if redacted:
                    queue_summary = "\n".join(visible_lines)
                    hidden = f"另有 {redacted} 个其它窗口的排队任务，内容已隐藏。"
                    queue_summary = f"{queue_summary}\n{hidden}".strip()
            except Exception:
                pass
        
        soul = get_soul_prompt()
        prompt = render_template('interrupt_detect.jinja', 'user',
                                 progress=progress, user_text=text,
                                 queue_summary=queue_summary)
        
        # 带入最近对话历史，让 smart_llm 理解上下文（而不是只看当前这一句）
        recent_history = []
        try:
            identity = identity or self._identity_for_runtime_key(chat_id)
            db_context = self.message_history.get_context_messages(
                identity.platform, identity.storage_id,
                memory_scope_id=identity.memory_scope_id, current_place_scope_id=identity.place_scope_id,
            )
            if len(db_context) > 20:
                db_context = db_context[-20:]
            recent_history = [
                {
                    "role": msg.get("role"),
                    "content": self._strip_timestamp_markers(str(msg.get("content", ""))),
                }
                for msg in db_context
                if msg.get("role") in ("user", "assistant") and msg.get("content")
            ]
        except Exception:
            recent_history = []
        
        try:
            for attempt in range(3):
                response = ""
                system_prompt = render_template('interrupt_detect.jinja', 'system', soul=soul)
                identity_context = load_identity_context(
                    include_schedule=False,
                    actor_display_name=identity.actor_display_name if identity else "",
                    is_owner=identity.is_owner if identity else True,
                    chat_type=identity.chat_type if identity else "private",
                )
                if identity_context:
                    system_prompt = identity_context + "\n\n" + system_prompt
                if identity:
                    system_prompt = system_prompt + "\n\n---\n" + build_current_scene_block(identity)
                if should_inject_custom("smart"):
                    custom_prompt = load_custom_prompt()
                    if custom_prompt:
                        system_prompt = (
                            system_prompt
                            + "\n\n"
                            + render_template('context_injection.jinja', 'custom', custom_content=custom_prompt)
                        )
                if attempt > 0:
                    system_prompt += (
                        "\n\n【重试】严格只输出以下格式："
                        "ACTION: <stop|change|queue|list_queue|cancel_queue:N|clear_queue> | REPLY: <一句话>。"
                        "禁止输出其他任何内容。"
                    )

                stream = self._chat_stream_wrapper(
                    self.llm,
                    chat_id,
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    history=recent_history,
                    tools=[],
                )
                async for chunk in stream:
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        response += chunk["content"]
                    elif isinstance(chunk, str):
                        response += chunk

                # 解析 LLM 输出
                response = response.strip()
                if "|" in response and "ACTION:" in response.upper():
                    parts = response.split("|", 1)
                    action_part = parts[0].strip()
                    reply_part = parts[1].strip() if len(parts) > 1 else ""

                    # 提取 ACTION（支持带参数格式如 cancel_queue:2）
                    action = "queue"  # 默认
                    action_param = None
                    if "ACTION:" in action_part.upper():
                        action_text = action_part.split(":", 1)[1].strip().lower()
                        if "stop" in action_text:
                            action = "stop"
                        elif "change" in action_text:
                            action = "change"
                        elif "clear_queue" in action_text:
                            action = "clear_queue"
                        elif "cancel_queue" in action_text:
                            action = "cancel_queue"
                            # 提取序号：cancel_queue:N
                            import re
                            num_match = re.search(r'(\d+)', action_text)
                            if num_match:
                                action_param = int(num_match.group(1))
                        elif "list_queue" in action_text:
                            action = "list_queue"

                    # 提取 REPLY
                    reply = reply_part
                    if "REPLY:" in reply.upper():
                        reply = reply.split(":", 1)[1].strip()

                    if not reply:
                        # fallback 回复
                        if action == "stop":
                            reply = "好的，已经停下来了～"
                        elif action == "change":
                            reply = "好，马上切换～"
                        elif action == "list_queue":
                            reply = "让我看看当前的情况～"
                        elif action == "cancel_queue":
                            reply = "好的，帮你取消了～"
                        elif action == "clear_queue":
                            reply = "好的，排队的任务都清掉了～"
                        else:
                            phase = status.phase if status else "排队任务"
                            reply = f"收到～ 我正在处理之前的请求（{phase}），完成后马上看你的新消息！"

                    result = {"action": action, "reply": reply}
                    if action_param is not None:
                        result["param"] = action_param
                    logger.info(f"[{chat_id}] LLM判断意图: action={action}, param={action_param}, reply='{reply[:50]}'")
                    return result

                if attempt == 0:
                    logger.warning(f"[{chat_id}] 意图解析失败，准备重试。LLM输出: {response[:100]}")
                else:
                    logger.warning(f"[{chat_id}] 意图解析再次失败，fallback 到 queue。LLM输出: {response[:100]}")

            return {
                "action": "queue",
                "reply": f"收到～ 我正在处理之前的请求（{status.phase if status else '排队任务'}），完成后马上看你的新消息！"
            }
        
        except Exception as e:
            logger.warning(f"[{chat_id}] 意图检测失败: {e}，fallback 到 queue")
            return {
                "action": "queue",
                "reply": "收到～ 我正在忙着处理之前的请求，完成后会看你的新消息！"
            }

    async def _interrupt_backend(
        self,
        chat_id: str,
        reason: str,
        user_text: str,
        skip_reply: bool = False,
        target_runtime_key: str = "",
        source_context: Optional[Dict[str, Any]] = None,
    ):
        """
        前端打断后端：取消当前任务，清理状态，根据 reason 决定下一步。
        
        reason:
            "stop"   - 停止并告知用户已停止
            "change" - 停止后立即处理新消息
        skip_reply: 是否跳过回复（调用方已经发过回复）
        """
        logger.info(f"[{chat_id}] 前端打断后端: reason={reason}, text='{user_text[:50]}'")
        identity = self._identity_for_runtime_key(chat_id)
        target_runtime_key = target_runtime_key or self.get_busy_backend_runtime(identity.memory_scope_id) or chat_id
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id

        # 设置打断标记，防止 finally 块自动处理排队消息
        session = self.sessions.setdefault(target_runtime_key, {"history": [], "interrupted_thought": "", "pending_text": ""})
        session["_interrupted_by_frontend"] = True
        
        # 1. 取消后端任务
        task = self.generation_tasks.get(target_runtime_key)
        if task and not task.done():
            task.cancel()
            # 等待任务真正结束（短超时，避免阻塞前端过久）
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self.generation_tasks.pop(target_runtime_key, None)
        
        # 2. 清理状态
        status = self.worker_status.get(target_runtime_key)
        if status:
            status.finish()
        
        # 3. 清空排队消息（用户已改变意图，旧队列失效）
        queue = self._get_scope_queue_for_runtime(chat_id)
        if queue:
            await queue.clear()

        self._cancel_followup_timer(chat_id)
        
        # 4. 根据 reason 决定下一步（如果调用方已回复，则跳过）
        if skip_reply:
            # 调用方已发送回复，直接执行后续操作
            if reason == "change":
                # 启动新任务处理 user_text
                context = dict(source_context or {})
                context.update({
                    "platform": platform,
                    "chat_id": identity.platform_chat_id,
                    "user_id": identity.actor_user_id,
                    "text": user_text,
                    "chat_type": identity.chat_type,
                    "user_name": identity.actor_display_name,
                })
                task = asyncio.create_task(self._run_polling_loop(context))
                self.generation_tasks[chat_id] = task
        else:
            # 需要生成并发送回复（兼容旧调用方式）
            if reason == "stop":
                reply = "好的，已经停下来了～"
                await self._send_platform_message(chat_id, reply)
                self.message_history.add_message(
                    platform=platform, chat_id=storage_id,
                    role="assistant", content=reply,
                    memory_scope_id=memory_scope_id, place_scope_id=place_scope_id,
                )
            elif reason == "change":
                reply = "好，马上切换～"
                await self._send_platform_message(chat_id, reply)
                self.message_history.add_message(
                    platform=platform, chat_id=storage_id,
                    role="assistant", content=reply,
                    memory_scope_id=memory_scope_id, place_scope_id=place_scope_id,
                )
                # 启动新任务
                context = dict(source_context or {})
                context.update({
                    "platform": platform,
                    "chat_id": identity.platform_chat_id,
                    "user_id": identity.actor_user_id,
                    "text": user_text,
                    "chat_type": identity.chat_type,
                    "user_name": identity.actor_display_name,
                })
                task = asyncio.create_task(self._run_polling_loop(context))
                self.generation_tasks[chat_id] = task
