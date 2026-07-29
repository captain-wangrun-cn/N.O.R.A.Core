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

from core.message_dedup import clear_persisted_user_messages
from core.message_handler import reply_target_message_id

logger = logging.getLogger(__name__)


class PollingMixin:
    """前后脑轮询主循环 + 排队消息出队处理。"""

    MAX_POLLING_ROUNDS = 5  # 最大轮询轮数，防止无限循环

    @staticmethod
    def _compact_polling_handoff(text: str, limit: int = 6000) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "\n...（轮询接力上下文已截断）"

    def _clear_polling_backend_context(self, chat_id: str) -> None:
        session = self.sessions.get(chat_id)
        if isinstance(session, dict):
            clear_fn = getattr(self, "_clear_polling_backend_history", None)
            if callable(clear_fn):
                clear_fn(session)
            else:
                session.pop("_polling_backend_history", None)

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
        identity = self._identity(context)
        chat_id = identity.runtime_key
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id

        # 标记轮询模式，让 _generate_response 的 finally 不自动处理队列
        context = context.copy()
        context["_in_polling_loop"] = True
        polling_finished = False
        
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
                # 获取后脑执行期间用户发送的新消息
                new_user_msgs = self.message_history.get_messages_since(
                    platform, storage_id, backend_start_ts, role="user", memory_scope_id=memory_scope_id
                )
                new_user_texts = [
                    self._strip_timestamp_markers(str(msg["content"]))
                    for msg in new_user_msgs
                ]

                # 审查阶段不再夹带用户在后脑执行期间的新提问，避免重复回复
                if new_user_texts:
                    logger.info(f"[{chat_id}] 轮询审查丢弃后脑期间的 {len(new_user_texts)} 条用户消息，避免重复回复")
                    new_user_texts = []
                
                # 获取后脑的最终回复（从 session history 中取最后一条 assistant 消息）
                session = self.sessions.get(chat_id, {})
                session_history = session.get("_polling_backend_history") or session.get("history", [])
                backend_result = ""
                for h in reversed(session_history):
                    if h.get("role") == "assistant":
                        backend_result = self._strip_timestamp_markers(str(h["content"]))
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

                # 追加关键结果摘要（如路径/ID/主要输出），便于前脑转述
                if status and status.key_results:
                    key_lines = []
                    for item in status.key_results[-5:]:  # 仅取最近 5 条关键结果
                        item_str = str(item).strip()
                        if not item_str:
                            continue
                        if len(item_str) > 300:
                            item_str = item_str[:297] + "..."
                        key_lines.append(f"- {item_str}")
                    if key_lines:
                        key_block = "\n".join(key_lines)
                        backend_result = (
                            f"{backend_result}\n\n【关键结果】\n{key_block}"
                            if backend_result else f"【关键结果】\n{key_block}"
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
                need_follow = review_result.get("need_follow")
                review_reply = review_result.get("user_reply", "")
                task_instruction = review_result.get("task_instruction", "")
                should_reply = review_result.get("should_reply", True)
                if context.get("no_reply"):
                    should_reply = False
                
                # 发送前脑审查回复给用户（如果有实质内容）
                if review_reply and should_reply:
                    send_target = self.resolve_send_target(
                        review_result.get("route"), identity, route_source="front_brain_review"
                    )
                    send_key = send_target.get("runtime_key")
                    if not send_key:
                        logger.warning(f"[{chat_id}] 前脑审查请求的投递目标被拒绝，跳过用户可见回复。")
                        continue
                    send_chat_type = send_target.get("chat_type") or context.get("chat_type", "")
                    redirected = bool(send_target.get("redirected"))
                    dest_identity = send_target.get("identity") or identity
                    reply_id = "" if redirected else reply_target_message_id(context)
                    sent_ids = await self._send_split_message(
                        send_key,
                        review_reply,
                        reply_to_message_id=reply_id,
                        chat_type=send_chat_type,
                    )

                    # 保存到数据库（重定向后按目标场景身份归属）
                    log_platform = dest_identity.platform if redirected else platform
                    log_storage_id = dest_identity.storage_id if redirected else storage_id
                    log_memory_scope_id = dest_identity.memory_scope_id if redirected else memory_scope_id
                    log_place_scope_id = dest_identity.place_scope_id if redirected else place_scope_id
                    self.message_history.add_message(
                        platform=log_platform,
                        chat_id=log_storage_id,
                        role="assistant",
                        content=review_reply,
                        user_id="assistant",
                        metadata={"source": "polling_review", "platform_message_ids": sent_ids},
                        memory_scope_id=log_memory_scope_id,
                        place_scope_id=log_place_scope_id,
                    )
                    # 同步到 session history
                    if not action == "continue":
                        session_history = session.setdefault("history", [])
                        session_history.append({"role": "assistant", "content": review_reply})
                        if len(session_history) > 20:
                            session["history"] = session_history[-20:]
                
                logger.info(f"[{chat_id}] 轮询第 {polling_round} 轮审查结果: action={action}")
                await self._send_debug(chat_id, f"🔍 审查结果: action={action}")

                apply_presence = getattr(self, "_apply_presence_markers", None)
                if callable(apply_presence):
                    presence_ended = await apply_presence(
                        chat_id,
                        identity.chat_type,
                        force_online=bool(review_result.get("force_online")),
                        force_semi_online=bool(review_result.get("force_semi_online")),
                    )
                else:
                    presence_ended = bool(review_result.get("force_semi_online"))
                    if presence_ended:
                        self._transition_to_semi_online(chat_id)
                if presence_ended:
                    polling_finished = True
                    break
                
                if action == "continue":
                    if polling_round >= self.MAX_POLLING_ROUNDS:
                        logger.warning(
                            f"[{chat_id}] 轮询第 {polling_round} 轮仍请求继续，"
                            f"已达到最大轮数 {self.MAX_POLLING_ROUNDS}，停止继续下发。"
                        )
                        await self._send_debug(
                            chat_id,
                            f"⚠️ 轮询第 {polling_round} 轮仍请求继续，但已达到最大轮数 {self.MAX_POLLING_ROUNDS}"
                        )
                        polling_finished = True
                        break

                    # 需要后脑继续 — 构建新 context
                    new_instruction = review_reply if review_reply else "请继续处理。"
                    if task_instruction:
                        new_instruction += f"\n\n【任务指示】\n{task_instruction}"
                    # 审查阶段已丢弃后脑期间的用户消息，避免重复回复
                    
                    context = context.copy()
                    context["text"] = new_instruction
                    context["_front_brain_handled"] = True
                    # 本轮输入已被改写成"继续处理"的指示，原用户消息回归普通历史；
                    # 清除其行 id 记录，避免后脑把这条真实历史误当成当前输入剔除。
                    clear_persisted_user_messages(context)
                    context["_front_brain_reply"] = review_reply if should_reply else ""
                    context["task_instruction"] = task_instruction
                    context["_polling_previous_backend_result"] = self._compact_polling_handoff(backend_result)
                    context["use_image_model"] = review_result.get("use_image_model", context.get("use_image_model", False))
                    if need_follow:
                        context["_followup_initial_delay"] = self.FOLLOWUP_NEED_FOLLOW_DELAY
                    # 消费掉队列中对应的消息
                    queue = self._get_scope_queue_for_runtime(chat_id)
                    
                    logger.info(f"[{chat_id}] 轮询继续: 新指示='{new_instruction[:60]}...'")
                    continue
                else:
                    # action == "done" or "chat" → 结束轮询
                    logger.info(f"[{chat_id}] 轮询结束: action={action}")
                    polling_finished = True
                    if need_follow:
                        # 用 NEED_FOLLOW 覆盖后续跟进的首次间隔
                        self._start_followup_timer(chat_id, initial_delay=self.FOLLOWUP_NEED_FOLLOW_DELAY)
                    break
            
            else:
                # for-else: 达到最大轮数
                logger.warning(f"[{chat_id}] 轮询达到最大轮数 {self.MAX_POLLING_ROUNDS}，强制结束")
                await self._send_debug(chat_id, f"⚠️ 轮询达到最大轮数 {self.MAX_POLLING_ROUNDS}")
                polling_finished = True
        
        except asyncio.CancelledError:
            logger.info(f"[{chat_id}] 轮询循环被取消")
            raise
        
        except Exception as e:
            logger.error(f"[{chat_id}] 轮询循环异常: {e}", exc_info=True)
        
        finally:
            if polling_finished:
                self._clear_polling_backend_context(chat_id)
            if not self._was_interrupted(chat_id):
                await self._process_pending_messages(chat_id)

    async def _process_pending_messages(self, chat_id: str):
        """后端完成后，从任务队列中取出下一个任务执行。逐个处理，不合并。"""
        queue = self._get_scope_queue_for_runtime(chat_id)
        if not queue or queue.size() == 0:
            return
        
        # 取出下一个任务
        item = await queue.dequeue()
        if not item:
            return
        
        context = item["context"]
        identity = self._identity(context)
        target_chat_id = identity.runtime_key
        remaining = queue.size()
        logger.info(f"[{target_chat_id}] 开始处理队列中的下一个任务 (剩余 {remaining} 个): '{context.get('text', '')[:50]}'")
        
        # 如果还有排队任务，通知用户
        if remaining > 0:
            try:
                msg = f"📋 开始处理排队任务... (还有 {remaining} 个任务在等待)"
                await self._send_platform_message(target_chat_id, msg, parse_media=False)
                self.message_history.add_message(
                    platform=identity.platform,
                    chat_id=identity.storage_id,
                    role="assistant",
                    content=msg,
                    user_id="assistant",
                    memory_scope_id=identity.memory_scope_id,
                    place_scope_id=identity.place_scope_id,
                )
            except Exception:
                pass
        
        # 启动新任务处理（走完整的生成流程，finally 块会继续处理下一个）
        task = asyncio.create_task(self._run_polling_loop(context))
        self.generation_tasks[target_chat_id] = task
