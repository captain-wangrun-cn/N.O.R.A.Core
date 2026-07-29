'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:43:41
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
N.O.R.A. 核心控制器 — 瘦身版。

业务逻辑已拆分到各 Mixin 模块中：
  - message_handler.py  → MessageHandlerMixin   (消息/命令路由)
  - interrupt_handler.py→ InterruptHandlerMixin  (忙碌时意图检测+打断)
  - front_brain.py      → FrontBrainMixin        (前脑即时回复+审查)
  - back_brain.py       → BackBrainMixin         (后脑工具循环+文本清洗)
  - polling.py          → PollingMixin           (前后脑轮询循环)
  - scheduler_mixin.py  → SchedulerMixin         (调度器+对话延续+主动消息)

本文件只保留：
  - NoraController 类定义 + __init__
  - 类级正则常量
  - _send_debug / _was_interrupted / _reload_llm_clients
  - 异步保存辅助方法
"""

import asyncio
from datetime import datetime
import inspect
import logging
import os
import platform as py_platform
import re
import sys
from typing import Dict, Any, Optional, Callable

from adapters.base import BaseAdapter
from brain.llm import llm_client, get_llm_client
from brain.prompts import get_lexicon_global_system_prompt_block
from brain.tools import ToolManager
from memory.rag import RAGEngine
from memory.image_store import ImageStore
from memory.message_history import MessageHistory
from skills.loader import SkillLoader
from core.cost_tracker import get_cost_tracker
from core.worker_status import WorkerStatus, BackendTaskQueue
from core.scheduler import ProactiveScheduler
from core.default_chat_id_store import load_default_chat_id, load_default_chat_target
from core.delivery_target_store import resolve_delivery_runtime_key, resolve_route_target
from core.conversation_identity import (
    ConversationIdentity,
    build_conversation_identity,
    build_identity_from_target,
    build_identity_from_parts,
    conversation_target_dict,
    resolve_platform,
    runtime_key_for,
)
from core.group_listener import GroupListenerManager
from core.group_presence_store import GroupPresenceStore
from core.private_presence_store import PrivatePresence, PrivatePresenceStore
from core.routing import sanitize_adapter_output_text, sanitize_user_visible_text, parse_route_markers
from adapters.message_controls import parse_message_controls, strip_message_control_markers
from triggers import TriggerManager
from triggers.factory import build_triggers
import config

# Mixin 导入
from core.message_handler import MessageHandlerMixin
from core.interrupt_handler import InterruptHandlerMixin
from core.front_brain import FrontBrainMixin
from core.back_brain import BackBrainMixin
from core.polling import PollingMixin
from core.scheduler_mixin import SchedulerMixin

logger = logging.getLogger(__name__)


class NoraController(
    MessageHandlerMixin,
    InterruptHandlerMixin,
    FrontBrainMixin,
    BackBrainMixin,
    PollingMixin,
    SchedulerMixin,
):
    """处理机器人的核心业务逻辑。"""

    # ------------------------------------------------------------------
    # 类级正则常量
    # ------------------------------------------------------------------
    _SPLIT_MARKER_PATTERN = re.compile(r"\[SPLIT(?::([0-9.]+))?\]", re.IGNORECASE)
    _THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
    _THINK_INLINE_PATTERN = re.compile(r"</?think>", re.IGNORECASE)
    _IMAGE_TAGS_PATTERN = re.compile(
        r'\[IMAGE_TAGS:([^\]\s]+)\](.*?)\[/IMAGE_TAGS\]',
        re.IGNORECASE | re.DOTALL,
    )
    _IMAGE_OCR_PATTERN = re.compile(
        r'\[IMAGE_OCR:([^\]\s]+)\](.*?)\[/IMAGE_OCR\]',
        re.IGNORECASE | re.DOTALL,
    )
    _IMAGE_DESC_PATTERN = re.compile(
        r'\[IMAGE_DESC:([^\]\s]+)\](.*?)\[/IMAGE_DESC\]',
        re.IGNORECASE | re.DOTALL,
    )
    _VIDEO_TAGS_PATTERN = re.compile(
        r'\[VIDEO_TAGS:([^\]\s]+)\](.*?)\[/VIDEO_TAGS\]',
        re.IGNORECASE | re.DOTALL,
    )
    _VIDEO_DESC_PATTERN = re.compile(
        r'\[VIDEO_DESC:([^\]\s]+)\](.*?)\[/VIDEO_DESC\]',
        re.IGNORECASE | re.DOTALL,
    )
    _SYSTEM_ENV_MARKER = "【系统环境信息 (System Environment)】"
    _LEXICON_GLOBAL_MARKER = "【词库全局说明 (Lexicon Global Prompt)】"

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def __init__(self, adapter, tui_callback: Optional[Callable[[str], None]] = None):
        # adapter 可为单个 BaseAdapter，或 list/tuple/dict（多适配器并发在线）。
        if isinstance(adapter, dict):
            adapter_list = [a for a in adapter.values() if a is not None]
        elif isinstance(adapter, (list, tuple)):
            adapter_list = [a for a in adapter if a is not None]
        else:
            adapter_list = [adapter]
        if not adapter_list:
            raise ValueError("NoraController 需要至少一个适配器")
        # 平台注册表：platform_name -> adapter。同名后者覆盖前者。
        self.adapters: Dict[str, BaseAdapter] = {}
        for a in adapter_list:
            self.adapters[getattr(a, "platform_name", "unknown")] = a
        # primary（首个）保留兼容旧的 self.adapter 单适配器引用。
        self.adapter = adapter_list[0]
        self.tui_callback = tui_callback
        # 注入主人识别回调，让 conversation_identity 能判断 is_owner（自动绑定+config 预声明）。
        try:
            from core.owner_registry import resolve_is_owner as _resolve_owner
            from core.conversation_identity import set_owner_resolver
            set_owner_resolver(_resolve_owner)
        except Exception as e:
            logger.warning(f"主人识别回调注入失败，将默认无主人: {e}")
        self.llm = llm_client
        self.rag = RAGEngine()
        self.image_store = ImageStore()
        self.skill_loader = SkillLoader()
        self.sessions: Dict[str, Any] = {}
        self.generation_tasks: Dict[str, asyncio.Task] = {}
        self.conversation_identities: Dict[str, ConversationIdentity] = {}
        
        # 前后端分离：每个 chat_id 一个 WorkerStatus
        self.worker_status: Dict[str, WorkerStatus] = {}
        # 后脑任务队列：后端忙碌时暂存用户新消息（per-chat）
        self.task_queues: Dict[str, BackendTaskQueue] = {}
        # Debug 模式：per-chat 开关
        self.debug_mode: Dict[str, bool] = {}
        
        # 对话延续定时器：per-chat asyncio.Task
        self._followup_timers: Dict[str, asyncio.Task] = {}
        # 对话延迟覆盖：per-chat 首次延迟（例如 NEED_FOLLOW 提示）
        self._followup_delay_override: Dict[str, float] = {}
        # 当前消息段标记为 KEEP_SEGMENT_OPEN 时暂停 followup 定时器
        self._followup_suspended_until_idle: Dict[str, bool] = {}
        # 前脑生成任务：per-chat asyncio.Task；新消息到来时可被打断
        self.front_brain_tasks: Dict[str, asyncio.Task] = {}
        # 前脑流式生成的"实时缓冲"：per-chat 字符串，被打断时作为草稿带入下轮
        self.front_brain_partial: Dict[str, str] = {}
        # 后脑流式生成的"实时缓冲"：per-chat 字符串，被新图片打断时作为草稿带入合并重启
        self.back_brain_partial: Dict[str, str] = {}
        # 后脑当前任务的输入快照：per-chat dict，包含原始 text + multimodal_images，
        # 用于"被新图片打断时把旧图与新图合并重启"
        self.back_brain_input_context: Dict[str, Dict[str, Any]] = {}

        # 是否启用非流式输出（per-chat，可被命令切换）
        interaction_cfg = (config.get_config() or {}).get("interaction", {})
        self.default_non_stream = interaction_cfg.get("non_stream", False)
        self.non_stream_flags: Dict[str, bool] = {}
        
        # 对话延续配置
        self.FOLLOWUP_INITIAL_DELAY = 120    # 2 分钟
        self.FOLLOWUP_RECHECK_INTERVAL = 60  # 1 分钟
        self.FOLLOWUP_MAX_COUNT = 3          # 最多追话 3 次
        self.FOLLOWUP_NEED_FOLLOW_DELAY = 15 # NEED_FOLLOW 时加快首次跟进
        # WAIT 分支防抖与超时：最多连续等待次数 & 总空闲超时后收尾
        self.FOLLOWUP_MAX_WAIT_LOOPS = 5     # WAIT 最多轮询 5 次（约 5 分钟）
        self.FOLLOWUP_END_IDLE_SECONDS = 900 # 空闲 ≥15 分钟则收尾
        self.FOLLOWUP_END_GRACE_SECONDS = 5  # END 收尾消息发送后缓冲，避免错过用户新消息
        
        # 快速模型
        try:
            self.fast_llm = get_llm_client(model_alias="fast")
        except Exception:
            self.fast_llm = self.llm

        # 后脑模型
        try:
            self.coder_llm = get_llm_client(model_alias="coder")
        except Exception:
            self.coder_llm = self.llm

        # 图片输入专用模型
        try:
            self.image_llm = get_llm_client(model_alias="image")
        except Exception:
            self.image_llm = self.llm

        # 视频输入专用模型（可选，None 表示未配置）
        try:
            self.video_llm = get_llm_client(model_alias="video")
        except Exception:
            self.video_llm = None

        # 表情包快速描述模型（可选，None 表示未配置）
        try:
            self.fast_image_llm = get_llm_client(model_alias="fast-image")
        except Exception:
            self.fast_image_llm = None

        # 消息历史管理
        history_cfg = config.get_message_history_config()
        db_path = history_cfg.get("db_path")
        self.message_history = MessageHistory(
            db_path=db_path,
            raw_window=history_cfg.get("raw_window", 50),
            compress_window=history_cfg.get("compress_window", 50),
            compress_ratio=history_cfg.get("compress_ratio", 10),
            archive_threshold=history_cfg.get("archive_threshold", 500),
            timezone=history_cfg.get("timezone", "Asia/Shanghai"),
            timestamp_format=history_cfg.get("timestamp_format", "%Y-%m-%d %H:%M:%S %A"),
            mirror_db_path=history_cfg.get("mirror_db_path"),
            context_db_path=history_cfg.get("context_db_path"),
            long_message_threshold=history_cfg.get("long_message_threshold", 1200),
        )

        group_listener_cfg = config.get_group_listener_config()
        self.group_presence_store = GroupPresenceStore()
        expired_groups = self.group_presence_store.expire_stale_entries()
        if expired_groups:
            logger.warning(
                "检测到 %d 个长时间未更新的 ONLINE 群记录，已重置为 SEMI_ONLINE",
                len(expired_groups),
            )
        demoted_groups = self.group_presence_store.normalize_single_online()
        if demoted_groups:
            logger.warning(
                "检测到遗留的多个 ONLINE 群，已保留最近活跃群并降级其余 %d 个群",
                len(demoted_groups),
            )
        self.private_presence = PrivatePresenceStore()
        self.private_presence.expire_stale_entries()
        self.group_listener = GroupListenerManager(
            store=self.group_presence_store,
            persist_message=self._persist_online_group_message,
            decide_passive=self._decide_group_passive_action,
            decide_append=self._decide_group_append_action,
            promote=self._promote_group_message_batch,
            interrupt=self._interrupt_group_generation,
            batch_size=group_listener_cfg["evaluation_batch_size"],
            idle_seconds=group_listener_cfg["idle_seconds"],
            max_pending=group_listener_cfg["max_pending_messages"],
            enabled=group_listener_cfg["enabled"],
            watchdog_interval_seconds=group_listener_cfg["watchdog_interval_seconds"],
            no_interaction_timeout_seconds=group_listener_cfg["no_interaction_timeout_seconds"],
            idle_exit_seconds=group_listener_cfg["idle_exit_seconds"],
            max_online_seconds=group_listener_cfg["max_online_seconds"],
            directed_veto_seconds=group_listener_cfg["directed_veto_seconds"],
            semi_online_vote_threshold=group_listener_cfg["semi_online_vote_threshold"],
        )
        self.group_listener_decision_timeout = group_listener_cfg["decision_timeout_seconds"]
        
        # 成本跟踪
        cost_cfg = (config.get_config() or {}).get("cost_tracking", {})
        self.cost_tracking_enabled = cost_cfg.get("enabled", True)
        if self.cost_tracking_enabled:
            custom_prices = cost_cfg.get("custom_prices", {})
            self.cost_tracker = get_cost_tracker(custom_prices=custom_prices)
            logger.info("成本跟踪已启用")
        else:
            self.cost_tracker = None
            logger.info("成本跟踪已禁用")
        
        # --- 主动消息调度器 ---
        from workspace_config import get_workspace_manager
        workspace_mgr = get_workspace_manager()
        schedule_cfg = (config.get_config() or {}).get("schedule", {})
        schedule_enabled = schedule_cfg.get("enabled", True)
        scheduler_tz = history_cfg.get("timezone", "Asia/Shanghai")
        persisted_default_target = load_default_chat_target()
        persisted_identity = (
            self._identity_from_target(persisted_default_target)
            if persisted_default_target
            else None
        )
        persisted_default_chat_id = persisted_identity.runtime_key if persisted_identity else ""
        persisted_default_target = conversation_target_dict(persisted_identity) if persisted_identity else {}
        
        if schedule_enabled:
            self.scheduler = ProactiveScheduler(
                cache_dir=workspace_mgr.cache_dir,
                timezone_str=scheduler_tz,
                generate_plan_callback=self._generate_daily_plan_via_llm,
                send_proactive_callback=self._send_proactive_message,
                daily_summary_callback=self._generate_daily_summary,
                resolve_delivery_callback=resolve_delivery_runtime_key,
                private_presence_store=self.private_presence,
                identity_resolver=self._identity_for_runtime_key,
            )
            if persisted_default_chat_id:
                self.scheduler.default_chat_id = persisted_default_chat_id
            logger.info("主动消息调度器已初始化")
        else:
            self.scheduler = None
            logger.info("主动消息调度器已禁用")

        trigger_cfg = config.get_triggers_config()
        trigger_enabled = trigger_cfg.get("enabled", False)
        trigger_cfg_default_chat_id = str(trigger_cfg.get("default_chat_id", "") or "").strip()
        trigger_default_target = (
            self._target_from_chat_id(trigger_cfg_default_chat_id)
            if trigger_cfg_default_chat_id
            else persisted_default_target
        )
        trigger_default_chat_id = (
            str(trigger_default_target.get("runtime_key") or "").strip()
            or trigger_cfg_default_chat_id
            or persisted_default_chat_id
        )
        if trigger_enabled:
            self.trigger_manager = TriggerManager(
                notify_callback=self._handle_trigger_notification,
                default_chat_id=trigger_default_chat_id,
                default_chat_target=trigger_default_target,
                resolve_delivery_callback=resolve_delivery_runtime_key,
            )
            for trigger in build_triggers(
                trigger_cfg,
                default_chat_id=trigger_default_chat_id,
                default_chat_target=trigger_default_target,
            ):
                self.trigger_manager.register(trigger)
            logger.info("Trigger 系统已初始化")
        else:
            self.trigger_manager = None
            logger.info("Trigger 系统已禁用")
        
        # ToolManager 暂仍按 primary 适配器注册平台工具；不能传入原始 adapter
        # 参数，因为多适配器模式下它可能是 list/dict。
        self.tool_manager = ToolManager(self.adapter, image_store=self.image_store, scheduler=self.scheduler)
        self.message_history.start_retry_worker()
        
        logger.info(
            f"NoraController 已初始化。RAG: {'Online' if self.rag.enabled else 'Offline'}, "
            f"MessageHistory: Online, Scheduler: {'Online' if self.scheduler else 'Offline'}, "
            f"Triggers: {'Online' if self.trigger_manager else 'Offline'}"
        )

    def _platform_name(self) -> str:
        return resolve_platform(adapter=self.adapter)

    def _identity(self, context: Dict[str, Any]) -> ConversationIdentity:
        identity = build_conversation_identity(context, adapter=self.adapter)
        self._remember_identity(identity)
        return identity

    def _identity_from_parts(
        self,
        chat_id: str,
        user_id: Optional[str] = None,
        chat_type: str = "private",
        platform: Optional[str] = None,
    ) -> ConversationIdentity:
        identity = build_identity_from_parts(
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
            platform=platform or self._platform_name(),
        )
        self._remember_identity(identity)
        return identity

    def _identity_from_target(self, target: Dict[str, Any]) -> ConversationIdentity:
        identity = build_identity_from_target(target, adapter=self.adapter)
        self._remember_identity(identity)
        return identity

    def _target_from_chat_id(self, chat_id: str) -> Dict[str, Any]:
        if not chat_id:
            return {}
        identity = build_identity_from_target(
            {"runtime_key": chat_id, "chat_type": "private"},
            adapter=self.adapter,
        )
        self._remember_identity(identity)
        return conversation_target_dict(identity)

    def _runtime_key(self, chat_id: str, platform: Optional[str] = None) -> str:
        return runtime_key_for(platform or self._platform_name(), str(chat_id))

    def _remember_identity(self, identity: ConversationIdentity) -> ConversationIdentity:
        self.conversation_identities[identity.runtime_key] = identity
        return identity

    def _identity_for_runtime_key(self, runtime_key: str) -> ConversationIdentity:
        key = str(runtime_key)
        identity = self.conversation_identities.get(key)
        if identity:
            return identity
        for known in self.conversation_identities.values():
            if known.platform_chat_id == key:
                return known
        # 群在线状态表里只会出现群 runtime_key。重启后身份缓存为空时，
        # 用它兜底判断 chat_type，避免把群 key 当私聊登记进缓存。
        fallback_chat_type = "group" if self._is_known_group_runtime_key(key) else "private"
        if ":" in key:
            platform, chat_id = key.split(":", 1)
            return self._identity_from_parts(
                chat_id=chat_id,
                user_id=chat_id,
                chat_type=fallback_chat_type,
                platform=platform,
            )
        return self._identity_from_parts(
            chat_id=key,
            user_id=key,
            chat_type=fallback_chat_type,
        )

    def _is_known_group_runtime_key(self, runtime_key: str) -> bool:
        store = getattr(self, "group_presence_store", None)
        if store is None:
            return False
        try:
            return str(runtime_key) in (store.all() or {})
        except Exception:
            return False

    def _target_for_key(self, chat_or_runtime_key: str) -> Dict[str, Any]:
        return conversation_target_dict(self._identity_for_runtime_key(str(chat_or_runtime_key)))

    def _platform_chat_id(self, chat_or_runtime_key: str) -> str:
        return self._identity_for_runtime_key(str(chat_or_runtime_key)).platform_chat_id

    def _storage_id_for_key(self, chat_or_runtime_key: str) -> str:
        return self._identity_for_runtime_key(str(chat_or_runtime_key)).storage_id

    def _platform_for_key(self, chat_or_runtime_key: str) -> str:
        return self._identity_for_runtime_key(str(chat_or_runtime_key)).platform

    def _adapter_for_key(self, chat_or_runtime_key: str) -> BaseAdapter:
        """按 runtime_key 的平台前缀选适配器；未知平台拒绝投递。"""
        platform = self._platform_for_key(chat_or_runtime_key)
        adapter = self.adapters.get(platform)
        if adapter is None:
            raise ValueError(f"未注册目标平台适配器: {platform or 'unknown'}")
        return adapter

    def _chat_type_lookup(self, platform: str, platform_chat_id: str) -> str:
        """给 resolve_route_target 用：从身份缓存查某会话的 chat_type。"""
        runtime_key = f"{platform}:{platform_chat_id}"
        identity = self.conversation_identities.get(runtime_key)
        if identity:
            return identity.chat_type
        return ""

    def resolve_send_target(
        self,
        route: Optional[Dict[str, Any]],
        source_identity: Optional[ConversationIdentity] = None,
        *,
        route_source: str = "ai_output",
    ) -> Dict[str, Any]:
        """把 Nora 输出的路由标记解析为投递目标，并登记目标身份。

        返回：{"runtime_key", "platform", "platform_chat_id", "chat_type",
               "redirected", "identity"}。
        无标记/解析失败时 runtime_key 为源会话，redirected=False。
        """
        current_target = conversation_target_dict(source_identity) if source_identity else None
        route = route or {}
        requested_platform = str(route.get("platform") or "").strip()
        requested_scene_id = str(route.get("scene_id") or "").strip()
        requested_scene_type = str(route.get("scene_type") or "").strip()
        source_key = str((current_target or {}).get("runtime_key") or "").strip()

        if requested_platform and requested_platform not in self.adapters:
            resolved = {
                "runtime_key": "",
                "platform": requested_platform,
                "platform_chat_id": requested_scene_id,
                "chat_type": requested_scene_type,
                "redirected": False,
                "policy": "rejected_unknown_platform",
                "identity": None,
            }
            self._log_route_resolution(route_source, route, source_key, resolved)
            return resolved

        resolved = resolve_route_target(
            route=route,
            current_target=current_target,
            chat_type_lookup=self._chat_type_lookup,
        )
        runtime_key = str(resolved.get("runtime_key") or "").strip()
        if not runtime_key:
            resolved["identity"] = None
            self._log_route_resolution(route_source, route, source_key, resolved)
            return resolved
        platform = str(resolved.get("platform") or "").strip()
        if platform not in self.adapters:
            resolved.update({
                "runtime_key": "",
                "redirected": False,
                "policy": "rejected_unknown_platform",
                "identity": None,
            })
            self._log_route_resolution(route_source, route, source_key, resolved)
            return resolved
        # 已知会话直接复用；显式 typed 场景可按目标建立身份。
        identity = self.conversation_identities.get(runtime_key)
        if not identity:
            identity = build_identity_from_target({
                "platform": resolved.get("platform"),
                "platform_chat_id": resolved.get("platform_chat_id"),
                "chat_id": resolved.get("platform_chat_id"),
                "chat_type": resolved.get("chat_type"),
                "runtime_key": runtime_key,
            })
            self._remember_identity(identity)
        resolved["identity"] = identity
        self._log_route_resolution(route_source, route, source_key, resolved)
        return resolved

    def _log_route_resolution(
        self,
        route_source: str,
        route: Dict[str, Any],
        source_key: str,
        resolved: Dict[str, Any],
    ) -> None:
        """记录 AI 平台/场景标记及最终投递决策，不打印完整模型文本。"""
        platform = str(route.get("platform") or "").strip()
        scene_id = str(route.get("scene_id") or "").strip()
        scene_type = str(route.get("scene_type") or "").strip()
        final_key = str(resolved.get("runtime_key") or "").strip()
        logger.info(
            "[%s] 投递路由标记: source=%s, platform=%s, scene=%s, requested_platform=%s, "
            "requested_scene_type=%s, requested_scene_id=%s, source_target=%s, final_target=%s, "
            "redirected=%s, policy=%s",
            source_key or "unknown",
            route_source,
            platform or "未识别",
            (f"{scene_type + ':' if scene_type else ''}{scene_id}" if scene_id else "未识别"),
            platform or "未指定",
            scene_type or "未指定",
            scene_id or "未指定",
            source_key or "未指定",
            final_key or "拒绝投递",
            bool(resolved.get("redirected")),
            resolved.get("policy") or "default_current",
        )

    def resolve_route_state(
        self,
        text: str,
        route_state: Dict[str, Any],
        source_identity: Optional[ConversationIdentity] = None,
    ) -> Dict[str, Any]:
        """流式发送前缓冲路由前缀，首次实际正文发送后锁定目标。

        ``route_state["send_text"]`` 是本次可实际发送的文本；空串表示路由前缀
        尚不完整，应继续等待后续 chunk。正文开始后再出现的路由标记只会清理，
        不再改变已锁定目标。
        """
        incoming = str(text or "")
        route_state["send_text"] = incoming
        if route_state.get("resolved"):
            late_route, cleaned = parse_route_markers(incoming)
            if late_route.get("platform") or late_route.get("scene_id"):
                self._log_route_resolution(
                    "back_brain_stream",
                    late_route,
                    str((source_identity or route_state.get("identity")).runtime_key if (source_identity or route_state.get("identity")) else ""),
                    {
                        "runtime_key": route_state.get("key") or "",
                        "redirected": bool(route_state.get("redirected")),
                        "policy": "rejected_late_route",
                    },
                )
                route_state["send_text"] = cleaned
            return route_state

        buffered = str(route_state.get("prefix_buffer") or "") + incoming
        route_state["prefix_buffer"] = buffered
        stripped = buffered.lstrip()
        lower = stripped.lower()
        partial_prefixes = ("[platform", "[scene")
        looks_like_partial_route = any(
            token.startswith(lower) for token in partial_prefixes
        ) or any(lower.startswith(token) and "]" not in lower for token in partial_prefixes)
        route, cleaned = parse_route_markers(buffered)
        has_route = bool(route.get("platform") or route.get("scene_id"))
        route_starts_output = lower.startswith(partial_prefixes)
        residual = cleaned.lstrip().lower()
        residual_partial_route = any(
            residual.startswith(token) and "]" not in residual
            for token in partial_prefixes
        )

        # 仅收到完整/不完整路由标记而尚无正文时继续等待，以便两个标记跨 chunk。
        if looks_like_partial_route or residual_partial_route or (has_route and not cleaned.strip()):
            route_state["send_text"] = ""
            return route_state

        route_state["resolved"] = True
        route_state["prefix_buffer"] = ""
        if has_route and not route_starts_output:
            self._log_route_resolution(
                "back_brain_stream",
                route,
                str((source_identity or route_state.get("identity")).runtime_key if (source_identity or route_state.get("identity")) else ""),
                {
                    "runtime_key": route_state.get("key") or "",
                    "redirected": bool(route_state.get("redirected")),
                    "policy": "rejected_late_route",
                },
            )
            route_state["send_text"] = cleaned
            return route_state

        route_state["send_text"] = cleaned if has_route else buffered
        target = self.resolve_send_target(
            route,
            source_identity,
            route_source="back_brain_stream",
        )
        key = str(target.get("runtime_key") or "").strip()
        if not key:
            route_state["send_text"] = ""
            route_state["rejected"] = True
            return route_state
        route_state["key"] = key
        route_state["chat_type"] = target.get("chat_type") or route_state.get("chat_type", "")
        route_state["redirected"] = bool(target.get("redirected"))
        route_state["identity"] = target.get("identity")
        return route_state

    def _scope_key_for_identity(self, identity: ConversationIdentity) -> str:
        """Runtime queue key for one shared memory scope."""
        return identity.memory_scope_id or identity.runtime_key

    def _scope_key_for_runtime_key(self, runtime_key: str) -> str:
        return self._scope_key_for_identity(self._identity_for_runtime_key(runtime_key))

    def _get_scope_queue_for_runtime(self, runtime_key: str, create: bool = False) -> Optional[BackendTaskQueue]:
        key = self._scope_key_for_runtime_key(runtime_key)
        if create:
            return self.task_queues.setdefault(key, BackendTaskQueue())
        return self.task_queues.get(key)

    def get_or_create_scope_queue(self, memory_scope_id: str) -> BackendTaskQueue:
        """Return the backend task queue shared by a memory scope."""
        return self.task_queues.setdefault(memory_scope_id, BackendTaskQueue())

    def _runtime_keys_for_scope(self, memory_scope_id: str) -> list[str]:
        keys = [
            key for key, identity in self.conversation_identities.items()
            if identity.memory_scope_id == memory_scope_id
        ]
        return sorted(set(keys))

    def _has_pending_task(self, runtime_key: str) -> bool:
        task = self.generation_tasks.get(runtime_key)
        return bool(task and not task.done())

    def _has_pending_front_task(self, runtime_key: str) -> bool:
        task = self.front_brain_tasks.get(runtime_key)
        return bool(task and not task.done())

    def get_busy_backend_runtime(self, memory_scope_id: str) -> Optional[str]:
        """Find the runtime currently occupying the shared backend attention."""
        for key in self._runtime_keys_for_scope(memory_scope_id):
            status = self.worker_status.get(key)
            if status and status.busy:
                return key
            if self._has_pending_task(key):
                return key
        return None

    def get_active_runtime_keys(self, memory_scope_id: str) -> list[str]:
        """Return runtimes that still keep the shared conversation segment open."""
        active: list[str] = []
        for key in self._runtime_keys_for_scope(memory_scope_id):
            if self._has_pending_front_task(key) or self._has_pending_task(key):
                active.append(key)
                continue
            status = self.worker_status.get(key)
            if status and status.busy:
                active.append(key)
                continue
            queue = self._get_scope_queue_for_runtime(key)
            if queue and queue.size() > 0:
                active.append(key)
                continue
            follow_task = self._followup_timers.get(key)
            if follow_task and not follow_task.done():
                active.append(key)
                continue
            if key in self._followup_suspended_until_idle:
                active.append(key)
                continue
            try:
                from core.scheduler import get_user_idle_seconds
                idle_secs = get_user_idle_seconds(key)
            except Exception:
                idle_secs = -1
            if idle_secs >= 0 and idle_secs < self.FOLLOWUP_END_IDLE_SECONDS:
                active.append(key)
        return sorted(set(active))

    def is_memory_scope_idle(self, memory_scope_id: str) -> bool:
        return not self.get_active_runtime_keys(memory_scope_id)

    def _sync_global_backend_flags(self):
        """Keep global scheduler flags true while any runtime backend is still active."""
        try:
            from core.scheduler import set_ai_backend_busy, set_ai_generating
            any_backend = any(
                (status and status.busy) or self._has_pending_task(key)
                for key, status in self.worker_status.items()
            )
            if not any_backend:
                any_backend = any(self._has_pending_task(key) for key in self.generation_tasks)
            set_ai_backend_busy(any_backend)
            set_ai_generating(any_backend)
        except Exception:
            logger.debug("同步全局后脑状态失败", exc_info=True)

    async def _send_platform_message(self, chat_or_runtime_key: str, text: str, **kwargs):
        adapter = self._adapter_for_key(chat_or_runtime_key)
        text = sanitize_adapter_output_text(text)
        controls = parse_message_controls(text)
        logger.info(
            "[%s] 消息控制标记: reply=%s, at=%s",
            chat_or_runtime_key,
            controls.reply_message_id or "未识别",
            controls.mention_user_ids or "未识别",
        )
        if not adapter.platform_features.supports_message_controls:
            text = strip_message_control_markers(text)
        if not text:
            return []
        return await adapter.send_message(
            self._platform_chat_id(chat_or_runtime_key),
            text,
            **kwargs,
        )

    async def _start_platform_typing(self, chat_or_runtime_key: str):
        return await self._adapter_for_key(chat_or_runtime_key).start_typing(
            self._platform_chat_id(chat_or_runtime_key)
        )

    async def _stop_platform_typing(self, chat_or_runtime_key: str):
        return await self._adapter_for_key(chat_or_runtime_key).stop_typing(
            self._platform_chat_id(chat_or_runtime_key)
        )

    async def _delete_platform_message(self, chat_or_runtime_key: str, message_id: str, **kwargs) -> bool:
        return await self._adapter_for_key(chat_or_runtime_key).delete_message(
            self._platform_chat_id(chat_or_runtime_key),
            message_id,
            **kwargs,
        )

    async def _handle_trigger_notification(self, chat_id: str, reason: str, event_type: str):
        """将外部 trigger 事件转换为主动消息链路通知。"""
        await self._send_proactive_message(chat_id, reason, event_type=event_type)

    async def start_triggers(self, default_chat_id: str = ""):
        """启动 Trigger 系统。应在 adapter 就绪后调用。"""
        # 群监听降级看门狗需要运行中的事件循环，随公共服务一起启动。
        try:
            self.group_listener.start()
        except Exception:
            logger.warning("群监听看门狗启动失败", exc_info=True)
        if self.trigger_manager:
            if default_chat_id:
                self.trigger_manager.default_chat_id = default_chat_id
                self.trigger_manager.default_chat_target = self._target_for_key(default_chat_id)
            if not self.trigger_manager.default_chat_id:
                # 兜底：先从 scheduler 同步，再从持久化文件兜底
                if self.scheduler and self.scheduler.default_chat_id:
                    self.trigger_manager.default_chat_id = self.scheduler.default_chat_id
                    self.trigger_manager.default_chat_target = self._target_for_key(self.scheduler.default_chat_id)
                else:
                    try:
                        persisted_target = load_default_chat_target()
                        if persisted_target:
                            identity = self._identity_from_target(persisted_target)
                            self.trigger_manager.default_chat_id = identity.runtime_key
                            self.trigger_manager.default_chat_target = conversation_target_dict(identity)
                    except Exception as e:
                        logger.warning(f"start_triggers 兜底读取 default_chat_id 失败: {e}")

            # 同步 Trigger 内已构建的子 trigger 的 default_chat_id（如 EmailTrigger）
            if self.trigger_manager.default_chat_id:
                for trig in getattr(self.trigger_manager, "_triggers", []) or []:
                    if hasattr(trig, "default_chat_id") and not trig.default_chat_id:
                        trig.default_chat_id = self.trigger_manager.default_chat_id
                    if hasattr(trig, "default_chat_target") and not getattr(trig, "default_chat_target", None):
                        trig.default_chat_target = dict(self.trigger_manager.default_chat_target or {})

            logger.info(
                f"Trigger 启动参数: default_chat_id={self.trigger_manager.default_chat_id or '(empty)'}"
            )
            await self.trigger_manager.startup()

    async def stop_triggers(self):
        """停止 Trigger 系统。"""
        if self.trigger_manager:
            await self.trigger_manager.shutdown()

    async def shutdown(self) -> None:
        """停止控制器持有的监听、Trigger 与调度服务。"""
        await self.group_listener.shutdown()
        await self.stop_triggers()
        self.stop_scheduler()

    # ------------------------------------------------------------------
    # 模型重载
    # ------------------------------------------------------------------

    def _reload_llm_clients(self):
        """重新加载模型配置并刷新 LLM 客户端。"""
        config.load_config()
        self.llm = get_llm_client(model_alias="smart")
        try:
            self.fast_llm = get_llm_client(model_alias="fast")
        except Exception:
            self.fast_llm = self.llm
        try:
            self.coder_llm = get_llm_client(model_alias="coder")
        except Exception:
            self.coder_llm = self.llm
        try:
            self.image_llm = get_llm_client(model_alias="image")
        except Exception:
            self.image_llm = self.llm
        try:
            self.video_llm = get_llm_client(model_alias="video")
        except Exception:
            self.video_llm = None
        try:
            self.fast_image_llm = get_llm_client(model_alias="fast-image")
        except Exception:
            self.fast_image_llm = None

    # ------------------------------------------------------------------
    # LLM 调用包装（支持 per-chat 非流式开关）
    # ------------------------------------------------------------------

    def _should_use_non_stream(self, chat_id: str) -> bool:
        flag = self.non_stream_flags.get(chat_id)
        if flag is None:
            return bool(self.default_non_stream)
        return bool(flag)

    def _chat_stream_wrapper(self, model_client, chat_id: str, **kwargs):
        # 自动注入词库全局说明 + 系统环境信息到 system_prompt（全链路生效）
        system_prompt = kwargs.get("system_prompt") or ""
        if self._LEXICON_GLOBAL_MARKER not in system_prompt:
            lexicon_global_block = get_lexicon_global_system_prompt_block()
            if lexicon_global_block:
                kwargs = dict(kwargs)
                kwargs["system_prompt"] = f"{system_prompt}\n\n---\n{lexicon_global_block}"
                system_prompt = kwargs["system_prompt"]

        if self._SYSTEM_ENV_MARKER not in system_prompt:
            kwargs = dict(kwargs)
            kwargs["system_prompt"] = (
                f"{system_prompt}\n\n---\n{self._build_system_environment_context(chat_id)}"
            )

        def _filter_kwargs(callable_obj, call_kwargs):
            try:
                sig = inspect.signature(callable_obj)
            except (TypeError, ValueError):
                return call_kwargs

            # 若目标函数支持 **kwargs，则无需过滤
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                return call_kwargs

            allowed = set(sig.parameters.keys())
            return {k: v for k, v in call_kwargs.items() if k in allowed}

        # 部分 provider 支持 non_stream 模式（返回一次性文本），用于特殊场景
        use_non_stream = self._should_use_non_stream(chat_id)
        if use_non_stream and hasattr(model_client, "chat"):
            async def _gen():
                safe_kwargs = _filter_kwargs(model_client.chat, kwargs)
                text = await model_client.chat(**safe_kwargs)
                yield {"type": "text", "content": text}
            return _gen()

        safe_kwargs = _filter_kwargs(model_client.chat_stream, kwargs)
        return model_client.chat_stream(**safe_kwargs)

    def _build_system_environment_context(self, chat_id: str) -> str:
        """构建注入到 system prompt 的系统环境信息。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        adapter_platform = getattr(self.adapter, "platform_name", "unknown")
        return (
            f"{self._SYSTEM_ENV_MARKER}\n"
            f"当前时间: {now}\n"
            f"Chat ID: {chat_id}\n"
            f"适配器平台: {adapter_platform}\n"
            f"操作系统: {py_platform.system()} {py_platform.release()}\n"
            f"Python: {sys.version.split()[0]}\n"
        )

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    async def _send_debug(self, chat_id: str, message: str):
        """如果 chat_id 开启了 debug 模式，发送调试信息给用户。"""
        if not self.debug_mode.get(chat_id, False):
            return
        try:
            if len(message) > 2000:
                message = message[:2000] + "\n... (truncated)"
            safe_debug = f"```\n🐛 {message}\n```"
            await self._send_platform_message(chat_id, safe_debug, parse_media=False)
        except Exception as e:
            logger.debug(f"[{chat_id}] 发送 debug 消息失败: {e}")

    def _was_interrupted(self, chat_id: str) -> bool:
        """检查本次任务是否是被前端打断的（通过检查 session 标记）。"""
        session = self.sessions.get(chat_id, {})
        was = session.pop("_interrupted_by_frontend", False)
        return was

    # ------------------------------------------------------------------
    # 异步保存辅助
    # ------------------------------------------------------------------

    async def _async_save_memory(self, text: str, user_id: str, metadata: Dict[str, Any]):
        """异步保存记忆，避免阻塞主线程。"""
        try:
            await asyncio.to_thread(self.rag.add_memory, text, user_id, metadata)
            logger.debug(f"[{metadata.get('chat_id')}] 记忆已异步保存 (Role: {metadata.get('role')})")
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

    async def _async_save_image_metadata(
        self,
        image_id: str,
        file_path: str,
        tags: str,
        user_id: str,
        chat_id: str,
        ocr_text: str = "",
        platform: str = "",
        platform_message_id: Optional[str] = None,
        tag_status: str = "completed",
        storage_id: str = "",
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
    ):
        """异步保存图片元数据到 MongoDB + Qdrant。"""
        try:
            extra = {}
            if ocr_text:
                extra["ocr_text"] = ocr_text
            await asyncio.to_thread(  # type: ignore[attr-defined]
                self.image_store.save_image_metadata,
                image_id=image_id,
                file_path=file_path,
                tags=tags,
                user_id=user_id,
                chat_id=chat_id,
                extra=extra,
                platform=platform,
                platform_message_id=platform_message_id,
                tag_status=tag_status,
                storage_id=storage_id,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
                owner_id=owner_id,
                relationship_id=relationship_id,
                actor_display_name=actor_display_name,
            )
            logger.debug(f"[{chat_id}] 图片元数据已异步保存: {image_id}" + (f" (含 OCR {len(ocr_text)} 字)" if ocr_text else ""))
        except Exception as e:
            logger.error(f"保存图片元数据失败: {image_id} - {e}")

    async def _async_save_image_stub(
        self,
        image_id: str,
        file_path: str,
        user_id: str,
        chat_id: str,
        platform: str = "",
        platform_message_id: Optional[str] = None,
        media_type: str = "image",
        storage_id: str = "",
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
    ):
        """异步保存占位媒体元数据（无标签，不写向量）。"""
        try:
            await asyncio.to_thread(  # type: ignore[attr-defined]
                self.image_store.save_image_stub,
                image_id=image_id,
                file_path=file_path,
                user_id=user_id,
                chat_id=chat_id,
                platform=platform,
                platform_message_id=platform_message_id,
                media_type=media_type,
                storage_id=storage_id,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
                owner_id=owner_id,
                relationship_id=relationship_id,
                actor_display_name=actor_display_name,
            )
            logger.debug(f"[{chat_id}] 图片占位已异步保存: {image_id}")
        except Exception as e:
            logger.error(f"保存图片占位失败: {image_id} - {e}")

    async def _async_update_image_tags(
        self,
        image_id: str,
        tags: str,
        ocr_text: str,
        description: str,
        user_id: str,
        chat_id: str,
        file_path: str,
        platform: str = "",
        platform_message_id: Optional[str] = None,
        storage_id: str = "",
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
    ):
        """异步补充标签/OCR 并写向量。"""
        try:
            await asyncio.to_thread(  # type: ignore[attr-defined]
                self.image_store.update_image_tags,
                image_id=image_id,
                tags=tags,
                ocr_text=ocr_text,
                description=description,
                user_id=user_id,
                chat_id=chat_id,
                file_path=file_path,
                platform=platform,
                platform_message_id=platform_message_id,
                storage_id=storage_id,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
                owner_id=owner_id,
                relationship_id=relationship_id,
                actor_display_name=actor_display_name,
            )
            logger.debug(f"[{chat_id}] 图片标签已异步更新: {image_id}")
        except Exception as e:
            logger.error(f"更新图片标签失败: {image_id} - {e}")
