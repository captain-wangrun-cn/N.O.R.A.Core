'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:28:21
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""消息处理 Mixin — handle_new_message + 命令路由。"""

import asyncio
import logging
import time
from typing import Dict, Any

from brain.multimodal import extract_image_payloads
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

    async def handle_new_message(self, context: Dict[str, Any]):
        """处理来自适配器的新消息/命令。"""
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
        if self.scheduler:
            # 自动设置 default_chat_id（首次消息时）
            if not self.scheduler.default_chat_id:
                storage_id_for_scheduler = chat_id if chat_type != "private" else user_id
                self.scheduler.default_chat_id = storage_id_for_scheduler
                logger.info(f"Scheduler default_chat_id 已设置: {storage_id_for_scheduler}")
        
        # ── 命令分发 ──
        stripped = text.strip()

        if stripped.startswith("/start"):
            await self._cmd_start(chat_id, chat_type, user_id)
            return

        if stripped.startswith("/reload_models"):
            await self._cmd_reload_models(chat_id)
            return

        if stripped.startswith("/clear"):
            await self._cmd_clear(chat_id, chat_type, user_id)
            return

        if stripped.startswith("/stop"):
            await self._cmd_stop(chat_id)
            return

        if stripped.startswith("/regenerate_proactive"):
            await self._cmd_regenerate_proactive(chat_id, stripped)
            return

        if stripped.startswith("/schedule_today"):
            await self._cmd_schedule_today(chat_id)
            return

        if stripped.startswith("/status"):
            await self._cmd_status(chat_id, chat_type, user_id)
            return

        if stripped.startswith("/debug"):
            await self._cmd_debug(chat_id)
            return

        if stripped.startswith("/custom_scope"):
            await self._cmd_custom_scope(chat_id, stripped)
            return

        if stripped.startswith("/undo"):
            await self._cmd_undo(chat_id, chat_type, user_id)
            return

        if stripped.startswith("/set_stream") or stripped.startswith("/nonstream"):
            await self._cmd_set_stream(chat_id, chat_type, user_id, stripped)
            return

        # --- 前后端分离逻辑 ---
        status = self.worker_status.get(chat_id)
        if status and status.busy:
            # 后端忙碌：用 fast 模型判断用户意图并生成回复
            logger.info(f"[{chat_id}] 后端忙碌，分析用户意图...")
            
            # 保存用户消息到数据库（不丢消息）
            storage_id = chat_id if chat_type != "private" else user_id
            message_content = f"{user_name}: {text}" if chat_type != "private" else text
            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="user",
                content=message_content,
                user_id=user_id
            )
            
            # LLM 判断意图并生成回复
            decision = await self._detect_interrupt_intent_and_reply(chat_id, text, status)
            action = decision["action"]
            reply = decision["reply"]
            
            # 发送回复
            await self.adapter.send_message(chat_id, reply)
            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="assistant",
                content=reply,
                user_id="assistant"
            )
            
            # 同步忙碌期间的对话到 session history（让后脑也能感知这段交互）
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session_history = session.setdefault("history", [])
            session_history.append({"role": "user", "content": message_content})
            session_history.append({"role": "assistant", "content": reply})
            # 保持 session history 不过度膨胀
            if len(session_history) > 20:
                session["history"] = session_history[-20:]
            
            # 根据 action 执行操作
            if action == "stop":
                await self._interrupt_backend(chat_id, reason="stop", user_text=text, skip_reply=True)
                return
            elif action == "change":
                await self._interrupt_backend(chat_id, reason="change", user_text=text, skip_reply=True)
                return
            elif action == "list_queue":
                # 查看队列：AI 已在 reply 中汇报了状态，无需额外操作
                return
            elif action == "cancel_queue":
                # 取消特定排队任务
                queue = self.task_queues.get(chat_id)
                param = decision.get("param")
                if queue and param:
                    removed_text = await queue.remove_by_index(param)
                    if removed_text:
                        logger.info(f"[{chat_id}] 已取消排队任务 #{param}: '{removed_text}'")
                    else:
                        logger.warning(f"[{chat_id}] 取消排队任务 #{param} 失败（序号无效或队列已空）")
                return
            elif action == "clear_queue":
                # 清空所有排队任务
                queue = self.task_queues.get(chat_id)
                if queue:
                    await queue.clear()
                    logger.info(f"[{chat_id}] 已清空所有排队任务")
                return
            else:  # action == "queue"
                # 新需求：忙碌时不再入待生成队列，避免重复回复。
                logger.info(f"[{chat_id}] 后端忙碌，按照策略不入队，已仅回复忙碌提示。")
                return

        # --- 正常流程：后端空闲 ---
        # 旧的抢占逻辑保留，以防万一有残留任务
        if chat_id in self.generation_tasks:
            logger.info(f"[{chat_id}] 抢占：取消正在进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        # 图片消息直接走后脑（需要 image 模型 + 工具能力）
        if image_input_detected:
            logger.info(f"[{chat_id}] 检测到图片输入，直接启动后脑。")
            task = asyncio.create_task(self._generate_response(context))
            self.generation_tasks[chat_id] = task
            return

        # --- 前脑优先路由 (Front-Brain-First) ---
        # 1) 保存用户消息到数据库（保证前脑能读到历史）
        storage_id = chat_id if chat_type != "private" else user_id
        message_content = f"{user_name}: {text}" if chat_type != "private" else text
        self.message_history.add_message(
            platform="telegram",
            chat_id=storage_id,
            role="user",
            content=message_content,
            user_id=user_id,
        )
        context["_message_saved"] = True

        # 2) 前脑生成即时回复 + 路由判断
        front_result = await self._generate_front_chat_response(context)

        # 3) 发送前脑回复给用户（如果有的话）
        user_reply = front_result.get("user_reply", "")
        should_reply = front_result.get("should_reply", True)
        if user_reply and should_reply:
            # 使用 [SPLIT] 分段发送
            parts = self._SPLIT_MARKER_PATTERN.split(user_reply)
            sent_message_ids = []
            for part in parts:
                part = part.strip()
                if part:
                    msg_id = await self.adapter.send_message(chat_id, part)
                    if msg_id:
                        sent_message_ids.append(str(msg_id))

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
        if front_result["needs_backend"]:
            logger.info(f"[{chat_id}] 前脑判定需要后脑，启动轮询循环...")
            # 标记上下文：后脑应跳过用户消息保存（前脑已保存）
            backend_context = context.copy()
            backend_context["_front_brain_handled"] = True
            backend_context["_front_brain_reply"] = user_reply if should_reply else ""
            backend_context["_message_saved"] = True
            backend_context["task_instruction"] = front_result.get("task_instruction", "")
            task = asyncio.create_task(self._run_polling_loop(backend_context))
            self.generation_tasks[chat_id] = task
        else:
            logger.info(f"[{chat_id}] 前脑判定纯聊天，无需后脑。")
            # 纯聊天完成后，也需要标记空闲并启动 followup 计时
            self._mark_scheduler_idle(chat_id)

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
            await self.adapter.send_message(chat_id, "✅ 已停止当前所有任务。")
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
            """撤销前脑上一条已发送的消息（仅 assistant / front 来源）。"""
            storage_id = chat_id if chat_type != "private" else user_id
            try:
                removed = self.message_history.delete_last_assistant_message(
                    platform="telegram",
                    chat_id=storage_id,
                    source="front",
                )
                if removed is None:
                    await self.adapter.send_message(chat_id, "❌ 没有可撤销的前脑消息。")
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
                        await self.adapter.send_message(chat_id, "✅ 已撤销上一条前脑消息（聊天界面已删除）。")
                    else:
                        await self.adapter.send_message(chat_id, "✅ 已撤销存档；⚠️ 平台消息删除失败或权限不足。")
                else:
                    await self.adapter.send_message(chat_id, "✅ 已撤销上一条前脑消息（仅存档层面）。")
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
