'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:37:36
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""调度器 Mixin — Scheduler 集成 + 对话延续 + 主动消息。"""

import asyncio
import json
import logging
from typing import Dict, Any, List

from brain.prompts import get_soul_prompt, render_template, _read_file_safe, WORKSPACE_SCHEDULE_FILE, WORKSPACE_SOUL_FILE, WORKSPACE_USER_FILE, WORKSPACE_MEMORY_FILE, _resolve_memory_file
from core.scheduler import (
    AIPresence,
    set_ai_presence,
    set_ai_generating,
    set_ai_backend_busy,
    record_ai_followup,
    get_followup_count,
    clear_conversation_tracking,
    get_ai_presence,
    get_ai_state_summary,
    get_user_idle_seconds,
)
import config

logger = logging.getLogger(__name__)


class SchedulerMixin:
    """Scheduler 集成、对话延续（followup）、主动消息（proactive）。"""

    # ------------------------------------------------------------------
    # Scheduler 启停
    # ------------------------------------------------------------------

    def start_scheduler(self, default_chat_id: str = ""):
        """启动主动消息调度器。应在 adapter 就绪后调用。"""
        if self.scheduler:
            if default_chat_id:
                self.scheduler.default_chat_id = default_chat_id
            self.scheduler.start()
            logger.info(f"Scheduler 已启动, default_chat_id={default_chat_id}")

    def stop_scheduler(self):
        """停止调度器。"""
        if self.scheduler:
            self.scheduler.stop()

    # ------------------------------------------------------------------
    # 状态同步
    # ------------------------------------------------------------------

    def _update_scheduler_state(self, chat_id: str, busy: bool):
        """同步 controller 的忙碌状态到全局 AI 状态。"""
        set_ai_backend_busy(busy)
        if busy:
            set_ai_presence(AIPresence.ONLINE)
            set_ai_generating(True)
        state = get_ai_state_summary()
        logger.info(
            f"[{chat_id}] 同步在线状态: presence={state['presence']}, "
            f"generating={state['is_generating']}, backend_busy={state['is_backend_busy']}"
        )

    def _mark_scheduler_idle(self, chat_id: str):
        """标记 AI 生成完毕。保持 ONLINE 状态并启动对话延续定时器。"""
        set_ai_generating(False)
        set_ai_backend_busy(False)
        state = get_ai_state_summary()
        logger.info(
            f"[{chat_id}] 进入空闲跟进状态: presence={state['presence']}, "
            f"generating={state['is_generating']}, backend_busy={state['is_backend_busy']}"
        )
        self._start_followup_timer(chat_id)

    # ------------------------------------------------------------------
    # 对话延续机制 (Conversation Follow-up)
    # ------------------------------------------------------------------

    def _start_followup_timer(self, chat_id: str):
        """启动对话延续定时器。如果已有定时器则先取消。"""
        self._cancel_followup_timer(chat_id)
        task = asyncio.create_task(self._followup_loop(chat_id))
        self._followup_timers[chat_id] = task
        logger.debug(f"[{chat_id}] 对话延续定时器已启动（{self.FOLLOWUP_INITIAL_DELAY}s 后首次检测）")

    def _cancel_followup_timer(self, chat_id: str):
        """取消对话延续定时器。"""
        task = self._followup_timers.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"[{chat_id}] 对话延续定时器已取消")

    async def _followup_loop(self, chat_id: str):
        """
        对话延续循环。

        流程:
        1. 等待 FOLLOWUP_INITIAL_DELAY 秒（2 分钟）
        2. 用 fast 模型检测是否应该追话
        3. 若 FOLLOWUP → 用正常模型生成追话内容并发送
        4. 若 WAIT → 等待 FOLLOWUP_RECHECK_INTERVAL 秒后再次检测
        5. 若 END 或追话次数超限 → 生成结束消息，进入 SEMI_ONLINE
        """
        try:
            await asyncio.sleep(self.FOLLOWUP_INITIAL_DELAY)

            while True:
                if get_ai_presence() != AIPresence.ONLINE:
                    logger.debug(f"[{chat_id}] AI 状态非 ONLINE，对话延续循环退出")
                    return

                status = self.worker_status.get(chat_id)
                if status and status.busy:
                    logger.debug(f"[{chat_id}] 后端忙碌，对话延续循环退出")
                    return

                idle_secs = get_user_idle_seconds(chat_id)
                count = get_followup_count(chat_id)

                if count >= self.FOLLOWUP_MAX_COUNT:
                    logger.info(f"[{chat_id}] 连续追话已达 {count} 次，引导结束对话")
                    await self._send_wrapup_message(chat_id)
                    self._transition_to_semi_online(chat_id)
                    return

                decision = await self._detect_followup_intent(chat_id, idle_secs, count)
                logger.info(f"[{chat_id}] 对话延续检测结果: {decision} (idle={idle_secs:.0f}s, count={count})")

                if decision == "FOLLOWUP":
                    await self._send_followup_message(chat_id, idle_secs)
                    record_ai_followup(chat_id)
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

                elif decision == "WAIT":
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

                elif decision == "END":
                    if count > 0:
                        await self._send_wrapup_message(chat_id)
                    self._transition_to_semi_online(chat_id)
                    return

                else:
                    logger.warning(f"[{chat_id}] 对话延续检测返回未知结果: {decision}")
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

        except asyncio.CancelledError:
            logger.debug(f"[{chat_id}] 对话延续循环被取消（用户回来了或新任务开始）")
        except Exception as e:
            logger.error(f"[{chat_id}] 对话延续循环异常: {e}", exc_info=True)
            self._transition_to_semi_online(chat_id)

    def _transition_to_semi_online(self, chat_id: str):
        """安全地从 ONLINE 过渡到 SEMI_ONLINE，同时关闭当前对话段落。"""
        clear_conversation_tracking(chat_id)
        set_ai_presence(AIPresence.SEMI_ONLINE)
        self._followup_timers.pop(chat_id, None)
        
        try:
            session_id = self.message_history.close_session(
                platform="telegram",
                chat_id=chat_id,
                trigger_type="user",
            )
            if session_id:
                logger.info(f"[{chat_id}] 对话段落 #{session_id} 已封闭")
        except Exception as e:
            logger.error(f"[{chat_id}] 关闭对话段落失败: {e}", exc_info=True)
        
        logger.info(f"[{chat_id}] 对话结束，AI 进入 SEMI_ONLINE 状态")

    async def _detect_followup_intent(self, chat_id: str, idle_secs: float, followup_count: int) -> str:
        """
        用 fast 模型判断是否应该追话。

        Returns:
            "FOLLOWUP" | "WAIT" | "END"
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz_str = config.get_message_history_config().get("timezone", "Asia/Shanghai")
        now = datetime.now(ZoneInfo(tz_str))

        storage_id = chat_id
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        recent_msgs = db_context[-10:] if db_context else []
        recent_conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
            for m in recent_msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        )

        if not recent_conversation:
            return "END"

        try:
            system_prompt = render_template('schedule.jinja', 'followup_detect_system')
            user_prompt = render_template('schedule.jinja', 'followup_detect_user',
                                          recent_conversation=recent_conversation,
                                          idle_seconds=int(idle_secs),
                                          followup_count=followup_count,
                                          current_time=now.strftime('%H:%M'))

            response = ""
            stream = self.fast_llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=[]
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += chunk["content"]
                elif isinstance(chunk, str):
                    response += chunk

            response = response.strip().upper()
            if "FOLLOWUP" in response:
                return "FOLLOWUP"
            elif "END" in response:
                return "END"
            elif "WAIT" in response:
                return "WAIT"
            else:
                logger.warning(f"[{chat_id}] fast_llm 对话延续检测返回无法解析: {response[:100]}")
                return "WAIT"

        except Exception as e:
            logger.error(f"[{chat_id}] 对话延续检测失败: {e}", exc_info=True)
            return "WAIT"

    async def _send_followup_message(self, chat_id: str, idle_secs: float):
        """用正常模型生成追话消息并发送，同时写入聊天记录。"""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz_str = config.get_message_history_config().get("timezone", "Asia/Shanghai")
        now = datetime.now(ZoneInfo(tz_str))

        soul = get_soul_prompt()

        storage_id = chat_id
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        recent_msgs = db_context[-10:] if db_context else []
        recent_conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
            for m in recent_msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        )

        try:
            system_prompt = render_template('schedule.jinja', 'followup_message_system', soul_prompt=soul)
            user_prompt = render_template('schedule.jinja', 'followup_message_user',
                                          recent_conversation=recent_conversation,
                                          idle_seconds=int(idle_secs))

            response = ""
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=[]
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += chunk["content"]
                elif isinstance(chunk, str):
                    response += chunk

            response = self._strip_thinking_content(response.strip())
            if response:
                await self.adapter.send_message(chat_id, response)
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant"
                )
                session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
                session_history = session.setdefault("history", [])
                session_history.append({"role": "assistant", "content": response})
                if len(session_history) > 20:
                    session["history"] = session_history[-20:]

                logger.info(f"[{chat_id}] 对话延续追话已发送: {response[:60]}...")
            else:
                logger.warning(f"[{chat_id}] LLM 未生成追话内容")

        except Exception as e:
            logger.error(f"[{chat_id}] 发送追话消息失败: {e}", exc_info=True)

    async def _send_wrapup_message(self, chat_id: str):
        """生成并发送结束对话的消息，同时写入聊天记录。"""
        soul = get_soul_prompt()
        idle_secs = get_user_idle_seconds(chat_id)
        count = get_followup_count(chat_id)

        storage_id = chat_id
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        recent_msgs = db_context[-10:] if db_context else []
        recent_conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
            for m in recent_msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        )

        try:
            system_prompt = render_template('schedule.jinja', 'wrapup_message_system', soul_prompt=soul)
            user_prompt = render_template('schedule.jinja', 'wrapup_message_user',
                                          recent_conversation=recent_conversation,
                                          idle_seconds=int(idle_secs) if idle_secs > 0 else 0,
                                          followup_count=count)

            response = ""
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=[]
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += chunk["content"]
                elif isinstance(chunk, str):
                    response += chunk

            response = self._strip_thinking_content(response.strip())
            if response:
                await self.adapter.send_message(chat_id, response)
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant"
                )
                session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
                session_history = session.setdefault("history", [])
                session_history.append({"role": "assistant", "content": response})
                if len(session_history) > 20:
                    session["history"] = session_history[-20:]

                logger.info(f"[{chat_id}] 对话结束消息已发送: {response[:60]}...")
            else:
                logger.warning(f"[{chat_id}] LLM 未生成结束对话消息")

        except Exception as e:
            logger.error(f"[{chat_id}] 发送结束对话消息失败: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 主动消息 (Proactive Messages)
    # ------------------------------------------------------------------

    async def _generate_daily_plan_via_llm(self) -> List[Dict[str, str]]:
        """
        LLM 回调：读取 SCHEDULE.md + SOUL.md + USER.md + MEMORY.md，
        生成今日主动消息触发计划。
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz_str = config.get_message_history_config().get("timezone", "Asia/Shanghai")
        now = datetime.now(ZoneInfo(tz_str))
        
        schedule_content = _read_file_safe(WORKSPACE_SCHEDULE_FILE)
        soul_content = _read_file_safe(WORKSPACE_SOUL_FILE, max_chars=2000)
        user_content = _read_file_safe(WORKSPACE_USER_FILE, max_chars=2000)
        memory_file = _resolve_memory_file("MEMORY.md")
        memory_content = _read_file_safe(memory_file, max_chars=2000)

        system_prompt = render_template(
            "schedule.jinja", "daily_plan_system",
        )
        user_prompt = render_template(
            "schedule.jinja", "daily_plan_user",
            date_display=now.strftime('%Y年%m月%d日 %A'),
            current_time=now.strftime('%H:%M'),
            schedule_content=schedule_content,
            soul_content=soul_content,
            user_content=user_content,
            memory_content=memory_content,
        )

        try:
            response = ""
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=[]
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += chunk["content"]
                elif isinstance(chunk, str):
                    response += chunk

            response = response.strip()
            if response.startswith("```"):
                response = response.split("\n", 1)[-1]
                if response.endswith("```"):
                    response = response[:-3]
                response = response.strip()
            
            plan = json.loads(response)
            if isinstance(plan, list):
                logger.info(f"LLM 生成了 {len(plan)} 个触发计划")
                return plan
            else:
                logger.warning(f"LLM 返回了非数组: {type(plan)}")
                return []

        except json.JSONDecodeError as e:
            logger.error(f"解析今日计划 JSON 失败: {e}, response={response[:200]}")
            return []
        except Exception as e:
            logger.error(f"LLM 生成今日计划异常: {e}", exc_info=True)
            return []

    async def _send_proactive_message(self, chat_id: str, reason: str, event_type: str = "proactive"):
        """
        Scheduler 回调：到达触发时间后，用 LLM 生成主动消息并发送。
        """
        reason_display = reason if reason else "(无缘由)"
        logger.info(f"[{chat_id}] 触发主动消息: type={event_type}, reason={reason_display}")

        soul = get_soul_prompt()

        if event_type == "alarm":
            system_prompt = render_template("schedule.jinja", "alarm_system", soul_prompt=soul)
            user_prompt = render_template("schedule.jinja", "alarm_user", reason=reason)
        else:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            tz_str = config.get_message_history_config().get("timezone", "Asia/Shanghai")
            now = datetime.now(ZoneInfo(tz_str))
            system_prompt = render_template("schedule.jinja", "proactive_system", soul_prompt=soul)
            user_prompt = render_template("schedule.jinja", "proactive_user", current_time=now.strftime('%H:%M'), reason=reason)

        db_context = self.message_history.get_context_messages("telegram", chat_id)
        proactive_history = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in db_context
            if msg.get("role") in ("user", "assistant", "system") and msg.get("content")
        ]
        if len(proactive_history) > 12:
            proactive_history = proactive_history[-12:]

        if len(proactive_history) < 4:
            session = self.sessions.get(chat_id, {})
            session_history = session.get("history", [])
            session_tail = [
                {"role": h.get("role"), "content": h.get("content")}
                for h in session_history[-8:]
                if h.get("role") in ("user", "assistant", "system") and h.get("content")
            ]
            if session_tail:
                proactive_history = (proactive_history + session_tail)[-12:]

        try:
            response = ""
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=proactive_history,
                tools=[]
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += chunk["content"]
                elif isinstance(chunk, str):
                    response += chunk

            response = self._strip_thinking_content(response.strip())
            if response:
                await self.adapter.send_message(chat_id, response)
                storage_id = chat_id
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant"
                )

                session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
                session_history = session.setdefault("history", [])
                trigger_note = f"[proactive:{event_type}] {reason}" if reason else f"[proactive:{event_type}]"
                session_history.append({"role": "system", "content": trigger_note})
                session_history.append({"role": "assistant", "content": response})
                if len(session_history) > 20:
                    session["history"] = session_history[-20:]

                logger.info(f"[{chat_id}] 主动消息已发送: {response[:50]}...")
            else:
                logger.warning(f"[{chat_id}] LLM 未生成主动消息内容")

        except Exception as e:
            logger.error(f"[{chat_id}] 发送主动消息失败: {e}", exc_info=True)
