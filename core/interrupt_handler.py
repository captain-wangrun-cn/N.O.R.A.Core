'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:29:23
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""中断处理 Mixin — 忙碌时的意图检测 + 后端打断。"""

import asyncio
import logging
from typing import Dict, Any

from brain.prompts import get_soul_prompt, render_template, load_custom_prompt, should_inject_custom
from core.worker_status import WorkerStatus

logger = logging.getLogger(__name__)


class InterruptHandlerMixin:
    """后端忙碌时的用户意图检测与任务打断。"""

    async def _detect_interrupt_intent_and_reply(self, chat_id: str, text: str, status: WorkerStatus) -> Dict[str, str]:
        """
        用 fast 模型智能判断用户意图并生成回复。
        
        Returns:
            {
                "action": "stop" | "change" | "queue",  # 要执行的操作
                "reply": "...",                          # 给用户的回复文案
            }
        """
        progress = status.get_summary()
        
        # 获取当前队列摘要，让 AI 看到已排队的任务
        queue = self.task_queues.get(chat_id)
        queue_summary = queue.get_queue_summary() if queue else ""
        
        soul = get_soul_prompt()
        prompt = render_template('interrupt_detect.jinja', 'user',
                                 progress=progress, user_text=text,
                                 queue_summary=queue_summary)
        
        # 带入最近几条对话历史，让 fast_llm 理解上下文（而不是只看当前这一句）
        recent_history = []
        try:
            session = self.sessions.get(chat_id, {})
            session_history = session.get("history", [])
            # 取最近 6 条（3 轮对话），足够理解上下文
            recent_history = [
                {"role": h["role"], "content": h["content"]}
                for h in session_history[-6:]
                if h.get("role") in ("user", "assistant") and h.get("content")
            ]
        except Exception:
            recent_history = []
        
        try:
            for attempt in range(3):
                response = ""
                system_prompt = render_template('interrupt_detect.jinja', 'system', soul=soul)
                if should_inject_custom("fast"):
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

                stream = self.fast_llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    history=recent_history,
                    tools=[]
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
                            reply = f"收到～ 我正在处理之前的请求（{status.phase}），完成后马上看你的新消息！"

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
                "reply": f"收到～ 我正在处理之前的请求（{status.phase}），完成后马上看你的新消息！"
            }
        
        except Exception as e:
            logger.warning(f"[{chat_id}] 意图检测失败: {e}，fallback 到 queue")
            return {
                "action": "queue",
                "reply": "收到～ 我正在忙着处理之前的请求，完成后会看你的新消息！"
            }

    async def _interrupt_backend(self, chat_id: str, reason: str, user_text: str, skip_reply: bool = False):
        """
        前端打断后端：取消当前任务，清理状态，根据 reason 决定下一步。
        
        reason:
            "stop"   - 停止并告知用户已停止
            "change" - 停止后立即处理新消息
        skip_reply: 是否跳过回复（调用方已经发过回复）
        """
        logger.info(f"[{chat_id}] 前端打断后端: reason={reason}, text='{user_text[:50]}'")
        
        # 设置打断标记，防止 finally 块自动处理排队消息
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        session["_interrupted_by_frontend"] = True
        
        # 1. 取消后端任务
        task = self.generation_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            # 等待任务真正结束（短超时，避免阻塞前端过久）
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        
        # 2. 清理状态
        status = self.worker_status.get(chat_id)
        if status:
            status.finish()
        
        # 3. 清空排队消息（用户已改变意图，旧队列失效）
        queue = self.task_queues.get(chat_id)
        if queue:
            await queue.clear()
        
        # 4. 根据 reason 决定下一步（如果调用方已回复，则跳过）
        if skip_reply:
            # 调用方已发送回复，直接执行后续操作
            if reason == "change":
                # 启动新任务处理 user_text
                context = {
                    "chat_id": chat_id,
                    "user_id": chat_id,  # Fallback, might need better user_id handling
                    "text": user_text,
                    "chat_type": "private"  # Fallback
                }
                task = asyncio.create_task(self._run_polling_loop(context))
                self.generation_tasks[chat_id] = task
        else:
            # 需要生成并发送回复（兼容旧调用方式）
            if reason == "stop":
                reply = "好的，已经停下来了～"
                await self.adapter.send_message(chat_id, reply)
                self.message_history.add_message(
                    platform="telegram", chat_id=chat_id,
                    role="assistant", content=reply
                )
            elif reason == "change":
                reply = "好，马上切换～"
                await self.adapter.send_message(chat_id, reply)
                self.message_history.add_message(
                    platform="telegram", chat_id=chat_id,
                    role="assistant", content=reply
                )
                # 启动新任务
                context = {
                    "chat_id": chat_id,
                    "user_id": chat_id,  # Fallback
                    "text": user_text,
                    "chat_type": "private"  # Fallback
                }
                task = asyncio.create_task(self._run_polling_loop(context))
                self.generation_tasks[chat_id] = task
