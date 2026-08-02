'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-08 23:55:34
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-08 23:07:35
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
core/scheduler.py — N.O.R.A. 主动消息调度系统 (Proactive Scheduler)

基于 APScheduler AsyncIOScheduler 的事件驱动调度，替代旧版 30 秒轮询。

功能:
1. 全局系统忙碌状态管理 (AIPresence): ONLINE / SEMI_ONLINE（系统级，不表示私聊在线）
2. 私聊在线状态管理 (PrivatePresenceStore): 按 memory_scope_id 独立存储
3. 每天凌晨 00:00 (CronTrigger) 根据 SCHEDULE.md + 身份文件生成今日主动消息触发计划
4. 每个触发点使用 DateTrigger 精准调度，到时直接回调
5. 动态闹钟: 允许 AI 实时添加定时/倒计时闹钟 (DateTrigger)，持久化到本地缓存
6. 主动消息在目标用户私聊在线或系统忙碌时被跳过
7. 群聊定时 ONLINE 监听: 凌晨生成当日「时间 + 平台 + 群号」计划（workspace/GROUP_LISTEN.json），
   每分钟 tick 检查到点条目，一个时间点只激活一个群；目标群已 ONLINE 时跳过
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, date
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ==================================================================
# 全局 AI 在线状态 (Global AI Presence)
# ==================================================================

class AIPresence(Enum):
    """全局系统级忙碌状态（System Presence）。

    两态模型:
    - SEMI_ONLINE: 系统空闲 — 允许调度主动消息
    - ONLINE: 系统忙碌 — 忽略调度主动消息

    注：此状态不再表示某个私聊的在线状态。
    私聊在线状态由 PrivatePresenceStore 按 scope 管理。
    群聊在线状态由 GroupPresenceStore 按群管理。
    """
    SEMI_ONLINE = "semi_online"  # 系统空闲 — 允许调度主动消息
    ONLINE = "online"            # 系统忙碌 — 忽略调度主动消息


class _AIPresenceState:
    """全局 AI 在线状态持有者（单例容器）。"""

    def __init__(self):
        self.presence: AIPresence = AIPresence.SEMI_ONLINE
        self.is_generating: bool = False
        self.is_backend_busy: bool = False
        # 对话延续追踪 (per-chat)
        self._last_user_message_time: Dict[str, float] = {}   # chat_id -> timestamp
        self._last_ai_followup_time: Dict[str, float] = {}    # chat_id -> timestamp (上次 AI 追话时间)
        self._followup_count: Dict[str, int] = {}             # chat_id -> 连续追话次数

    @property
    def should_skip_proactive(self) -> bool:
        """系统级忙碌检查：生成中或后脑忙时跳过主动消息。"""
        if self.is_generating or self.is_backend_busy:
            return True
        return False

    def record_user_activity(self, chat_id: str):
        """记录用户最后一次发消息的时间，并重置追话计数器。"""
        self._last_user_message_time[chat_id] = time.time()
        self._followup_count[chat_id] = 0
        self._last_ai_followup_time.pop(chat_id, None)

    def get_user_idle_seconds(self, chat_id: str) -> float:
        """获取用户距离上次发消息过去了多少秒。无记录返回 -1。"""
        ts = self._last_user_message_time.get(chat_id)
        if ts is None:
            return -1
        return time.time() - ts

    def record_ai_followup(self, chat_id: str):
        """记录一次 AI 追话。"""
        self._last_ai_followup_time[chat_id] = time.time()
        self._followup_count[chat_id] = self._followup_count.get(chat_id, 0) + 1

    def get_followup_count(self, chat_id: str) -> int:
        """获取连续追话次数。"""
        return self._followup_count.get(chat_id, 0)

    def clear_conversation_tracking(self, chat_id: str):
        """清除对话追踪数据（进入 SEMI_ONLINE 时调用）。"""
        self._last_user_message_time.pop(chat_id, None)
        self._last_ai_followup_time.pop(chat_id, None)
        self._followup_count.pop(chat_id, None)


_ai_state = _AIPresenceState()


def get_ai_presence() -> AIPresence:
    return _ai_state.presence


def set_ai_presence(
    presence: AIPresence,
    *,
    reason: str = "unspecified",
    runtime_key: str = "",
):
    old = _ai_state.presence
    _ai_state.presence = presence
    if old != presence:
        runtime_part = f" runtime={runtime_key}" if runtime_key else ""
        logger.info(
            "[GLOBAL PRESENCE]%s %s -> %s reason=%s",
            runtime_part,
            old.value,
            presence.value,
            reason,
        )


def set_ai_generating(is_generating: bool):
    _ai_state.is_generating = is_generating


def set_ai_backend_busy(is_busy: bool):
    _ai_state.is_backend_busy = is_busy


def should_skip_proactive() -> bool:
    return _ai_state.should_skip_proactive


def should_skip_proactive_for_target(
    memory_scope_id: str,
    private_presence_store: Any = None,
) -> bool:
    """Check if proactive message should be skipped for a specific target user.

    Skipped when:
    - System is busy (is_generating or is_backend_busy)
    - Target user's private chat is ONLINE (they are actively chatting)
    """
    if _ai_state.should_skip_proactive:
        return True
    if private_presence_store is not None:
        from core.private_presence_store import PrivatePresence
        if private_presence_store.get(memory_scope_id) == PrivatePresence.ONLINE:
            return True
    return False


def get_ai_state_summary() -> Dict[str, Any]:
    return {
        "presence": _ai_state.presence.value,
        "is_generating": _ai_state.is_generating,
        "is_backend_busy": _ai_state.is_backend_busy,
        "should_skip_proactive": _ai_state.should_skip_proactive,
    }


def record_user_activity(chat_id: str):
    """记录用户活动（供 controller 调用）。"""
    _ai_state.record_user_activity(chat_id)


def get_user_idle_seconds(chat_id: str) -> float:
    """获取用户空闲时间（秒）。"""
    return _ai_state.get_user_idle_seconds(chat_id)


def record_ai_followup(chat_id: str):
    """记录 AI 追话。"""
    _ai_state.record_ai_followup(chat_id)


def get_followup_count(chat_id: str) -> int:
    """获取 AI 连续追话次数。"""
    return _ai_state.get_followup_count(chat_id)


def clear_conversation_tracking(chat_id: str):
    """清除对话追踪数据。"""
    _ai_state.clear_conversation_tracking(chat_id)


# ==================================================================
# ScheduledEvent 数据类
# ==================================================================

class ScheduledEvent:
    """一个计划中的触发事件。reason 为可选字段。"""

    def __init__(
        self,
        trigger_time: datetime,
        reason: str = "",
        event_type: str = "proactive",  # "proactive" | "alarm"
        message_kind: str = "autonomous",  # "autonomous" | "explicit"
        event_id: str = "",
        chat_id: str = "",
    ):
        self.trigger_time = trigger_time
        self.reason = reason
        self.event_type = event_type
        self.message_kind = message_kind
        self.event_id = event_id or f"{event_type}_{int(trigger_time.timestamp())}_{id(self)}"
        self.chat_id = chat_id
        self.fired = False

    def to_dict(self) -> dict:
        return {
            "trigger_time": self.trigger_time.isoformat(),
            "reason": self.reason,
            "event_type": self.event_type,
            "message_kind": self.message_kind,
            "event_id": self.event_id,
            "chat_id": self.chat_id,
            "fired": self.fired,
        }

    @classmethod
    def from_dict(cls, d: dict, tz: ZoneInfo) -> "ScheduledEvent":
        trigger_time = datetime.fromisoformat(d["trigger_time"])
        if trigger_time.tzinfo is None:
            trigger_time = trigger_time.replace(tzinfo=tz)
        ev = cls(
            trigger_time=trigger_time,
            reason=d.get("reason", ""),
            event_type=d.get("event_type", "alarm"),
            message_kind=d.get("message_kind", "explicit" if d.get("event_type", "alarm") == "alarm" else "autonomous"),
            event_id=d.get("event_id", ""),
            chat_id=d.get("chat_id", ""),
        )
        ev.fired = d.get("fired", False)
        return ev

    def __repr__(self):
        r = self.reason[:30] if self.reason else "(无缘由)"
        return f"<ScheduledEvent {self.event_type} at {self.trigger_time.strftime('%H:%M')} reason='{r}'>"


# ==================================================================
# ProactiveScheduler — 基于 APScheduler
# ==================================================================

class ProactiveScheduler:
    """
    主动消息调度器 (APScheduler 版)。

    - 启动时注册 CronTrigger (每天 00:00) 生成今日计划
    - 每日计划的每个触发点注册为独立的 DateTrigger Job
    - 动态闹钟同样使用 DateTrigger
    - 持久化闹钟到 cache/alarms.json
    """

    DAILY_PLAN_HOUR = 0
    DAILY_PLAN_MINUTE = 0

    def __init__(
        self,
        cache_dir: str,
        timezone_str: str = "Asia/Shanghai",
        generate_plan_callback: Optional[Callable[..., Awaitable[List[Dict[str, str]]]]] = None,
        send_proactive_callback: Optional[Callable[..., Awaitable[None]]] = None,
    daily_summary_callback: Optional[Callable[[date], Awaitable[None]]] = None,
    resolve_delivery_callback: Optional[Callable[[str], str]] = None,
    private_presence_store: Any = None,
    identity_resolver: Optional[Callable[[str], Any]] = None,
    generate_group_listen_plan_callback: Optional[Callable[[], Awaitable[List[Dict[str, Any]]]]] = None,
    activate_group_listen_callback: Optional[Callable[[Dict[str, Any]], Awaitable[str]]] = None,
    ):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.tz = ZoneInfo(timezone_str)
        self.timezone_str = timezone_str

        self._generate_plan_callback = generate_plan_callback
        self._send_proactive_callback = send_proactive_callback
        self._daily_summary_callback = daily_summary_callback
        # 投递目标解析：proactive 默认发到最近活跃私聊端，不可用回退传入 fallback。
        # 注入 None 时退化为恒等（直接用 fallback），保持可独立测试。
        self._resolve_delivery_callback = resolve_delivery_callback
        self._private_presence_store = private_presence_store
        self._identity_resolver = identity_resolver
        # 群聊定时 ONLINE 监听：生成当日计划 + 到点激活。两个回调都缺省时该子系统不启用。
        self._generate_group_listen_plan_callback = generate_group_listen_plan_callback
        self._activate_group_listen_callback = activate_group_listen_callback

        # 事件列表（用于查询/序列化，实际触发由 APScheduler Job 驱动）
        self._daily_events: List[ScheduledEvent] = []
        self._alarms: List[ScheduledEvent] = []
        self._last_plan_date: Optional[str] = None

        self.default_chat_id: str = ""

        # APScheduler 实例
        self._scheduler = AsyncIOScheduler(timezone=self.tz)

        # 加载持久化数据
        self._load_alarms()
        self._load_daily_plan()

    # ----------------------------------------------------------------
    # 公开方法
    # ----------------------------------------------------------------

    def start(self):
        """启动 APScheduler。"""
        if self._scheduler.running:
            return

        # 注册每日计划生成 CronTrigger (每天 00:00)
        self._scheduler.add_job(
            self._on_daily_plan_trigger,
            CronTrigger(hour=self.DAILY_PLAN_HOUR, minute=self.DAILY_PLAN_MINUTE, timezone=self.tz),
            id="daily_plan_cron",
            replace_existing=True,
            name="每日计划生成",
        )

        # 注册每日总结 CronTrigger (每天 00:00)
        self._scheduler.add_job(
            self._on_daily_summary_trigger,
            CronTrigger(hour=0, minute=0, timezone=self.tz),
            id="daily_summary_cron",
            replace_existing=True,
            name="每日对话总结",
        )

        # 群聊定时 ONLINE 监听：凌晨生成当日计划 + 每分钟 tick 检查是否到点
        if self._group_listen_enabled:
            self._scheduler.add_job(
                self._on_group_listen_plan_trigger,
                CronTrigger(hour=self.DAILY_PLAN_HOUR, minute=self.DAILY_PLAN_MINUTE, timezone=self.tz),
                id="group_listen_plan_cron",
                replace_existing=True,
                name="群聊监听计划生成",
            )
            self._scheduler.add_job(
                self._on_group_listen_tick,
                CronTrigger(minute="*", timezone=self.tz),
                id="group_listen_tick_cron",
                replace_existing=True,
                name="群聊监听到点检查",
            )

        # 恢复之前加载的未触发闹钟 → DateTrigger
        now = datetime.now(self.tz)
        for alarm in self._alarms:
            if not alarm.fired and alarm.trigger_time > now:
                self._add_event_job(alarm)

        # 恢复今日计划中未触发的事件
        for ev in self._daily_events:
            if not ev.fired and ev.trigger_time > now:
                self._add_event_job(ev)

        self._scheduler.start()
        logger.info("ProactiveScheduler 已启动 (APScheduler)")

        # 如果今天还没生成过计划且已经过了凌晨，立即异步触发一次
        today_str = now.strftime("%Y-%m-%d")
        if self._last_plan_date != today_str and not self._daily_events:
            async def _safe_initial_plan():
                try:
                    await self._on_daily_plan_trigger()
                except Exception as e:
                    logger.error(f"初始生成今日计划失败: {e}", exc_info=True)
            asyncio.ensure_future(_safe_initial_plan())

        # 群聊监听计划同理：进程在白天启动时补生成一次当日计划
        if self._group_listen_enabled:
            async def _safe_initial_group_listen_plan():
                try:
                    await self._on_group_listen_plan_trigger()
                except Exception as e:
                    logger.error(f"初始生成群聊监听计划失败: {e}", exc_info=True)
            asyncio.ensure_future(_safe_initial_group_listen_plan())

    def stop(self):
        """停止 APScheduler。"""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("ProactiveScheduler 已停止")

    ALARM_CONFLICT_WINDOW_MINUTES = 5

    def add_alarm(
        self,
        trigger_time_str: str,
        reason: str = "",
        chat_id: str = "",
        is_countdown: bool = False,
        countdown_minutes: int = 0,
        allow_conflict: bool = False,
    ) -> Dict[str, Any]:
        """
        添加动态闹钟。reason 为可选字段。

        Args:
            trigger_time_str: 触发时间，格式 "YYYY-MM-DD HH:MM" 或 "HH:MM"（当天）。
                              如果 is_countdown=True，此参数忽略。
            reason: 闹钟缘由（可选）
            chat_id: 触发时发送到的 chat_id
            is_countdown: 是否为倒计时模式
            countdown_minutes: 倒计时分钟数
            allow_conflict: 是否允许与既有闹钟时间在冲突窗口内（默认 False，冲突窗口=5 分钟）

        Returns:
            {"success": True/False, "alarm_id": ..., "trigger_time": ..., "message": ...}
        """
        now = datetime.now(self.tz)
        target_chat = chat_id or self.default_chat_id

        if is_countdown:
            if countdown_minutes <= 0:
                return {"success": False, "message": "倒计时分钟数必须大于 0"}
            trigger_dt = now + timedelta(minutes=countdown_minutes)
        else:
            try:
                trigger_dt = self._parse_time_str(trigger_time_str, now)
            except ValueError as e:
                return {"success": False, "message": f"时间格式错误: {e}"}

        if trigger_dt <= now:
            return {"success": False, "message": f"触发时间 {trigger_dt.strftime('%Y-%m-%d %H:%M')} 已过，请设置未来的时间"}

        # 冲突检测：同一 chat 近 ALARM_CONFLICT_WINDOW_MINUTES 内已有闹钟
        conflict_window = timedelta(minutes=self.ALARM_CONFLICT_WINDOW_MINUTES)
        now_active = [a for a in self._alarms if not a.fired]
        conflicts = [
            a for a in now_active
            if abs(a.trigger_time - trigger_dt) <= conflict_window and (not target_chat or a.chat_id == target_chat)
        ]
        if conflicts and not allow_conflict:
            conflict_list = ", ".join(sorted({a.trigger_time.strftime('%Y-%m-%d %H:%M') for a in conflicts}))
            return {
                "success": False,
                "message": (
                    f"在 {self.ALARM_CONFLICT_WINDOW_MINUTES} 分钟内已有闹钟：{conflict_list}。"
                    "若仍需设置，请重试并确认。"
                ),
            }

        alarm = ScheduledEvent(
            trigger_time=trigger_dt,
            reason=(reason or "").strip(),
            event_type="alarm",
            chat_id=target_chat,
        )
        self._alarms.append(alarm)
        self._save_alarms()

        # 注册 APScheduler DateTrigger Job
        self._add_event_job(alarm)

        trigger_str = trigger_dt.strftime("%Y-%m-%d %H:%M")
        reason_display = reason.strip() if reason and reason.strip() else "(无缘由)"
        logger.info(f"新增闹钟: {trigger_str} — {reason_display[:50]}")
        return {
            "success": True,
            "alarm_id": alarm.event_id,
            "trigger_time": trigger_str,
            "message": f"闹钟已设置: {trigger_str}" + (f" — {reason.strip()}" if reason and reason.strip() else ""),
        }

    def list_alarms(self) -> List[Dict[str, Any]]:
        """列出所有未触发的闹钟。"""
        now = datetime.now(self.tz)
        active = [a for a in self._alarms if not a.fired and a.trigger_time > now]
        return [a.to_dict() for a in sorted(active, key=lambda x: x.trigger_time)]

    def cancel_alarm(self, alarm_id: str) -> Dict[str, Any]:
        """取消一个闹钟。"""
        for i, alarm in enumerate(self._alarms):
            if alarm.event_id == alarm_id and not alarm.fired:
                self._alarms.pop(i)
                self._save_alarms()
                # 移除 APScheduler Job
                try:
                    self._scheduler.remove_job(alarm_id)
                except Exception:
                    pass  # Job 可能已不存在
                logger.info(f"闹钟已取消: {alarm_id}")
                return {"success": True, "message": f"闹钟 {alarm_id} 已取消"}
        return {"success": False, "message": f"未找到活跃闹钟: {alarm_id}"}

    def get_today_plan(self) -> List[Dict[str, Any]]:
        """获取今日触发计划。"""
        return [e.to_dict() for e in self._daily_events if not e.fired]

    async def regenerate_today_plan(self, clear_existing: bool = True) -> Dict[str, Any]:
        """
        手动重新生成今天的主动消息计划。

        Args:
            clear_existing: 是否先清空现有今日计划再重建。

        Returns:
            生成结果摘要。
        """
        return await self._on_daily_plan_trigger(force=True, clear_existing=clear_existing)

    # ----------------------------------------------------------------
    # 群聊定时 ONLINE 监听
    # ----------------------------------------------------------------

    #: 到点后仍允许补触发的宽限分钟数（覆盖 tick 抖动与短暂重启）
    GROUP_LISTEN_GRACE_MINUTES = 10

    @property
    def _group_listen_enabled(self) -> bool:
        return bool(
            self._generate_group_listen_plan_callback
            and self._activate_group_listen_callback
        )

    def get_group_listen_plan(self) -> Dict[str, Any]:
        """读取当前群聊监听计划（供工具/调试查看）。"""
        from core import group_listen_plan as glp
        return glp.read_plan()

    async def regenerate_group_listen_plan(self) -> Dict[str, Any]:
        """手动重新生成今日群聊监听计划。"""
        return await self._on_group_listen_plan_trigger(force=True)

    async def _on_group_listen_plan_trigger(self, force: bool = False) -> Dict[str, Any]:
        """CronTrigger 回调：凌晨依据 GROUP_LISTEN.json 的候选群池生成当日 entries。"""
        if not self._group_listen_enabled:
            return {"success": False, "message": "群聊监听调度未启用"}

        from core import group_listen_plan as glp

        now = datetime.now(self.tz)
        today_str = now.strftime("%Y-%m-%d")
        state = glp.read_plan()
        if not state.get("enabled", True):
            logger.info("群聊监听计划已被禁用 (enabled=false)，跳过生成")
            return {"success": True, "skipped": True, "message": "群聊监听计划已禁用", "count": 0}
        if not force and state.get("generated_date") == today_str:
            logger.debug("今日群聊监听计划已生成，跳过重复触发")
            return {
                "success": True,
                "skipped": True,
                "message": "今日群聊监听计划已生成",
                "count": len([e for e in state.get("entries") or [] if not e.get("fired")]),
            }
        if not glp.candidate_groups(state):
            logger.info("GROUP_LISTEN.json 没有候选群，跳过生成（Nora 可自行往 groups 里添加）")
            glp.save_generated_entries([], now)
            return {"success": True, "skipped": True, "message": "没有候选群", "count": 0}

        try:
            raw_entries = await self._generate_group_listen_plan_callback()
        except Exception as e:
            logger.error(f"生成群聊监听计划失败: {e}", exc_info=True)
            return {"success": False, "message": f"生成群聊监听计划失败: {e}"}

        state = glp.save_generated_entries(raw_entries or [], now)
        entries = state.get("entries") or []
        logger.info(f"今日群聊监听计划已生成: {len(entries)} 个触发点")
        for entry in entries:
            label = entry.get("name") or entry.get("group_id")
            logger.info(f"  • {entry['time']} — {entry['platform']}:{label}")
        return {
            "success": True,
            "skipped": False,
            "date": today_str,
            "count": len(entries),
            "message": f"今日群聊监听计划已生成: {len(entries)} 个触发点",
        }

    async def _on_group_listen_tick(self):
        """每分钟 tick：检查是否有群监听条目到点。一次最多激活一个群。"""
        if not self._group_listen_enabled:
            return

        from core import group_listen_plan as glp

        now = datetime.now(self.tz)
        try:
            index, entry = glp.due_entry(
                glp.read_plan(), now, grace_minutes=self.GROUP_LISTEN_GRACE_MINUTES
            )
        except Exception as e:
            logger.warning(f"读取群聊监听计划失败: {e}")
            return

        if entry is None:
            # 顺手把早已错过的条目收尾，避免它们永远待触发
            try:
                glp.expire_stale_entries(now, grace_minutes=self.GROUP_LISTEN_GRACE_MINUTES)
            except Exception as e:
                logger.debug(f"清理过期群聊监听条目失败: {e}")
            return

        runtime_key = glp.runtime_key_for(entry)
        if not runtime_key:
            glp.mark_entry(index, result="invalid_target")
            return

        # 系统忙碌时不抢占（与私聊 proactive 一致：跳过而非轮询重试）
        if should_skip_proactive():
            state_summary = get_ai_state_summary()
            logger.info(
                "跳过群监听触发 %s: generating=%s backend_busy=%s",
                runtime_key,
                state_summary["is_generating"],
                state_summary["is_backend_busy"],
            )
            glp.mark_entry(index, result="skipped_system_busy")
            return

        try:
            result = await self._activate_group_listen_callback(entry)
        except Exception as e:
            logger.error(f"激活群监听失败 ({runtime_key}): {e}", exc_info=True)
            result = f"error: {e}"

        glp.mark_entry(index, result=str(result or "unknown"))

    # ----------------------------------------------------------------
    # 每日总结回调
    # ----------------------------------------------------------------

    async def _on_daily_summary_trigger(self):
        """CronTrigger 回调：凌晨生成上一日对话总结文件。"""
        if not self._daily_summary_callback:
            logger.debug("未设置 daily_summary_callback，跳过每日总结")
            return

        now = datetime.now(self.tz)
        target_date = now.date() - timedelta(days=1)
        try:
            await self._daily_summary_callback(target_date)
            logger.info(f"每日总结完成: {target_date.isoformat()}")
        except Exception as e:
            logger.error(f"每日总结失败: {e}", exc_info=True)

    # ----------------------------------------------------------------
    # APScheduler 回调
    # ----------------------------------------------------------------

    async def _on_daily_plan_trigger(self, force: bool = False, clear_existing: bool = True) -> Dict[str, Any]:
        """CronTrigger 回调：凌晨生成今日计划。支持 force 强制重建。"""
        now = datetime.now(self.tz)
        today_str = now.strftime("%Y-%m-%d")
        if not force and self._last_plan_date == today_str:
            logger.debug("今日计划已生成，跳过重复触发")
            return {
                "success": True,
                "skipped": True,
                "message": "今日计划已生成，跳过重复触发",
                "count": len([e for e in self._daily_events if not e.fired]),
                "date": today_str,
            }

        logger.info("🗓️ 开始生成今日主动消息计划...")
        if not self._generate_plan_callback:
            logger.warning("未设置 generate_plan_callback，跳过计划生成")
            return {"success": False, "message": "未设置 generate_plan_callback"}

        if not self.default_chat_id:
            logger.warning("default_chat_id 未设置（尚未收到任何消息），跳过计划生成。用户发送第一条消息后将自动初始化。")
            return {"success": False, "message": "default_chat_id 未设置，等待用户首条消息"}

        try:
            plan_items = await self._generate_plan_callback(self.default_chat_id)

            if clear_existing:
                # 清除旧的 daily event Jobs
                for old_ev in self._daily_events:
                    try:
                        self._scheduler.remove_job(old_ev.event_id)
                    except Exception:
                        pass
                self._daily_events = []

            generated_count = 0
            for item in plan_items:
                time_str = item.get("time", "")
                reason = item.get("reason", "")  # reason 可选
                message_kind = str(item.get("message_kind", "autonomous") or "autonomous").strip().lower()
                if message_kind not in {"autonomous", "explicit"}:
                    message_kind = "autonomous"
                if not time_str:
                    continue
                try:
                    trigger_dt = self._parse_time_str(f"{today_str} {time_str}", now)
                    if trigger_dt > now:
                        event = ScheduledEvent(
                            trigger_time=trigger_dt,
                            reason=reason,
                            event_type="proactive",
                            message_kind=message_kind,
                            chat_id=self.default_chat_id,
                        )
                        self._daily_events.append(event)
                        self._add_event_job(event)
                        generated_count += 1
                except ValueError:
                    logger.warning(f"跳过无效时间: {time_str}")

            self._last_plan_date = today_str
            logger.info(f"今日计划已生成: {len(self._daily_events)} 个触发点")
            for ev in self._daily_events:
                r = ev.reason if ev.reason else "(无缘由)"
                logger.info(f"  • {ev.trigger_time.strftime('%H:%M')} — {r}")

            self._save_daily_plan()
            return {
                "success": True,
                "skipped": False,
                "date": today_str,
                "count": generated_count,
                "total_today": len([e for e in self._daily_events if not e.fired]),
                "message": f"今日计划已{'重建' if clear_existing else '追加'}: {generated_count} 个触发点",
            }

        except Exception as e:
            logger.error(f"生成今日计划失败: {e}", exc_info=True)
            return {"success": False, "message": f"生成今日计划失败: {e}"}

    async def _on_event_trigger(self, event_id: str):
        """DateTrigger 回调：到达触发时间。"""
        event = self._find_event(event_id)
        if not event:
            logger.warning(f"触发事件未找到: {event_id}")
            return
        if event.fired:
            logger.debug(f"事件已触发过，跳过: {event_id}")
            return

        # 判断是否跳过
        # 解析目标用户的 memory_scope_id，用于判断该用户是否正在私聊
        target_scope_id = ""
        if self._identity_resolver and event.chat_id:
            try:
                identity = self._identity_resolver(event.chat_id)
                if identity and hasattr(identity, "memory_scope_id"):
                    target_scope_id = identity.memory_scope_id
            except Exception:
                pass

        if should_skip_proactive_for_target(target_scope_id, self._private_presence_store):
            state = get_ai_state_summary()
            reason_short = (event.reason or "无")[:30]
            private_online = (
                self._private_presence_store is not None
                and target_scope_id
                and self._private_presence_store.get(target_scope_id).value == "online"
            )
            logger.info(
                f"跳过触发 {event.event_type} (reason: {reason_short}): "
                f"generating={state['is_generating']}, "
                f"backend_busy={state['is_backend_busy']}, "
                f"target_private_online={private_online}"
            )
            # 闹钟不受在线状态影响（闹钟必须触发）
            if event.event_type != "alarm":
                # 取消轮询式重试：在线/忙碌时直接跳过本次 proactive 事件
                event.fired = True
                self._persist_event_list(event)
                return

        event.fired = True
        reason_display = event.reason if event.reason else "(无缘由)"
        logger.info(f"触发 {event.event_type}: {reason_display[:50]} (chat={event.chat_id})")

        if self._send_proactive_callback:
            try:
                target_chat = self._resolve_delivery_target(event)
                if target_chat:
                    await self._send_proactive_callback(
                        target_chat,
                        event.reason,
                        event.event_type,
                        event.message_kind,
                    )
                else:
                    logger.warning(f"无法触发事件 {event_id}：缺少 chat_id 且 default_chat_id 未设置。"
                                   "请先向机器人发送一条消息以初始化 default_chat_id。")
            except Exception as e:
                logger.error(f"触发主动消息失败 ({event_id}): {e}", exc_info=True)
        else:
            logger.warning(f"事件 {event_id} 触发但 send_proactive_callback 未设置")

        self._persist_event_list(event)

    def _resolve_delivery_target(self, event: ScheduledEvent) -> str:
        """决定事件实际投递到哪个 runtime_key。

        - 闹钟（alarm）：用户/AI 显式设定了目标，原样投递，绝不重定向。
        - 主动消息（proactive）：默认投到最近活跃的私聊端，不可用则回退
          event.chat_id 或 default_chat_id（语义已改为 fallback）。
        """
        explicit_chat = (event.chat_id or self.default_chat_id or "").strip()
        if event.event_type == "alarm":
            return explicit_chat
        if self._resolve_delivery_callback is not None:
            try:
                resolved = (self._resolve_delivery_callback(explicit_chat) or "").strip()
                if resolved:
                    return resolved
            except Exception as e:
                logger.warning(f"解析最近活跃投递端失败，回退默认: {e}")
        return explicit_chat

    # ----------------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------------

    def _add_event_job(self, event: ScheduledEvent):
        """为一个 ScheduledEvent 注册 DateTrigger Job。"""
        try:
            self._scheduler.add_job(
                self._on_event_trigger,
                DateTrigger(run_date=event.trigger_time, timezone=self.tz),
                id=event.event_id,
                replace_existing=True,
                args=[event.event_id],
                name=f"{event.event_type}: {(event.reason or '无缘由')[:40]}",
            )
        except Exception as e:
            logger.warning(f"注册 Job 失败 ({event.event_id}): {e}")

    def _find_event(self, event_id: str) -> Optional[ScheduledEvent]:
        """在 daily_events 和 alarms 中查找事件。"""
        for ev in self._daily_events:
            if ev.event_id == event_id:
                return ev
        for ev in self._alarms:
            if ev.event_id == event_id:
                return ev
        return None

    def _persist_event_list(self, event: ScheduledEvent):
        """根据事件类型持久化对应列表。"""
        if event.event_type == "alarm":
            self._save_alarms()
        else:
            self._save_daily_plan()

    # ----------------------------------------------------------------
    # 持久化
    # ----------------------------------------------------------------

    @property
    def _alarms_file(self) -> str:
        return os.path.join(self.cache_dir, "alarms.json")

    @property
    def _daily_plan_file(self) -> str:
        return os.path.join(self.cache_dir, "daily_plan.json")

    def _save_alarms(self):
        try:
            data = {
                "updated_at": datetime.now(self.tz).isoformat(),
                "alarms": [a.to_dict() for a in self._alarms],
            }
            with open(self._alarms_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"闹钟已保存: {len(self._alarms)} 条")
        except Exception as e:
            logger.error(f"保存闹钟失败: {e}")

    def _load_alarms(self):
        try:
            if os.path.exists(self._alarms_file):
                with open(self._alarms_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._alarms = [
                    ScheduledEvent.from_dict(a, self.tz)
                    for a in data.get("alarms", [])
                ]
                now = datetime.now(self.tz)
                self._alarms = [
                    a for a in self._alarms
                    if not a.fired or a.trigger_time > now
                ]
                logger.info(f"已加载 {len(self._alarms)} 个持久化闹钟")
        except Exception as e:
            logger.warning(f"加载闹钟失败: {e}")
            self._alarms = []

    def _save_daily_plan(self):
        try:
            data = {
                "date": datetime.now(self.tz).strftime("%Y-%m-%d"),
                "last_plan_date": self._last_plan_date,
                "events": [e.to_dict() for e in self._daily_events],
            }
            with open(self._daily_plan_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存今日计划失败: {e}")

    def _load_daily_plan(self):
        try:
            if os.path.exists(self._daily_plan_file):
                with open(self._daily_plan_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                today_str = datetime.now(self.tz).strftime("%Y-%m-%d")
                if data.get("date") == today_str:
                    self._daily_events = [
                        ScheduledEvent.from_dict(e, self.tz)
                        for e in data.get("events", [])
                    ]
                    self._last_plan_date = data.get("last_plan_date")
                    logger.info(f"已加载今日计划: {len(self._daily_events)} 个事件")
                else:
                    logger.info("缓存的计划非今日，将在凌晨重新生成")
        except Exception as e:
            logger.warning(f"加载今日计划失败: {e}")

    # ----------------------------------------------------------------
    # 工具函数
    # ----------------------------------------------------------------

    def _parse_time_str(self, time_str: str, now: datetime) -> datetime:
        """
        解析时间字符串。
        支持: "HH:MM", "YYYY-MM-DD HH:MM", "MM-DD HH:MM"
        """
        time_str = time_str.strip()

        for fmt in ["%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"]:
            try:
                dt = datetime.strptime(time_str, fmt)
                return dt.replace(tzinfo=self.tz)
            except ValueError:
                pass

        for fmt in ["%m-%d %H:%M", "%m/%d %H:%M"]:
            try:
                dt = datetime.strptime(time_str, fmt)
                return dt.replace(year=now.year, tzinfo=self.tz)
            except ValueError:
                pass

        try:
            dt = datetime.strptime(time_str, "%H:%M")
            return now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
        except ValueError:
            pass

        raise ValueError(f"无法解析时间: '{time_str}'，支持格式: HH:MM, YYYY-MM-DD HH:MM, MM-DD HH:MM")
