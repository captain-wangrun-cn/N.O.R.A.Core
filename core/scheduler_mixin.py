'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:37:36
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""调度器 Mixin — Scheduler 集成 + 对话延续 + 主动消息 + 每日总结。"""
# mypy: disable-error-code=attr-defined
# pyright: reportAttributeAccessIssue=false

import asyncio
import random
import json
import logging
import re
from typing import Dict, Any, List

from brain.prompts import get_soul_prompt, render_template, _read_file_safe, WORKSPACE_SCHEDULE_FILE, WORKSPACE_SOUL_FILE, WORKSPACE_USER_FILE, WORKSPACE_MEMORY_FILE, _resolve_memory_file, load_custom_prompt, should_inject_custom
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
from core.routing import parse_route_markers, sanitize_adapter_output_text
from core.group_presence_store import GroupPresence
from core.private_presence_store import PrivatePresence
from core.conversation_identity import normalize_chat_type
from core.delivery_target_store import load_known_scenes
from workspace_config import get_workspace_manager
from memory.message_history import MessageHistory
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import os
import config

logger = logging.getLogger(__name__)


def _load_yesterday_daily_summary() -> str:
    """仅读取昨天的每日总结。"""
    try:
        ws = get_workspace_manager()
        summary_dir = Path(ws.data_dir) / "memory"
    except Exception as e:
        logger.warning(f"读取每日总结目录失败: {e}")
        return ""

    if not summary_dir.exists():
        return ""

    target_date = datetime.now().date() - timedelta(days=1)
    summary_path = summary_dir / f"{target_date.isoformat()}.md"
    if not summary_path.exists():
        return ""

    try:
        raw_text = summary_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"读取每日总结失败 {summary_path.name}: {e}")
        return ""

    if not raw_text:
        return ""

    normalized = re.sub(r"^#\s*Daily Summary\s+.*$", "", raw_text, flags=re.MULTILINE).strip()
    if not normalized:
        return ""

    return f"## {target_date.isoformat()}\n{normalized}"


class SchedulerMixin:
    """Scheduler 集成、对话延续（followup）、主动消息（proactive）。"""

    # ------------------------------------------------------------------
    # 每日总结
    # ------------------------------------------------------------------

    async def _generate_daily_summary(self, target_date: date):
        """
        生成指定日期的对话每日总结，写入 workspace/data/memory/yyyy-mm-dd.md。

        - 如果文件已存在，会把旧内容带入提示，生成融合后的新摘要。
        - 若无新的对话消息且已有文件，跳过改写。
        """

        # 1) 准备时区与时间范围
        try:
            tz_str = config.get_message_history_config().get("timezone", "Asia/Shanghai")
        except Exception:
            tz_str = "Asia/Shanghai"
        tz = ZoneInfo(tz_str)
        start_dt = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=tz)
        end_dt = start_dt + timedelta(days=1)
        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        # 2) 确定会话（默认用 scheduler.default_chat_id，内部存 runtime_key）
        target_key = ""
        scheduler = getattr(self, "scheduler", None)
        if scheduler:
            target_key = getattr(scheduler, "default_chat_id", "") or ""
        sessions = getattr(self, "sessions", {}) or {}
        if not target_key and sessions:
            target_key = next(iter(sessions.keys()), "")

        if not target_key:
            logger.warning("每日总结跳过：default_chat_id 未设置，尚无会话")
            return

        identity = self._identity_for_runtime_key(target_key)
        platform = identity.platform
        storage_id = identity.storage_id

        # 3) 读取当日消息
        msgs = []
        try:
            history = getattr(self, "message_history", None)
            if history:
                msgs = history.get_messages_between(
                    platform, storage_id, start_ts, end_ts, memory_scope_id=identity.memory_scope_id
                )
        except Exception as e:
            logger.error(f"每日总结：读取消息失败: {e}")
            msgs = []

        # 4) 读取旧文件内容（若有），供融合
        ws = get_workspace_manager()
        summary_dir = Path(ws.data_dir) / "memory"
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / f"{target_date.isoformat()}.md"
        existing_content = ""
        if summary_path.exists():
            try:
                existing_content = summary_path.read_text(encoding="utf-8")
            except Exception:
                existing_content = ""

        if not msgs and existing_content:
            logger.info(f"每日总结：无新消息，保留已有文件 {summary_path.name}")
            return

        # 5) 构造对话文本
        convo_lines = []
        for m in msgs:
            ts = m.get("timestamp", 0)
            try:
                ts_str = datetime.fromtimestamp(ts, tz).strftime("%H:%M")
            except Exception:
                ts_str = "--:--"
            role = m.get("role", "?")
            content = m.get("raw_content") or m.get("content") or ""
            convo_lines.append(f"[{ts_str}] {role}: {content}")
        convo_text = "\n".join(convo_lines)

        # 6) 选择总结模型
        llm_client = getattr(self, "llm", None)
        try:
            from brain.llm import get_llm_client
            summary_client = get_llm_client(model_alias="summary")
            llm_client = summary_client or llm_client
        except Exception:
            pass

        if llm_client is None:
            logger.warning("每日总结跳过：未找到可用的 LLM 客户端")
            return

        # 7) 生成总结
        system_prompt = (
            "你是对话日志整理助手，请为给定日期生成简明的每日总结。"
            " 保留关键事件、决定、情绪、待办，避免复述冗余。"
        )
        user_prompt = (
            f"日期: {target_date.isoformat()}\n"
            f"已有总结(可为空):\n{existing_content}\n\n"
            f"当日对话原文:\n{convo_text}\n\n"
            "请输出 Markdown，要点列表和待办 (如有)。"
        )

        summary_text = ""
        try:
            summary_text = await llm_client.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
            )
        except Exception as e:
            logger.error(f"每日总结生成失败，使用回退: {e}")

        if not summary_text:
            if not msgs:
                summary_text = "今日无对话记录。"
            else:
                summary_text = "生成失败，以下为原始要点：\n" + "\n".join(convo_lines[:20])

        content_to_write = f"# Daily Summary {target_date.isoformat()}\n\n" + summary_text.strip()
        try:
            summary_path.write_text(content_to_write, encoding="utf-8")
            logger.info(f"每日总结已写入: {summary_path}")
        except Exception as e:
            logger.error(f"每日总结写入失败: {e}")

    # ------------------------------------------------------------------
    # Scheduler 启停
    # ------------------------------------------------------------------

    def start_scheduler(self, default_chat_id: str = ""):
        """启动主动消息调度器。应在 adapter 就绪后调用。"""
        if self.scheduler:
            if default_chat_id:
                self.scheduler.default_chat_id = default_chat_id
            elif not self.scheduler.default_chat_id:
                # 兜底：再次尝试从持久化文件读取（init 时可能 workspace 还未就绪）
                try:
                    from core.default_chat_id_store import load_default_chat_target
                    persisted_target = load_default_chat_target()
                    if persisted_target:
                        identity = self._identity_from_target(persisted_target)
                        self.scheduler.default_chat_id = identity.runtime_key
                except Exception as e:
                    logger.warning(f"start_scheduler 兜底读取 default_chat_id 失败: {e}")
            self.scheduler.start()
            logger.info(
                f"Scheduler 已启动, default_chat_id={self.scheduler.default_chat_id or '(empty)'}"
            )

    def stop_scheduler(self):
        """停止调度器。"""
        if self.scheduler:
            self.scheduler.stop()

    # ------------------------------------------------------------------
    # 状态同步
    # ------------------------------------------------------------------

    def _active_presence_for_runtime(self, chat_id: str, chat_type: str = "") -> str:
        """Return the online presence value ('online' / 'semi_online') for the given runtime.

        - private → PrivatePresenceStore.get(memory_scope_id)
        - group   → GroupListenerManager.mode_for(chat_id).value
        """
        normalized = normalize_chat_type(chat_type)
        if normalized == "group":
            return self.group_listener.mode_for(chat_id).value
        identity = self._identity_for_runtime_key(chat_id)
        return self.private_presence.get(identity.memory_scope_id).value

    def _update_scheduler_state(self, chat_id: str, busy: bool):
        """同步 controller 的忙碌状态到全局 AI 状态。"""
        if busy:
            set_ai_backend_busy(True)
            set_ai_generating(True)
            # 私聊后脑启动 → 标记该 scope 私聊 ONLINE
            identity = self._identity_for_runtime_key(chat_id)
            if normalize_chat_type(identity.chat_type) != "group":
                self.private_presence.set(
                    identity.memory_scope_id,
                    PrivatePresence.ONLINE,
                    reason="backend_started",
                )
        else:
            self._sync_global_backend_flags()
        state = get_ai_state_summary()
        private_mode = self.private_presence.get(
            self._identity_for_runtime_key(chat_id).memory_scope_id
        )
        logger.info(
            f"[{chat_id}] 同步在线状态: global={state['presence']}, "
            f"private={private_mode.value}, "
            f"generating={state['is_generating']}, backend_busy={state['is_backend_busy']}"
        )

    def _mark_scheduler_idle(self, chat_id: str, *, initial_delay: float | None = None):
        """标记 AI 生成完毕；私聊启动对话延续定时器。"""
        self._sync_global_backend_flags()
        state = get_ai_state_summary()
        identity = self._identity_for_runtime_key(chat_id)
        if normalize_chat_type(identity.chat_type) == "group":
            logger.debug(
                f"[{chat_id}] 群聊生成完毕: global={state['presence']}, "
                f"generating={state['is_generating']}, backend_busy={state['is_backend_busy']}；"
                "后续监听由 group listener 管理。"
            )
        else:
            private_mode = self.private_presence.get(identity.memory_scope_id)
            logger.info(
                f"[{chat_id}] 进入空闲跟进状态: global={state['presence']}, "
                f"private={private_mode.value}, "
                f"generating={state['is_generating']}, backend_busy={state['is_backend_busy']}"
            )
        if self._followup_suspended_until_idle.pop(chat_id, False):
            logger.info(f"[{chat_id}] 当前消息段标记为 KEEP_SEGMENT_OPEN，暂不启动 followup 定时器。")
            return
        self._start_followup_timer(chat_id, initial_delay=initial_delay)

    # ------------------------------------------------------------------
    # 对话延续机制 (Conversation Follow-up)
    # ------------------------------------------------------------------

    def _start_followup_timer(self, chat_id: str, *, initial_delay: float | None = None):
        """启动私聊对话延续定时器；群聊生命周期由 group listener 管理。"""
        identity = self._identity_for_runtime_key(chat_id)
        if normalize_chat_type(identity.chat_type) == "group":
            self._cancel_followup_timer(chat_id)
            logger.debug(f"[{chat_id}] 群聊不启动 scheduler followup；由 group listener 管理。")
            return
        self._cancel_followup_timer(chat_id)
        delay = initial_delay if initial_delay is not None else self.FOLLOWUP_INITIAL_DELAY
        # 记录单次延迟，防止并发覆盖
        self._followup_delay_override[chat_id] = delay
        task = asyncio.create_task(self._followup_loop(chat_id))
        self._followup_timers[chat_id] = task
        logger.debug(f"[{chat_id}] 对话延续定时器已启动（{delay}s 后首次检测）")

    def _get_nora_preferences(self) -> dict:
        return config.get_nora_preferences()

    def _should_pass_probability(self, value: float) -> bool:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = 1.0
        numeric = max(0.0, min(1.0, numeric))
        return random.random() <= numeric

    def _cancel_followup_timer(self, chat_id: str):
        """取消对话延续定时器。"""
        self._followup_delay_override.pop(chat_id, None)
        task = self._followup_timers.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"[{chat_id}] 对话延续定时器已取消")

    def _suspend_followup_until_idle(self, chat_id: str):
        """本轮允许消息段不闭合：暂停 followup，直到下一次用户长时间不回复再恢复。"""
        self._cancel_followup_timer(chat_id)
        self._followup_suspended_until_idle[chat_id] = True
        logger.info(f"[{chat_id}] followup detect 已暂停，等待后续真正空闲时恢复。")

    # 告别/收尾词正则（确定性短路用）：
    # 当 AI 最近一条消息或用户最后一条消息匹配下面任一关键短语时，
    # 直接判定本段对话已自然收尾，followup_loop 不再发任何消息。
    _GOODBYE_PATTERNS = [
        re.compile(r"晚安", re.IGNORECASE),
        re.compile(r"再见", re.IGNORECASE),
        re.compile(r"拜拜|88|byebye|bye[\s\-_~!.。\?？]*$|see\s*you", re.IGNORECASE),
        re.compile(r"早点睡|早些睡|早点休息|去睡吧|睡个好觉|做个好梦|好梦"),
        re.compile(r"先这样(?:吧|啦|哦)?[\s。.~!！]*$"),
        re.compile(r"先(?:不|别)聊了"),
        re.compile(r"先下了|下线|下播|溜了|去忙(?:了|吧|啦)"),
        re.compile(r"有空再(?:聊|说|找你)"),
        re.compile(r"不打扰你了|你先忙"),
        re.compile(r"\[SEMI_ONLINE\]"),
        re.compile(r"\[TASK_DONE\]"),
        re.compile(r"^\s*good\s*night", re.IGNORECASE),
    ]

    def _looks_like_farewell(self, content: str) -> bool:
        """启发式判断一段消息是否已是告别/收尾基调。"""
        if not content:
            return False
        text = content.strip()
        if not text:
            return False
        # 仅检查最后 80 字符，避免大段无关文本干扰
        tail = text[-160:]
        for pat in self._GOODBYE_PATTERNS:
            if pat.search(tail):
                return True
        return False

    def _conversation_already_closed(self, chat_id: str) -> bool:
        """
        检查当前消息段最后几条消息是否已经互相告别/收尾。

        判定逻辑：
          - 取最近 6 条 user/assistant 消息
          - 若其中存在用户告别 + AI 告别 任一组合，视为已收尾
          - 或 AI 的最后一条消息直接是收尾性质（晚安/拜拜/有空再聊/[SEMI_ONLINE] 等）
        """
        try:
            identity = self._identity_for_runtime_key(chat_id)
            msgs = self._current_place_segment_messages(identity) or []
        except Exception as e:
            logger.debug(f"[{chat_id}] 读取当前段消息失败，无法做告别短路: {e}")
            return False
        recent = [m for m in msgs[-6:] if m.get("role") in ("user", "assistant") and m.get("content")]
        if not recent:
            return False

        last = recent[-1]
        last_role = last.get("role")
        last_content = str(last.get("content") or "")

        # 1) AI 最后一条已是收尾
        if last_role == "assistant" and self._looks_like_farewell(last_content):
            return True

        # 2) 用户告别 + AI 已回复（即 AI 最后一条紧跟在用户告别之后）
        if last_role == "assistant":
            # 找用户的最后一条
            for m in reversed(recent[:-1]):
                if m.get("role") == "user":
                    if self._looks_like_farewell(str(m.get("content") or "")):
                        return True
                    break

        # 3) 用户最后一条就是告别（AI 还没回，但马上就要回；followup 不该插队）
        if last_role == "user" and self._looks_like_farewell(last_content):
            return True

        return False

    def _current_place_segment_messages(self, identity) -> List[Dict[str, Any]]:
        """Return current open-segment messages from the same runtime/place only.

        普通后脑上下文可以跨平台共享，但 followup/wrapup 会主动发消息，
        必须只依据当前地点的最后对话，避免把 A 平台的内容追问到 B 平台。
        """
        msgs = self.message_history.get_current_segment_messages(
            identity.platform,
            identity.storage_id,
            memory_scope_id=identity.memory_scope_id,
        ) or []
        current_place = identity.place_scope_id or f"{identity.platform}:{identity.platform_chat_id}"
        return [
            m for m in msgs
            if str(m.get("place_scope_id") or f"{m.get('platform') or ''}:{m.get('chat_id') or ''}") == current_place
        ]

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
            delay = self._followup_delay_override.pop(chat_id, self.FOLLOWUP_INITIAL_DELAY)
            await asyncio.sleep(delay)

            # 🛡️ 启动后第一道闸：若上一段对话已经互相告别（或 AI 最后一条本身已是收尾），
            # 直接静默进入 SEMI_ONLINE，不再发任何追话/wrapup 消息。
            if self._conversation_already_closed(chat_id):
                logger.info(f"[{chat_id}] 检测到对话已自然告别，followup_loop 静默退出，不发任何消息")
                self._transition_to_semi_online(chat_id)
                return

            wait_loops = 0  # 连续 WAIT 轮数（用于超时收尾）
            skipped_followups = 0  # FOLLOWUP 命中但因概率未发送的次数

            while True:
                identity = self._identity_for_runtime_key(chat_id)
                current_presence = self._active_presence_for_runtime(chat_id, identity.chat_type)
                if current_presence != "online":
                    logger.debug(f"[{chat_id}] 当前对话在线状态非 online ({current_presence})，followup 退出")
                    return

                busy_runtime = self.get_busy_backend_runtime(identity.memory_scope_id)
                status = self.worker_status.get(busy_runtime) if busy_runtime else self.worker_status.get(chat_id)
                if status and status.busy:
                    logger.debug(f"[{chat_id}] 共享后端忙碌({busy_runtime or chat_id})，对话延续循环退出")
                    return

                idle_secs = get_user_idle_seconds(chat_id)
                if idle_secs < 0:
                    idle_secs = 0
                count = get_followup_count(chat_id)

                if count >= self.FOLLOWUP_MAX_COUNT:
                    logger.info(f"[{chat_id}] 连续追话已达 {count} 次，引导结束对话")
                    await self._send_wrapup_message(chat_id)
                    self._transition_to_semi_online(chat_id)
                    return

                decision = await self._detect_followup_intent(chat_id, idle_secs, count)
                logger.info(f"[{chat_id}] 对话延续检测结果: {decision} (idle={idle_secs:.0f}s, count={count})")

                # 🛡️ 第二道闸：每次 decision 出来后，再次确认对话是否已经互相告别。
                # fast 模型有时会在已告别场景误判 FOLLOWUP/WAIT；这里用确定性正则兜底，
                # 一旦命中告别词就静默收尾，绝不再发任何消息。
                if self._conversation_already_closed(chat_id):
                    logger.info(
                        f"[{chat_id}] 决策={decision} 但检测到对话已自然告别，强制静默退出 followup_loop"
                    )
                    self._transition_to_semi_online(chat_id)
                    return

                if decision == "FOLLOWUP":
                    wait_loops = 0  # 重置 WAIT 计数
                    prefs = self._get_nora_preferences()
                    followup_probability = prefs.get("nora_followup_probability", 1.0)
                    skip_end_after = int(prefs.get("followup_skip_end_after", 3) or 3)

                    if self._should_pass_probability(followup_probability):
                        skipped_followups = 0
                        await self._send_followup_message(chat_id, idle_secs)
                        record_ai_followup(chat_id)
                        await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)
                    else:
                        skipped_followups += 1
                        logger.info(
                            f"[{chat_id}] FOLLOWUP 命中但未通过发送概率，转 WAIT (skip={skipped_followups}, idle={idle_secs:.0f}s)"
                        )
                        if skipped_followups >= max(1, skip_end_after):
                            logger.info(f"[{chat_id}] FOLLOWUP 连续跳过达到上限，直接 END")
                            # 仅当 AI 真的追话过且用户没回，才发 wrapup；其余静默收尾
                            if count >= 2 and not self._conversation_already_closed(chat_id):
                                await self._send_wrapup_message(chat_id)
                                await asyncio.sleep(self.FOLLOWUP_END_GRACE_SECONDS)
                            self._transition_to_semi_online(chat_id)
                            return
                        wait_loops += 1
                        await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

                elif decision == "WAIT":
                    wait_loops += 1
                    # 用户长时间未回或 WAIT 次数过多，直接收尾，避免无限 WAIT
                    if idle_secs >= self.FOLLOWUP_END_IDLE_SECONDS or wait_loops >= self.FOLLOWUP_MAX_WAIT_LOOPS:
                        logger.info(
                            f"[{chat_id}] WAIT 超时收尾: idle={idle_secs:.0f}s, wait_loops={wait_loops}, count={count}"
                        )
                        # 仅当 AI 真的追话过（count>=2）且不是已告别的场景，才发 wrapup；
                        # 其余情况（包括 count==0 + 已自然告别）一律静默退出。
                        if count >= 2 and not self._conversation_already_closed(chat_id):
                            await self._send_wrapup_message(chat_id)
                        self._transition_to_semi_online(chat_id)
                        return
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

                elif decision == "END":
                    # END 决策：fast 模型已经判定本段对话该结束。
                    # 只有 AI 真的追话过（count>=2）才需要补一句温和的收尾；
                    # 若 count==0/1 说明 AI 前脑已经自然告别（或一直没追话），不需要再发任何消息。
                    if count >= 2:
                        # 二次安全闸：若已检测到对话本身已自然告别，仍跳过 wrapup
                        if not self._conversation_already_closed(chat_id):
                            await self._send_wrapup_message(chat_id)
                            await asyncio.sleep(self.FOLLOWUP_END_GRACE_SECONDS)
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

    async def _apply_presence_markers(
        self,
        chat_id: str,
        chat_type: str,
        *,
        force_online: bool = False,
        force_semi_online: bool = False,
    ) -> bool:
        """Apply model presence markers without conflating group mode and shared presence."""
        if chat_type == "group":
            if force_semi_online:
                await self.group_listener.set_mode(
                    chat_id,
                    GroupPresence.SEMI_ONLINE,
                    reason="front_marker_semi_online",
                )
                return True
            if force_online:
                await self.group_listener.set_mode(
                    chat_id,
                    GroupPresence.ONLINE,
                    reason="front_marker_online",
                )
            return False

        # 私聊分支
        identity = self._identity_for_runtime_key(chat_id)
        if force_online:
            self.private_presence.set(
                identity.memory_scope_id,
                PrivatePresence.ONLINE,
                reason="front_marker_online",
            )
            return False
        if force_semi_online:
            self._transition_to_semi_online(chat_id)
            return True
        return False

    def _transition_to_semi_online(self, chat_id: str):
        """安全地从 ONLINE 过渡到 SEMI_ONLINE，同时关闭当前对话段落。"""
        identity = self._identity_for_runtime_key(chat_id)
        clear_conversation_tracking(chat_id)
        self._followup_timers.pop(chat_id, None)
        self._followup_delay_override.pop(chat_id, None)

        active_keys = self.get_active_runtime_keys(identity.memory_scope_id)
        if active_keys:
            self._sync_global_backend_flags()
            logger.info(
                f"[{chat_id}] 当前地点已空闲，但共享作用域仍活跃: {active_keys}，暂不关闭消息段"
            )
            return

        if normalize_chat_type(identity.chat_type) != "group":
            self.private_presence.set(
                identity.memory_scope_id,
                PrivatePresence.SEMI_ONLINE,
                reason="shared_scope_idle",
            )
        self._sync_global_backend_flags()

        try:
            session_id = self.message_history.close_session(
                platform=identity.platform,
                chat_id=identity.storage_id,
                trigger_type="user",
                memory_scope_id=identity.memory_scope_id,
            )
            if session_id:
                logger.info(f"[{chat_id}] 对话段落 #{session_id} 已封闭")
        except Exception as e:
            logger.error(f"[{chat_id}] 关闭对话段落失败: {e}", exc_info=True)

        logger.info(f"[{chat_id}] 对话结束，私聊进入 SEMI_ONLINE 状态")

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

        identity = self._identity_for_runtime_key(chat_id)
        storage_id = identity.storage_id
        current_segment_msgs = self._current_place_segment_messages(identity)
        recent_msgs = current_segment_msgs[-10:] if current_segment_msgs else []
        recent_conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
            for m in recent_msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        )

        if not recent_conversation:
            return "END"

        try:
            system_prompt = render_template('schedule.jinja', 'followup_detect_system')
            if should_inject_custom("fast"):
                custom_prompt = load_custom_prompt()
                if custom_prompt:
                    system_prompt = (
                        system_prompt
                        + "\n\n"
                        + render_template('context_injection.jinja', 'custom', custom_content=custom_prompt)
                    )
            system_prompt += (
                "\n\n【强制输出】你必须且只能输出 FOLLOWUP / WAIT / END 之一。"
                "不要输出其他任何内容。"
            )
            user_prompt = render_template('schedule.jinja', 'followup_detect_user',
                                          recent_conversation=recent_conversation,
                                          idle_seconds=int(idle_secs),
                                          followup_count=followup_count,
                                          current_time=now.strftime('%H:%M'))

            response = ""
            stream = self._chat_stream_wrapper(
                self.fast_llm,
                chat_id,
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

        identity = self._identity_for_runtime_key(chat_id)
        storage_id = identity.storage_id
        current_segment_msgs = self._current_place_segment_messages(identity)
        recent_msgs = current_segment_msgs[-10:] if current_segment_msgs else []
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
            stream = self._chat_stream_wrapper(
                self.llm,
                chat_id,
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
                route, response = parse_route_markers(response)
                response = sanitize_adapter_output_text(response)
                self._log_route_resolution(
                    "followup",
                    route,
                    identity.runtime_key,
                    {
                        "runtime_key": identity.runtime_key,
                        "redirected": False,
                        "policy": "origin_locked" if route.get("platform") or route.get("scene_id") else "default_current",
                    },
                )
            if response:
                await self._send_split_response(chat_id, response)
                self.message_history.add_message(
                    platform=identity.platform,
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant",
                    memory_scope_id=identity.memory_scope_id,
                    place_scope_id=identity.place_scope_id,
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

    async def _send_split_response(self, chat_id: str, response: str) -> bool:
        """按 [SPLIT] / [SPLIT:秒数] 分段发送。"""
        if self._SPLIT_MARKER_PATTERN.search(response):
            await self._send_split_message(chat_id, response)
            return True

        await self._send_platform_message(chat_id, response)
        return False

    async def _send_wrapup_message(self, chat_id: str):
        """生成并发送结束对话的消息，同时写入聊天记录。"""
        soul = get_soul_prompt()
        idle_secs = get_user_idle_seconds(chat_id)
        count = get_followup_count(chat_id)

        identity = self._identity_for_runtime_key(chat_id)
        storage_id = identity.storage_id
        current_segment_msgs = self._current_place_segment_messages(identity)
        recent_msgs = current_segment_msgs[-10:] if current_segment_msgs else []
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
            stream = self._chat_stream_wrapper(
                self.llm,
                chat_id,
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
                route, response = parse_route_markers(response)
                response = sanitize_adapter_output_text(response)
                self._log_route_resolution(
                    "wrapup",
                    route,
                    identity.runtime_key,
                    {
                        "runtime_key": identity.runtime_key,
                        "redirected": False,
                        "policy": "origin_locked" if route.get("platform") or route.get("scene_id") else "default_current",
                    },
                )
            if response:
                await self._send_split_response(chat_id, response)
                self.message_history.add_message(
                    platform=identity.platform,
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant",
                    memory_scope_id=identity.memory_scope_id,
                    place_scope_id=identity.place_scope_id,
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

    async def _generate_daily_plan_via_llm(self, chat_id: str) -> List[Dict[str, str]]:
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
            stream = self._chat_stream_wrapper(
                self.llm,
                chat_id,
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
                normalized_plan = []
                for item in plan:
                    if not isinstance(item, dict):
                        continue
                    normalized = dict(item)
                    kind = str(item.get("message_kind", "autonomous") or "autonomous").strip().lower()
                    if kind not in {"autonomous", "explicit"}:
                        kind = "autonomous"
                    normalized["message_kind"] = kind
                    normalized_plan.append(normalized)
                logger.info(f"LLM 生成了 {len(plan)} 个触发计划")
                return normalized_plan
            else:
                logger.warning(f"LLM 返回了非数组: {type(plan)}")
                return []

        except json.JSONDecodeError as e:
            logger.error(f"解析今日计划 JSON 失败: {e}, response={response[:200]}")
            return []
        except Exception as e:
            logger.error(f"LLM 生成今日计划异常: {e}", exc_info=True)
            return []

    async def _send_proactive_message(self, chat_id: str, reason: str, event_type: str = "proactive", message_kind: str = "autonomous"):
        """
        Scheduler 回调：到达触发时间后，用 LLM 生成主动消息并发送。
        """
        identity = self._identity_for_runtime_key(chat_id)
        chat_id = identity.runtime_key
        platform = identity.platform
        storage_id = identity.storage_id
        platform_chat_id = identity.platform_chat_id
        message_kind = (message_kind or "autonomous").strip().lower()
        if event_type != "alarm" and message_kind == "autonomous":
            prefs = self._get_nora_preferences()
            probability = prefs.get("proactive_message_probability", 1.0)
            if not self._should_pass_probability(probability):
                logger.info(f"[{chat_id}] 自主型主动消息被概率跳过: type={event_type}, reason={reason}")
                return

        reason_display = reason if reason else "(无缘由)"
        logger.info(f"[{chat_id}] 触发主动消息: type={event_type}, kind={message_kind}, reason={reason_display}")

        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz_str = config.get_message_history_config().get("timezone", "Asia/Shanghai")
        now = datetime.now(ZoneInfo(tz_str))
        current_time = now.strftime('%H:%M')
        recent_daily_summaries = _load_yesterday_daily_summary()

        # 触发前提取完整对话线索，确保 trigger 消息基于完整上下文继续生成
        recent_hint = ""
        try:
            db_context = self.message_history.get_context_messages(
                platform, storage_id,
                memory_scope_id=identity.memory_scope_id, current_place_scope_id=identity.place_scope_id,
            )
            recent_msgs = db_context if db_context else []
            hint_lines = []
            for m in recent_msgs:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = str(m.get("content", "")).strip()
                if not content:
                    continue
                speaker = "用户" if role == "user" else "AI"
                hint_lines.append(f"{speaker}: {content}")
            recent_hint = "\n".join(hint_lines)
        except Exception as e:
            logger.warning(f"[{chat_id}] 构建 trigger 完整对话上下文失败: {e}")

        # 构造前脑上下文与元数据
        proactive_meta = {
            "event_type": event_type,
            "message_kind": message_kind,
            "reason": reason,
            "current_time": current_time,
            "trigger_from": "scheduler",
            "recent_hint": recent_hint,
            "recent_daily_summaries": recent_daily_summaries,
        }
        front_context = {
            "platform": platform,
            "chat_id": platform_chat_id,
            "user_id": identity.actor_user_id,
            "chat_type": identity.chat_type,
            "user_name": "User",
            "text": f"[proactive:{event_type}:{message_kind}] {reason_display}",
        }

        front_result = await self._generate_front_chat_response(front_context, proactive_meta=proactive_meta)
        user_reply = front_result.get("user_reply", "")
        raw_front_reply = front_result.get("raw_response", "")

        if isinstance(user_reply, str):
            user_reply = user_reply.strip()

        # 发送前脑回复。闹钟与显式提醒锁定创建时目标；自主消息可选择已知目标，
        # 但进入群聊必须显式使用 typed [scene:group:ID]。
        send_key = chat_id
        send_chat_type = identity.chat_type
        dest_identity = identity
        redirected = False
        route = front_result.get("route") or {}
        route_has_marker = bool(route.get("platform") or route.get("scene_id"))
        route_source = "alarm" if event_type == "alarm" else "proactive"
        origin_locked = event_type == "alarm" or message_kind == "explicit"
        if origin_locked:
            self._log_route_resolution(
                route_source,
                route,
                identity.runtime_key,
                {
                    "runtime_key": identity.runtime_key,
                    "redirected": False,
                    "policy": "origin_locked" if route_has_marker else "default_current",
                },
            )
        else:
            send_target = self.resolve_send_target(route, identity, route_source=route_source)
            send_key = str(send_target.get("runtime_key") or "").strip()
            send_chat_type = send_target.get("chat_type") or identity.chat_type
            redirected = bool(send_target.get("redirected"))
            dest_identity = send_target.get("identity") or identity
            if send_key and redirected:
                known_target = any(
                    str(scene.get("runtime_key") or "") == send_key
                    and str(scene.get("chat_type") or "") == send_chat_type
                    for scene in load_known_scenes()
                )
                if not known_target:
                    self._log_route_resolution(
                        route_source,
                        route,
                        identity.runtime_key,
                        {
                            "runtime_key": "",
                            "redirected": False,
                            "policy": "rejected_unknown_scene",
                        },
                    )
                    send_key = ""
                elif send_chat_type == "group" and route.get("scene_type") != "group":
                    self._log_route_resolution(
                        route_source,
                        route,
                        identity.runtime_key,
                        {
                            "runtime_key": "",
                            "redirected": False,
                            "policy": "rejected_group_not_explicit",
                        },
                    )
                    send_key = ""

        sent_reply = bool(user_reply and send_key)
        if sent_reply:
            await self._send_split_message(send_key, user_reply, chat_type=send_chat_type)

            log_platform = dest_identity.platform if redirected else platform
            log_storage_id = dest_identity.storage_id if redirected else storage_id
            log_memory_scope_id = dest_identity.memory_scope_id if redirected else identity.memory_scope_id
            log_place_scope_id = dest_identity.place_scope_id if redirected else identity.place_scope_id
            self.message_history.add_message(
                platform=log_platform,
                chat_id=log_storage_id,
                role="assistant",
                content=user_reply,
                user_id="assistant",
                memory_scope_id=log_memory_scope_id,
                place_scope_id=log_place_scope_id,
            )
        else:
            if user_reply and not send_key:
                logger.info(f"[{chat_id}] 主动消息投递目标被拒绝，跳过发送")
            else:
                logger.info(f"[{chat_id}] 主动消息生成为空，跳过发送")

        # 记录到实际目标的 session 历史（含触发标记）。若投递被拒绝，不记录伪发送。
        session_key = send_key if sent_reply else chat_id
        session = self.sessions.setdefault(session_key, {"history": [], "interrupted_thought": "", "pending_text": ""})
        session_history = session.setdefault("history", [])
        trigger_note = f"[proactive:{event_type}:{message_kind}] {reason}" if reason else f"[proactive:{event_type}:{message_kind}]"
        session_history.append({"role": "system", "content": trigger_note})
        if sent_reply:
            session_history.append({"role": "assistant", "content": user_reply})
        if len(session_history) > 20:
            session["history"] = session_history[-20:]

        # 如需后脑则启动轮询，否则标记空闲
        if front_result.get("needs_backend") and send_key:
            backend_instruction = (
                f"主动消息触发（类型: {event_type}, 分类: {message_kind}, 时间: {current_time}, 缘由: {reason_display}）。"
                "请完成前脑提到的需要后台处理的任务，生成最终要发送给用户的结果。"
            )
            if user_reply:
                backend_instruction += f"\n\n前脑已对用户说: {user_reply}"
            elif raw_front_reply:
                backend_instruction += f"\n\n前脑原始回复: {raw_front_reply}"

            task_instruction = front_result.get("task_instruction", "")
            if task_instruction:
                backend_instruction += f"\n\n【任务指示】\n{task_instruction}"

            backend_context = {
                "platform": dest_identity.platform,
                "chat_id": dest_identity.platform_chat_id,
                "chat_type": dest_identity.chat_type,
                "user_id": dest_identity.actor_user_id,
                "user_name": "User",
                "text": backend_instruction,
                "_front_brain_handled": True,
                "_front_brain_reply": user_reply,
                "task_instruction": task_instruction,
                "use_image_model": front_result.get("use_image_model", False),
            }

            busy_runtime = self.get_busy_backend_runtime(dest_identity.memory_scope_id)
            queue = self._get_scope_queue_for_runtime(send_key)
            if busy_runtime or (queue and queue.size() > 0):
                queue = self._get_scope_queue_for_runtime(send_key, create=True)
                await queue.enqueue({
                    "context": backend_context,
                    "text": backend_context.get("text", ""),
                })
                logger.info(f"[{send_key}] 主动消息后脑任务已加入共享队列 (队列长度 {queue.size()})")
            else:
                logger.info(f"[{send_key}] 前脑标记需要后脑，启动轮询处理主动消息")
                task = asyncio.create_task(self._run_polling_loop(backend_context))
                self.generation_tasks[send_key] = task
        else:
            logger.info(f"[{chat_id}] 主动消息前脑已完成，无需后脑")
            self._mark_scheduler_idle(chat_id)
