'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:35:13
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""轮询 Mixin — 前后脑轮询主循环 + 排队消息处理。"""

import asyncio
import logging
import time
from typing import Dict, Any

logger = logging.getLogger(__name__)


class PollingMixin:
    """前后脑轮询主循环 + 排队消息出队处理。"""

    MAX_POLLING_ROUNDS = 5  # 最大轮询轮数，防止无限循环

    async def _run_polling_loop(self, context: Dict[str, Any]):
        """
        前后脑轮询主循环。
        
        流程：
        1. 后脑执行任务（_generate_response）
        2. 后脑完成后，前脑审查结果 + 收集期间用户新消息
        3. 前脑判断：
           - [TASK_DONE] → 结束轮询
           - [NEED_BACKEND] → 构建新 context 继续后脑
           - 纯聊天 → 结束轮询
        4. 重复 1-3 直到结束或达到最大轮数
        
        用户新消息处理策略：
        - 后脑执行期间：用户消息由 busy handler 保存到 DB + queue
        - 前脑审查时：从 DB 获取后脑启动后的新用户消息注入审查上下文
        - 前脑生成期间到达的新消息：等前脑完成后在下一轮审查中处理
        """
        chat_id = context["chat_id"]
        
        # 标记轮询模式，让 _generate_response 的 finally 不自动处理队列
        context = context.copy()
        context["_in_polling_loop"] = True
        
        try:
            for polling_round in range(1, self.MAX_POLLING_ROUNDS + 1):
                logger.info(f"[{chat_id}] 轮询第 {polling_round}/{self.MAX_POLLING_ROUNDS} 轮: 启动后脑")
                await self._send_debug(chat_id, f"🔄 轮询第 {polling_round}/{self.MAX_POLLING_ROUNDS} 轮")
                
                # 记录后脑启动时间戳，用于后续获取期间的新用户消息
                backend_start_ts = time.time()
                
                # --- 后脑执行 ---
                await self._generate_response(context)
                
                # 后脑执行完毕，检查是否被打断
                if self._was_interrupted(chat_id):
                    logger.info(f"[{chat_id}] 轮询第 {polling_round} 轮: 后脑被打断，终止轮询")
                    return  # 被打断时不进入审查
                
                # --- 前脑审查 ---
                chat_type = context.get("chat_type", "private")
                user_id = context["user_id"]
                storage_id = chat_id if chat_type != "private" else user_id
                
                # 获取后脑执行期间用户发送的新消息
                new_user_msgs = self.message_history.get_messages_since(
                    "telegram", storage_id, backend_start_ts, role="user"
                )
                new_user_texts = [msg["content"] for msg in new_user_msgs]
                
                # 获取后脑的最终回复（从 session history 中取最后一条 assistant 消息）
                session = self.sessions.get(chat_id, {})
                session_history = session.get("history", [])
                backend_result = ""
                for h in reversed(session_history):
                    if h.get("role") == "assistant":
                        backend_result = h["content"]
                        break

                # 追加后脑执行过的工具操作记录，供前脑审查与转述
                status = self.worker_status.get(chat_id)
                if status and status.tool_history_readable:
                    compact_steps = []
                    for step in status.tool_history_readable:
                        step = str(step).strip()
                        if not step:
                            continue
                        if len(step) > 40:
                            if "(" in step and ")" in step:
                                prefix, rest = step.split("(", 1)
                                args, suffix = rest.rsplit(")", 1)
                                if len(args) > 20:
                                    args = args[:17] + "..."
                                step = f"{prefix}({args}){suffix}"
                            elif ":" in step:
                                head, tail = step.split(":", 1)
                                tail = tail.strip()
                                if len(tail) > 20:
                                    tail = tail[:17] + "..."
                                step = f"{head}: {tail}"
                            if len(step) > 60:
                                step = step[:57] + "..."
                        compact_steps.append(step)
                    tool_lines = "\n".join([f"- {step}" for step in compact_steps])
                    backend_result = (
                        f"{backend_result}\n\n【后脑执行的操作】\n{tool_lines}"
                        if backend_result
                        else f"【后脑执行的操作】\n{tool_lines}"
                    )
                
                if not backend_result:
                    logger.info(f"[{chat_id}] 轮询第 {polling_round} 轮: 后脑无输出，结束轮询")
                    break
                
                # 如果没有用户新消息且后脑正常完成，默认结束轮询（无需前脑审查）
                if not new_user_texts and polling_round < self.MAX_POLLING_ROUNDS:
                    pass  # 继续执行下面的审查逻辑
                
                # 前脑审查
                review_result = await self._front_brain_review(
                    context, backend_result, new_user_texts
                )
                
                action = review_result["action"]
                review_reply = review_result.get("user_reply", "")
                task_instruction = review_result.get("task_instruction", "")
                should_reply = review_result.get("should_reply", True)
                
                # 发送前脑审查回复给用户（如果有实质内容）
                if review_reply and should_reply:
                    parts = self._SPLIT_MARKER_PATTERN.split(review_reply)
                    for part in parts:
                        part = part.strip()
                        if part:
                            await self.adapter.send_message(chat_id, part)
                    
                    # 保存到数据库
                    self.message_history.add_message(
                        platform="telegram",
                        chat_id=storage_id,
                        role="assistant",
                        content=review_reply,
                        user_id="assistant",
                    )
                    # 同步到 session history
                    session_history = session.setdefault("history", [])
                    session_history.append({"role": "assistant", "content": review_reply})
                    if len(session_history) > 20:
                        session["history"] = session_history[-20:]
                
                logger.info(f"[{chat_id}] 轮询第 {polling_round} 轮审查结果: action={action}")
                await self._send_debug(chat_id, f"🔍 审查结果: action={action}")
                
                if action == "continue":
                    # 需要后脑继续 — 构建新 context
                    new_instruction = review_reply if review_reply else "请继续处理。"
                    if task_instruction:
                        new_instruction += f"\n\n【任务指示】\n{task_instruction}"
                    if new_user_texts:
                        new_instruction += "\n\n用户新消息:\n" + "\n".join(new_user_texts)
                    
                    context = context.copy()
                    context["text"] = new_instruction
                    context["_front_brain_handled"] = True
                    context["_front_brain_reply"] = review_reply if should_reply else ""
                    context["task_instruction"] = task_instruction
                    # 消费掉队列中对应的消息
                    queue = self.task_queues.get(chat_id)
                    if queue and new_user_texts:
                        consumed = 0
                        while consumed < len(new_user_texts) and queue.size() > 0:
                            await queue.dequeue()
                            consumed += 1
                    
                    logger.info(f"[{chat_id}] 轮询继续: 新指示='{new_instruction[:60]}...'")
                    continue
                else:
                    # action == "done" or "chat" → 结束轮询
                    logger.info(f"[{chat_id}] 轮询结束: action={action}")
                    break
            
            else:
                # for-else: 达到最大轮数
                logger.warning(f"[{chat_id}] 轮询达到最大轮数 {self.MAX_POLLING_ROUNDS}，强制结束")
                await self._send_debug(chat_id, f"⚠️ 轮询达到最大轮数 {self.MAX_POLLING_ROUNDS}")
        
        except asyncio.CancelledError:
            logger.info(f"[{chat_id}] 轮询循环被取消")
            raise
        
        except Exception as e:
            logger.error(f"[{chat_id}] 轮询循环异常: {e}", exc_info=True)
        
        finally:
            if not self._was_interrupted(chat_id):
                await self._process_pending_messages(chat_id)

    async def _process_pending_messages(self, chat_id: str):
        """后端完成后，从任务队列中取出下一个任务执行。逐个处理，不合并。"""
        queue = self.task_queues.get(chat_id)
        if not queue or queue.size() == 0:
            return
        
        # 取出下一个任务
        item = await queue.dequeue()
        if not item:
            return
        
        context = item["context"]
        remaining = queue.size()
        logger.info(f"[{chat_id}] 开始处理队列中的下一个任务 (剩余 {remaining} 个): '{context.get('text', '')[:50]}'")
        
        # 如果还有排队任务，通知用户
        if remaining > 0:
            try:
                await self.adapter.send_message(
                    chat_id,
                    f"📋 开始处理排队任务... (还有 {remaining} 个任务在等待)",
                    parse_media=False
                )
            except Exception:
                pass
        
        # 启动新任务处理（走完整的生成流程，finally 块会继续处理下一个）
        task = asyncio.create_task(self._run_polling_loop(context))
        self.generation_tasks[chat_id] = task
