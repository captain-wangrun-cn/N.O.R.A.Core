'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:28:21
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""消息处理 Mixin — handle_new_message + 命令路由。"""

import asyncio
import logging
import re
import time
from typing import Dict, Any, Optional

from brain.multimodal import extract_image_payloads
from brain.prompts import render_template, get_soul_prompt, load_identity_context
from core.routing import has_image_input
from core.scheduler import (
    AIPresence,
    set_ai_presence,
    record_user_activity,
    get_ai_state_summary,
)
from core.worker_status import BackendTaskQueue
import config

logger = logging.getLogger(__name__)


class MessageHandlerMixin:
    """处理来自适配器的新消息 / 命令路由。"""

    _CONFIRM_DECISION_PATTERN = re.compile(
        r"DECISION\s*:\s*(confirm|cancel|unclear|ignore)",
        re.IGNORECASE,
    )
    _CONFIRM_REPLY_PATTERN = re.compile(
        r"REPLY\s*:\s*(.*)",
        re.IGNORECASE | re.DOTALL,
    )

    def _append_busy_queue_summary(self, chat_id: str, reply: str) -> str:
        """后脑忙碌时，在给用户的回复后附带当前队列详情。"""
        text = (reply or "").strip()
        queue = self.task_queues.get(chat_id)
        queue_summary = queue.get_queue_summary() if queue else ""
        queue_block = queue_summary if queue_summary else "（暂无排队任务）"

        # 避免重复附加
        if "【后脑队列详情】" in text:
            return text

        if text:
            return f"{text}\n\n【后脑队列详情】\n{queue_block}"
        return f"【后脑队列详情】\n{queue_block}"

    async def handle_new_message(self, context: Dict[str, Any]):
        """处理来自适配器的新消息/命令。"""
        # copy context to allow safe mutation (inject multimodal payloads, flags, etc.)
        context = dict(context)
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")

        # 解析多模态输入：从 [image: ...] 中提取真实图片内容
        clean_text, multimodal_images = extract_image_payloads(text)
        image_input_detected = bool(multimodal_images) or has_image_input(text)
        if clean_text:
            text = clean_text

        # 将清洗后的文本和多模态图片回填到 context，避免后续丢失图片（尤其是带 caption 时）
        context["text"] = text
        if multimodal_images:
            context["multimodal_images"] = multimodal_images

        logger.debug(f"[{chat_id}] 收到新聚合消息: '{text[:50]}...'")
        
        # --- 更新 AI 在线状态：收到消息 → AI 进入在线 ---
        set_ai_presence(AIPresence.ONLINE)
        # 统一使用 chat_id 进行对话活跃度追踪，避免后续 followup 循环取用不同 key 导致计数/空闲时间错乱
        record_user_activity(chat_id)
        ai_state = get_ai_state_summary()
        logger.info(
            f"[{chat_id}] 当前在线状态: presence={ai_state['presence']}, "
            f"generating={ai_state['is_generating']}, backend_busy={ai_state['is_backend_busy']}"
        )
        # 取消该 chat 已有的对话延续定时器（用户回来了）
        self._cancel_followup_timer(chat_id)

        # 统一计算 storage id（私聊用 user_id，群聊用 chat_id）
        storage_id_for_scheduler = chat_id if chat_type != "private" else user_id

        if self.scheduler:
            # 自动设置 default_chat_id（首次消息时）
            if not self.scheduler.default_chat_id:
                self.scheduler.default_chat_id = storage_id_for_scheduler
                logger.info(f"Scheduler default_chat_id 已设置: {storage_id_for_scheduler}")

        if getattr(self, "trigger_manager", None) and not self.trigger_manager.default_chat_id:
            self.trigger_manager.default_chat_id = storage_id_for_scheduler
            logger.info(f"Trigger default_chat_id 已设置: {storage_id_for_scheduler}")
        
        # ── 命令分发 ──
        stripped = text.strip()

        if re.search(r"(?m)^\s*/start\b", stripped):
            await self._cmd_start(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/reload_models\b", stripped):
            await self._cmd_reload_models(chat_id)
            return

        if re.search(r"(?m)^\s*/clear\b", stripped):
            await self._cmd_clear(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/stop\b", stripped):
            await self._cmd_stop(chat_id)
            return

        if re.search(r"(?m)^\s*/regenerate_proactive\b", stripped):
            await self._cmd_regenerate_proactive(chat_id, stripped)
            return

        if re.search(r"(?m)^\s*/schedule_today\b", stripped):
            await self._cmd_schedule_today(chat_id)
            return

        if re.search(r"(?m)^\s*/status\b", stripped):
            await self._cmd_status(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/debug\b", stripped):
            await self._cmd_debug(chat_id)
            return

        if re.search(r"(?m)^\s*/custom_scope\b", stripped):
            await self._cmd_custom_scope(chat_id, stripped)
            return

        if re.search(r"(?m)^\s*/undo\b", stripped):
            await self._cmd_undo(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/set_stream\b", stripped) or re.search(r"(?m)^\s*/nonstream\b", stripped):
            await self._cmd_set_stream(chat_id, chat_type, user_id, stripped)
            return

        # 若存在“待确认入队”请求，优先处理确认结果
        if await self._try_handle_pending_backend_enqueue(context, text, chat_type, user_id, user_name):
            return

        # --- 前后端分离逻辑 ---
        status = self.worker_status.get(chat_id)
        queue = self.task_queues.get(chat_id)
        backend_busy_or_queued = bool((status and status.busy) or (queue and queue.size() > 0))

        if backend_busy_or_queued:
            # 后端忙碌：允许前脑继续生成，不强制回复“在忙”。
            # 仅在有图片时直接排队，其他情况交由前脑处理并按需打断/切换。
            storage_id = chat_id if chat_type != "private" else user_id
            message_content = f"{user_name}: {text}" if chat_type != "private" else text

            # 若包含图片输入，直接入队，避免图片丢失
            if image_input_detected:
                user_metadata = {}
                platform_msg_id = context.get("platform_message_id")
                if platform_msg_id:
                    user_metadata["platform_message_ids"] = [str(platform_msg_id)]

                queue = self.task_queues.setdefault(chat_id, BackendTaskQueue())
                await queue.enqueue({
                    "context": context,
                    "text": text,
                })
                busy_reply = self._append_busy_queue_summary(
                    chat_id,
                    "后端忙碌，已把图片需求排队，稍后处理哦～",
                )
                busy_msg_id = await self.adapter.send_message(chat_id, busy_reply)
                busy_md = {}
                if busy_msg_id:
                    busy_md["platform_message_ids"] = [str(busy_msg_id)]

                # 保存用户消息与提示
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="user",
                    content=message_content,
                    user_id=user_id,
                    metadata=user_metadata or None,
                )
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=busy_reply,
                    user_id="assistant",
                    metadata=busy_md or None,
                )
                context["_message_saved"] = True
                logger.info(f"[{chat_id}] 后端忙碌且检测到图片，消息已入队 (队列长度 {queue.size()})")
                return

            # 无图片：记录消息并运行意图检测，仅在需要打断/切换/队列操作时回复；
            # 普通 queue 场景不再强制提示“在忙”，让前脑即时回复。
            logger.info(f"[{chat_id}] 后端忙碌（允许前脑即时回复），记录消息后进行意图检测")
            user_metadata = {}
            platform_msg_id = context.get("platform_message_id")
            if platform_msg_id:
                user_metadata["platform_message_ids"] = [str(platform_msg_id)]

            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="user",
                content=message_content,
                user_id=user_id,
                metadata=user_metadata or None,
            )
            decision = await self._detect_interrupt_intent_and_reply(chat_id, text, status)
            action = decision.get("action", "queue")
            reply = decision.get("reply", "")

            # 标记已保存用户消息，后续前脑阶段无需重复写库
            context["_message_saved"] = True
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session_history = session.setdefault("history", [])

            if action in {"stop", "change"}:
                session_history.append({"role": "user", "content": message_content})
                if reply:
                    reply = self._append_busy_queue_summary(chat_id, reply)
                    msg_id = await self.adapter.send_message(chat_id, reply)
                    md = {"source": "interrupt"}
                    if msg_id:
                        md["platform_message_ids"] = [str(msg_id)]
                    self.message_history.add_message(
                        platform="telegram",
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                await self._interrupt_backend(chat_id, reason=action, user_text=text, skip_reply=True)
                return
            elif action == "list_queue":
                session_history.append({"role": "user", "content": message_content})
                if reply:
                    reply = self._append_busy_queue_summary(chat_id, reply)
                    msg_id = await self.adapter.send_message(chat_id, reply)
                    md = {"source": "queue_status"}
                    if msg_id:
                        md["platform_message_ids"] = [str(msg_id)]
                    self.message_history.add_message(
                        platform="telegram",
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                return
            elif action == "cancel_queue":
                session_history.append({"role": "user", "content": message_content})
                queue = self.task_queues.get(chat_id)
                param = decision.get("param")
                if queue and param:
                    removed_text = await queue.remove_by_index(param)
                    if removed_text:
                        logger.info(f"[{chat_id}] 已取消排队任务 #{param}: '{removed_text}'")
                    else:
                        logger.warning(f"[{chat_id}] 取消排队任务 #{param} 失败（序号无效或队列已空）")
                if reply:
                    reply = self._append_busy_queue_summary(chat_id, reply)
                    msg_id = await self.adapter.send_message(chat_id, reply)
                    md = {"source": "queue_cancel"}
                    if msg_id:
                        md["platform_message_ids"] = [str(msg_id)]
                    self.message_history.add_message(
                        platform="telegram",
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                return
            elif action == "clear_queue":
                session_history.append({"role": "user", "content": message_content})
                queue = self.task_queues.get(chat_id)
                if queue:
                    await queue.clear()
                    logger.info(f"[{chat_id}] 已清空所有排队任务")
                if reply:
                    reply = self._append_busy_queue_summary(chat_id, reply)
                    msg_id = await self.adapter.send_message(chat_id, reply)
                    md = {"source": "queue_clear"}
                    if msg_id:
                        md["platform_message_ids"] = [str(msg_id)]
                    self.message_history.add_message(
                        platform="telegram",
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                return
            else:
                # action == "queue"：不发忙碌提示，让前脑继续。
                pass

        # --- 正常流程：后端空闲 ---
        # 仅在确保非忙碌的情况下，才执行旧的抢占逻辑，清理残留的“假死”任务
        if not (status and status.busy):
            if chat_id in self.generation_tasks:
                logger.info(f"[{chat_id}] 抢占：清理可能残留的生成任务。")
                self.generation_tasks[chat_id].cancel()
                await asyncio.sleep(0.1)

        # 图片消息直接走后脑（需要 image 模型 + 工具能力）
        if image_input_detected:
            if not multimodal_images:
                warn = "没有读取到图片内容，可能文件路径或下载失败了，能再发一次原图吗？"
                await self.adapter.send_message(chat_id, warn)
                logger.warning(f"[{chat_id}] 检测到图片标记但未能加载图片，已提示用户重发。原始文本: {text[:100]}")
                return
            logger.info(f"[{chat_id}] 检测到图片输入，直接启动后脑。")
            # 将解析后的干净文本放入 context，避免后脑重复清洗
            context = dict(context)
            context["text"] = text
            if multimodal_images:
                context["multimodal_images"] = multimodal_images
            task = asyncio.create_task(self._generate_response(context))
            self.generation_tasks[chat_id] = task
            return

        # --- 前脑优先路由 (Front-Brain-First) ---
        # 1) 保存用户消息到数据库（保证前脑能读到历史）
        storage_id = chat_id if chat_type != "private" else user_id
        message_content = f"{user_name}: {text}" if chat_type != "private" else text
        if not context.get("_message_saved"):
            user_metadata = {}
            platform_msg_id = context.get("platform_message_id")
            if platform_msg_id:
                user_metadata["platform_message_ids"] = [str(platform_msg_id)]

            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="user",
                content=message_content,
                user_id=user_id,
                metadata=user_metadata or None,
            )
            context["_message_saved"] = True

        # 2) 前脑生成即时回复 + 路由判断
        front_result = await self._generate_front_chat_response(context)

        # 3) 发送前脑回复给用户（如果有的话）
        user_reply = front_result.get("user_reply", "")
        should_reply = front_result.get("should_reply", True)
        send_front_reply = user_reply and should_reply
        status = self.worker_status.get(chat_id)
        queue = self.task_queues.get(chat_id)
        backend_busy_or_queued = bool((status and status.busy) or (queue and queue.size() > 0))
        if backend_busy_or_queued and front_result.get("needs_backend"):
            # 避免后脑忙碌时重复回复：保留前脑回复供后脑参考，但不再发送给用户
            send_front_reply = False

        # 去除了：后脑忙碌时，只要要给用户发消息，就附带当前队列详情（避免聊天时泄漏队列信息）

        if send_front_reply:
            # 使用 [SPLIT] 分段发送
            sent_message_ids = await self._send_split_message(chat_id, user_reply)

            # 保存前脑回复到数据库，记录来源与平台 message_id
            metadata = {"source": "front"}
            if sent_message_ids:
                metadata["platform_message_ids"] = sent_message_ids
            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="assistant",
                content=user_reply,
                user_id="assistant",
                metadata=metadata,
            )

            # 同步到 session history
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session_history = session.setdefault("history", [])
            session_history.append({"role": "user", "content": message_content})
            session_history.append({"role": "assistant", "content": user_reply})
            if len(session_history) > 20:
                session["history"] = session_history[-20:]

        # 4) 根据路由信号决定是否启动后脑
        if front_result.get("force_semi_online"):
            logger.info(f"[{chat_id}] 前脑触发 [SEMI_ONLINE]，立即进入半在线状态。")
            self._transition_to_semi_online(chat_id)
            return

        followup_delay = self.FOLLOWUP_NEED_FOLLOW_DELAY if front_result.get("need_follow") else None
        if front_result["needs_backend"]:
            # 标记上下文：后脑应跳过用户消息保存（前脑已保存）
            backend_context = context.copy()
            backend_context["_front_brain_handled"] = True
            backend_context["_front_brain_reply"] = user_reply if should_reply else ""
            backend_context["_message_saved"] = True
            backend_context["task_instruction"] = front_result.get("task_instruction", "")
            backend_context["use_image_model"] = front_result.get("use_image_model", context.get("use_image_model", False))
            if followup_delay is not None:
                backend_context["_followup_initial_delay"] = followup_delay

            status = self.worker_status.get(chat_id)
            queue = self.task_queues.get(chat_id)
            backend_busy_or_queued = bool((status and status.busy) or (queue and queue.size() > 0))
            if backend_busy_or_queued:
                logger.info(f"[{chat_id}] 前脑判定需要后脑，但后脑忙碌，进入二次确认入队流程")
                await self._prepare_backend_enqueue_confirmation(
                    chat_id=chat_id,
                    storage_id=storage_id,
                    user_message_content=message_content,
                    backend_context=backend_context,
                    pending_request_text=text,
                )
                return

            logger.info(f"[{chat_id}] 前脑判定需要后脑，启动轮询循环...")
            task = asyncio.create_task(self._run_polling_loop(backend_context))
            self.generation_tasks[chat_id] = task
        else:
            logger.info(f"[{chat_id}] 前脑判定纯聊天，无需后脑。")
            # 纯聊天完成后，也需要标记空闲并启动 followup 计时
            self._mark_scheduler_idle(chat_id, initial_delay=followup_delay)

    async def _prepare_backend_enqueue_confirmation(
        self,
        chat_id: str,
        storage_id: str,
        user_message_content: str,
        backend_context: Dict[str, Any],
        pending_request_text: str,
    ) -> None:
        """后脑忙碌时，先缓存待入队任务并向用户发起二次确认。"""
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})

        queue = self.task_queues.get(chat_id)
        queue_summary = queue.get_queue_summary() if queue else ""
        status = self.worker_status.get(chat_id)
        progress_summary = status.get_summary() if status else ""

        session["pending_backend_enqueue"] = {
            "backend_context": backend_context,
            "pending_request_text": pending_request_text,
            "queue_summary": queue_summary,
            "created_at": time.time(),
        }

        confirm_reply = await self._generate_enqueue_confirmation_reply(
            chat_id=chat_id,
            pending_request_text=pending_request_text,
            queue_summary=queue_summary,
            progress_summary=progress_summary,
        )

        if confirm_reply:
            sent_ids = await self._send_split_message(chat_id, confirm_reply)
            metadata = {"source": "front", "stage": "queue_confirm"}
            if sent_ids:
                metadata["platform_message_ids"] = sent_ids

            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="assistant",
                content=confirm_reply,
                user_id="assistant",
                metadata=metadata,
            )

            session_history = session.setdefault("history", [])
            session_history.append({"role": "user", "content": user_message_content})
            session_history.append({"role": "assistant", "content": confirm_reply})
            if len(session_history) > 20:
                session["history"] = session_history[-20:]

    async def _try_handle_pending_backend_enqueue(
        self,
        context: Dict[str, Any],
        text: str,
        chat_type: str,
        user_id: str,
        user_name: str,
    ) -> bool:
        """处理“待确认入队”对话；已处理返回 True。"""
        chat_id = context["chat_id"]
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        pending = session.get("pending_backend_enqueue")
        if not pending:
            return False

        decision, reply = await self._detect_enqueue_confirmation_decision(
            chat_id=chat_id,
            user_text=text,
            pending_request_text=str(pending.get("pending_request_text", "")),
            queue_summary=str(pending.get("queue_summary", "")),
        )

        if decision == "ignore":
            session.pop("pending_backend_enqueue", None)
            logger.info(f"[{chat_id}] 待确认入队取消：用户开始了新话题或拒绝（判断为 ignore）")
            return False

        decision, reply = await self._detect_enqueue_confirmation_decision(
            chat_id=chat_id,
            user_text=text,
            pending_request_text=str(pending.get("pending_request_text", "")),
            queue_summary=str(pending.get("queue_summary", "")),
        )

        if decision == "ignore":
            session.pop("pending_backend_enqueue", None)
            logger.info(f"[{chat_id}] 待确认入队取消：用户开始了新话题或拒绝（判断为 ignore）")
            return False

        storage_id = chat_id if chat_type != "private" else user_id
        message_content = f"{user_name}: {text}" if chat_type != "private" else text

        user_metadata = {}
        platform_msg_id = context.get("platform_message_id")
        if platform_msg_id:
            user_metadata["platform_message_ids"] = [str(platform_msg_id)]

        self.message_history.add_message(
            platform="telegram",
            chat_id=storage_id,
            role="user",
            content=message_content,
            user_id=user_id,
            metadata=user_metadata or None,
        )
        context["_message_saved"] = True

        session_history = session.setdefault("history", [])
        session_history.append({"role": "user", "content": message_content})

        if decision == "confirm":
            backend_context = pending.get("backend_context") or {}
            status = self.worker_status.get(chat_id)
            if status and status.busy:
                queue = self.task_queues.setdefault(chat_id, BackendTaskQueue())
                await queue.enqueue({
                    "context": backend_context,
                    "text": backend_context.get("text", ""),
                })
                logger.info(f"[{chat_id}] 待确认任务已入队 (队列长度 {queue.size()})")
            else:
                task = asyncio.create_task(self._run_polling_loop(backend_context))
                self.generation_tasks[chat_id] = task
                logger.info(f"[{chat_id}] 待确认任务已直接启动（后脑空闲）")

            session.pop("pending_backend_enqueue", None)
        elif decision == "cancel":
            session.pop("pending_backend_enqueue", None)
            logger.info(f"[{chat_id}] 用户取消待确认入队任务")
        else:
            logger.info(f"[{chat_id}] 待确认入队：用户回复不明确，继续等待确认")

        if reply:
            sent_ids = await self._send_split_message(chat_id, reply)
            metadata = {"source": "front", "stage": "queue_confirm"}
            if sent_ids:
                metadata["platform_message_ids"] = sent_ids

            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="assistant",
                content=reply,
                user_id="assistant",
                metadata=metadata,
            )
            session_history.append({"role": "assistant", "content": reply})

        if len(session_history) > 20:
            session["history"] = session_history[-20:]

        return True

    async def _generate_enqueue_confirmation_reply(
        self,
        chat_id: str,
        pending_request_text: str,
        queue_summary: str,
        progress_summary: str,
    ) -> str:
        """让前脑生成“是否入队”的二次确认消息。"""
        soul = get_soul_prompt()
        identity_context = load_identity_context(include_schedule=False)
        system_prompt = render_template(
            "queue_confirmation.jinja",
            "ask_system",
            soul=soul,
            identity_context=identity_context,
        )
        user_prompt = render_template(
            "queue_confirmation.jinja",
            "ask_user",
            pending_request_text=pending_request_text,
            queue_summary=queue_summary,
            progress_summary=progress_summary,
        )

        response = ""
        try:
            stream = self._chat_stream_wrapper(
                self.fast_llm,
                chat_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=None,
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += str(chunk.get("content", ""))
                elif isinstance(chunk, str):
                    response += chunk
        except Exception:
            logger.warning(f"[{chat_id}] 生成入队确认消息失败，使用模板回退", exc_info=True)
            response = render_template(
                "queue_confirmation.jinja",
                "ask_fallback",
                pending_request_text=pending_request_text,
                queue_summary=queue_summary,
            )

        return self._strip_timestamp_markers(response.strip())

    async def _detect_enqueue_confirmation_decision(
        self,
        chat_id: str,
        user_text: str,
        pending_request_text: str,
        queue_summary: str,
    ) -> tuple[str, str]:
        """识别用户对“是否入队”的确认意图。"""
        soul = get_soul_prompt()
        identity_context = load_identity_context(include_schedule=False)
        system_prompt = render_template(
            "queue_confirmation.jinja",
            "detect_system",
            soul=soul,
            identity_context=identity_context,
        )
        user_prompt = render_template(
            "queue_confirmation.jinja",
            "detect_user",
            user_text=user_text,
            pending_request_text=pending_request_text,
            queue_summary=queue_summary,
        )

        response = ""
        try:
            stream = self._chat_stream_wrapper(
                self.fast_llm,
                chat_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=None,
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += str(chunk.get("content", ""))
                elif isinstance(chunk, str):
                    response += chunk
        except Exception:
            logger.warning(f"[{chat_id}] 入队确认意图检测失败，默认 unclear", exc_info=True)
            return "unclear", render_template("queue_confirmation.jinja", "unclear_fallback")

        response = response.strip()
        decision_match = self._CONFIRM_DECISION_PATTERN.search(response)
        decision = decision_match.group(1).lower() if decision_match else "unclear"

        reply_match = self._CONFIRM_REPLY_PATTERN.search(response)
        reply = reply_match.group(1).strip() if reply_match else ""
        reply = self._strip_timestamp_markers(reply)

        if not reply:
            if decision == "confirm":
                reply = render_template("queue_confirmation.jinja", "confirm_fallback")
            elif decision == "cancel":
                reply = render_template("queue_confirmation.jinja", "cancel_fallback")
            else:
                reply = render_template("queue_confirmation.jinja", "unclear_fallback")

        return decision, reply

    async def _send_split_message(self, chat_id: str, text: str) -> list[str]:
        """按 [SPLIT] 或 [SPLIT:delay] 规则发送消息，返回平台消息 ID 列表。"""
        sent_message_ids: list[str] = []
        # _SPLIT_MARKER_PATTERN 现在返回 [text_part, delay_str, text_part, ...]
        parts = self._SPLIT_MARKER_PATTERN.split(text)
        
        for i in range(0, len(parts), 2):
            part = parts[i].strip()
            if part:
                msg_id = await self.adapter.send_message(chat_id, part)
                if msg_id:
                    sent_message_ids.append(str(msg_id))
            
            # 如果后面还有 delay，则执行停顿（最后一个 part 后面没有 delay 所以用 i+1 检查）
            if i + 1 < len(parts) and parts[i+1] is not None:
                try:
                    delay_val = float(parts[i+1])
                    if delay_val > 0:
                        await asyncio.sleep(delay_val)
                except ValueError:
                    pass
        return sent_message_ids

    # ------------------------------------------------------------------
    # 斜杠命令处理子方法
    # ------------------------------------------------------------------

    async def _cmd_start(self, chat_id: str, chat_type: str, user_id: str):
        if chat_id in self.generation_tasks:
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)
        self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
        storage_id = chat_id if chat_type != "private" else user_id
        self.message_history.clear_chat_history("telegram", storage_id, keep_pinned=True)
        status = self.worker_status.get(chat_id)
        if status:
            status.finish()
        queue = self.task_queues.get(chat_id)
        if queue:
            await queue.clear()
        await self.adapter.send_message(chat_id, "N.O.R.A. Core 已启动。")
        logger.info(f"[{chat_id}] Session已重置 by /start command.")

    async def _cmd_reload_models(self, chat_id: str):
        try:
            self._reload_llm_clients()
            await self.adapter.send_message(chat_id, "✅ 模型配置已重新加载。")
            logger.info(f"[{chat_id}] 模型配置已通过 /reload_models 刷新。")
        except Exception as e:
            logger.error(f"[{chat_id}] /reload_models 执行失败: {e}", exc_info=True)
            await self.adapter.send_message(chat_id, f"❌ 模型刷新失败: {e}")

    async def _cmd_clear(self, chat_id: str, chat_type: str, user_id: str):
        try:
            if chat_id in self.generation_tasks:
                self.generation_tasks[chat_id].cancel()
                await asyncio.sleep(0.1)
            self.sessions.pop(chat_id, None)
            queue = self.task_queues.get(chat_id)
            if queue:
                await queue.clear()
            status = self.worker_status.get(chat_id)
            if status:
                status.finish()
            storage_id = chat_id if chat_type != "private" else user_id
            self.message_history.clear_chat_history("telegram", storage_id, keep_pinned=False)
            await self.adapter.send_message(chat_id, "✅ 当前聊天的历史记录已清空。")
            logger.info(f"[{chat_id}] 聊天历史已通过 /clear 命令清空。")
        except Exception as e:
            logger.error(f"[{chat_id}] 清空历史记录时出错: {e}")
            await self.adapter.send_message(chat_id, "❌ 清空历史记录时发生错误。")

    async def _cmd_stop(self, chat_id: str):
        try:
            if chat_id in self.generation_tasks:
                self.generation_tasks[chat_id].cancel()
                await asyncio.sleep(0.1)
            self.sessions.pop(chat_id, None)
            queue = self.task_queues.get(chat_id)
            if queue:
                await queue.clear()
            status = self.worker_status.get(chat_id)
            if status:
                status.finish()
            self._cancel_followup_timer(chat_id)
            # 标记被前端打断，避免 finally 再次调度排队任务
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session["_interrupted_by_frontend"] = True
            reply = "✅ 已停止当前所有任务。"
            await self.adapter.send_message(chat_id, reply)
            self.message_history.add_message(
                platform="telegram",
                chat_id=chat_id,
                role="assistant",
                content=reply,
                user_id="assistant",
            )
            logger.info(f"[{chat_id}] 已通过 /stop 命令停止所有任务。")
        except Exception as e:
            logger.error(f"[{chat_id}] 停止任务时出错: {e}")
            await self.adapter.send_message(chat_id, "❌ 停止任务时发生错误。")

    async def _cmd_regenerate_proactive(self, chat_id: str, text: str):
        if not self.scheduler:
            await self.adapter.send_message(chat_id, "❌ Scheduler 未启用，无法重新生成主动消息计划。")
            return

        mode = "replace"
        parts = text.strip().split()
        if len(parts) >= 2 and parts[1].lower() in ("append", "replace"):
            mode = parts[1].lower()
        clear_existing = (mode != "append")

        try:
            result = await self.scheduler.regenerate_today_plan(clear_existing=clear_existing)
            if result.get("success"):
                msg = (
                    f"✅ 主动消息计划已{'覆盖重建' if clear_existing else '追加生成'}\n"
                    f"日期: {result.get('date', '-')}\n"
                    f"本次新增: {result.get('count', 0)}\n"
                    f"今日总未触发: {result.get('total_today', 0)}"
                )
            else:
                msg = f"❌ 重新生成失败: {result.get('message', 'unknown error')}"
            await self.adapter.send_message(chat_id, msg)
        except Exception as e:
            logger.error(f"[{chat_id}] /regenerate_proactive 执行失败: {e}", exc_info=True)
            await self.adapter.send_message(chat_id, f"❌ 重新生成失败: {e}")

    async def _cmd_schedule_today(self, chat_id: str):
        if not self.scheduler:
            await self.adapter.send_message(chat_id, "❌ Scheduler 未启用，无法查看今日计划。")
            return
        try:
            today_plan = self.scheduler.get_today_plan()
            if not today_plan:
                await self.adapter.send_message(chat_id, "📭 今日没有未触发的主动消息计划。")
                return
            sorted_plan = sorted(today_plan, key=lambda x: x.get("trigger_time", ""))
            lines = ["🗓️ 今日主动消息计划（未触发）:"]
            for i, item in enumerate(sorted_plan, start=1):
                trigger_time = item.get("trigger_time", "")
                hhmm = trigger_time[11:16] if len(trigger_time) >= 16 else trigger_time
                reason = item.get("reason", "") or "(无缘由)"
                lines.append(f"{i}. {hhmm} - {reason}")
            lines.append(f"\n共 {len(sorted_plan)} 条")
            await self.adapter.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"[{chat_id}] /schedule_today 执行失败: {e}", exc_info=True)
            await self.adapter.send_message(chat_id, f"❌ 获取今日计划失败: {e}")

    async def _cmd_status(self, chat_id: str, chat_type: str, user_id: str):
        try:
            lines = ["📊 **N.O.R.A. Core 状态报告**\n"]
            
            # 后脑状态
            status = self.worker_status.get(chat_id)
            if status and status.busy:
                elapsed = int(time.time() - status.started_at)
                lines.append("🧠 后脑 (Back Brain): 🔴 忙碌")
                lines.append(f"  任务: \"{status.original_query[:80]}\"")
                lines.append(f"  阶段: {status.phase}")
                if status.detail:
                    lines.append(f"  详情: {status.detail}")
                lines.append(f"  进度: 第 {status.current_turn}/{status.max_turns} 轮")
                lines.append(f"  耗时: {elapsed}s")
                if status.tool_history:
                    recent = status.tool_history[-3:]
                    lines.append("  最近工具:")
                    for step in recent:
                        lines.append(f"    • {step[:80]}")
            else:
                lines.append("🧠 后脑 (Back Brain): 🟢 空闲")
            
            # 前脑信息
            lines.append("")
            lines.append("⚡ 前脑 (Fast Brain): 🟢 就绪")

            # 全局在线状态
            lines.append("")
            ai_state = get_ai_state_summary()
            lines.append("🌐 在线状态:")
            lines.append(f"  presence: {ai_state['presence']}")
            lines.append(f"  generating: {ai_state['is_generating']}")
            lines.append(f"  backend_busy: {ai_state['is_backend_busy']}")
            lines.append(f"  skip_proactive: {ai_state['should_skip_proactive']}")
            
            # 模型信息
            lines.append("")
            lines.append("🤖 模型配置:")
            try:
                smart_model = config.get_model_name("smart")
                fast_model = config.get_model_name("fast")
                image_model = config.get_model_name("image")
                lines.append(f"  smart: {smart_model}")
                lines.append(f"  fast: {fast_model}")
                lines.append(f"  image: {image_model}")
            except Exception:
                lines.append("  (无法获取模型信息)")
            
            # Scheduler 状态
            lines.append("")
            if self.scheduler:
                lines.append(f"📅 Scheduler: {'🟢 运行中' if self.scheduler._scheduler.running else '🔴 停止'}")
                lines.append(f"  AI 状态: {ai_state['presence']}")
                today_plan = self.scheduler.get_today_plan()
                active_alarms = self.scheduler.list_alarms()
                lines.append(f"  今日待触发: {len(today_plan)} 条")
                lines.append(f"  活跃闹钟: {len(active_alarms)} 个")
            else:
                lines.append("📅 Scheduler: 🔴 未启用")
            
            # 排队消息
            queue = self.task_queues.get(chat_id)
            if queue and queue.size() > 0:
                lines.append(f"\n{queue.get_queue_summary()}")
            
            # RAG / 消息历史
            lines.append("")
            lines.append(f"🗄️ RAG: {'🟢 启用' if self.rag.enabled else '🔴 离线'}")
            lines.append(f"💰 成本跟踪: {'🟢 启用' if self.cost_tracking_enabled else '🔴 禁用'}")
            
            # 对话分段统计
            try:
                _stat_storage_id = chat_id if chat_type != "private" else user_id
                stats = self.message_history.get_statistics("telegram", _stat_storage_id)
                lines.append("")
                lines.append(f"📋 对话段落: {stats.get('total_sessions', 0)} 个已封闭")
                active_msgs = stats.get('active_messages', 0)
                if active_msgs > 0:
                    lines.append(f"  当前活跃对话: {active_msgs} 条消息")
            except Exception:
                pass
            
            await self.adapter.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"[{chat_id}] /status 执行失败: {e}", exc_info=True)
            await self.adapter.send_message(chat_id, f"❌ 获取状态失败: {e}")

    async def _cmd_debug(self, chat_id: str):
        current = self.debug_mode.get(chat_id, False)
        self.debug_mode[chat_id] = not current
        state_str = "🟢 已开启" if self.debug_mode[chat_id] else "🔴 已关闭"
        await self.adapter.send_message(chat_id, f"🐛 Debug 模式: {state_str}\n开启后每次工具调用、技能执行、思考阶段都会实时推送详情。")
        logger.info(f"[{chat_id}] Debug 模式切换为: {self.debug_mode[chat_id]}")

    async def _cmd_custom_scope(self, chat_id: str, text: str):
        """设置 CUSTOM.md 注入范围。"""
        parts = text.strip().split()
        args = [p.strip().lower() for p in parts[1:]]
        try:
            cfg = config.get_config() or {}
            current_scopes = config.get_custom_injection_scopes()
            if not args:
                if not current_scopes:
                    scope_desc = "all"
                elif any(s in ("none", "off") for s in current_scopes):
                    scope_desc = "none"
                else:
                    scope_desc = ", ".join(current_scopes)
                await self.adapter.send_message(
                    chat_id,
                    "🧭 CUSTOM 注入范围\n"
                    f"当前: {scope_desc}\n"
                    "用法: /custom_scope（按钮多选）或 /custom_scope fast smart | image | coder | all | none",
                )
                return

            if "all" in args:
                scopes_to_set = []
            elif any(a in ("none", "off") for a in args):
                scopes_to_set = ["none"]
            else:
                allowed = {"fast", "smart", "image", "coder"}
                invalid = [a for a in args if a not in allowed]
                if invalid:
                    await self.adapter.send_message(
                        chat_id,
                        "❌ 无效范围: " + ", ".join(invalid) + "。可选: fast, smart, image, coder, all, none",
                    )
                    return
                scopes_to_set = list(dict.fromkeys(args))

            cfg.setdefault("custom_injection", {})["scopes"] = scopes_to_set
            config.save_config(cfg)

            if not scopes_to_set:
                scope_desc = "all"
            elif any(s in ("none", "off") for s in scopes_to_set):
                scope_desc = "none"
            else:
                scope_desc = ", ".join(scopes_to_set)

            await self.adapter.send_message(chat_id, f"✅ CUSTOM 注入范围已更新为: {scope_desc}")
        except Exception as e:
            logger.error(f"[{chat_id}] /custom_scope 执行失败: {e}", exc_info=True)
            await self.adapter.send_message(chat_id, f"❌ 设置失败: {e}")

    async def _cmd_undo(self, chat_id: str, chat_type: str, user_id: str):
        """撤销上一条已发送的 assistant 消息。"""
        storage_id = chat_id if chat_type != "private" else user_id
        try:
            removed = self.message_history.delete_last_assistant_message(
                platform="telegram",
                chat_id=storage_id,
                source=None,
            )
            if removed is None:
                await self.adapter.send_message(chat_id, "❌ 没有可撤销的消息。")
                return

            # 内存会话历史同步删除最近一条 assistant（不连带用户）
            session = self.sessions.get(chat_id)
            if session and session.get("history"):
                hist = session["history"]
                for idx in range(len(hist) - 1, -1, -1):
                    if hist[idx].get("role") == "assistant":
                        hist.pop(idx)
                        break

            # 尝试删除平台消息
            md = removed.get("metadata") or {}
            platform_ids = md.get("platform_message_ids") or []
            delete_ok = False
            delete_attempted = False
            if platform_ids and getattr(self.adapter, "platform_features", None):
                feats = self.adapter.platform_features
                if getattr(feats, "supports_delete_message", False):
                    delete_attempted = True
                    for pid in platform_ids:
                        try:
                            ok = await self.adapter.delete_message(chat_id, pid)
                            delete_ok = delete_ok or ok
                        except Exception:
                            logger.warning(f"[{chat_id}] 平台删除消息 {pid} 失败", exc_info=True)

            if delete_attempted:
                if delete_ok:
                    await self.adapter.send_message(chat_id, "✅ 已撤销上一条 AI 消息（聊天界面已删除）。")
                else:
                    await self.adapter.send_message(chat_id, "✅ 已撤销存档；⚠️ 平台消息删除失败或权限不足。")
            else:
                await self.adapter.send_message(chat_id, "✅ 已撤销上一条 AI 消息（仅存档层面）。")
        except Exception as e:
            logger.error(f"[{chat_id}] /undo 执行失败: {e}", exc_info=True)
            await self.adapter.send_message(chat_id, f"❌ 撤销失败: {e}")

    async def _cmd_set_stream(self, chat_id: str, chat_type: str, user_id: str, text: str):
        """切换 per-chat 非流式输出模式。/set_stream on|off（on=一次性输出，off=流式），空参=查询当前状态。"""
        parts = text.strip().split()
        arg = parts[1].lower() if len(parts) >= 2 else ""
        current = self.non_stream_flags.get(chat_id, self.default_non_stream)

        if arg in ("on", "off"):
            new_val = arg == "on"
            self.non_stream_flags[chat_id] = new_val
            msg = "✅ 已启用一次性输出（非流式）" if new_val else "✅ 已恢复流式输出"
        else:
            msg = "ℹ️ 当前输出模式: " + ("一次性输出（非流式）" if current else "流式输出")

        await self.adapter.send_message(chat_id, msg)
        storage_id = chat_id if chat_type != "private" else user_id
        self.message_history.add_message(
            platform="telegram",
            chat_id=storage_id,
            role="assistant",
            content=msg,
            user_id="assistant",
        )
