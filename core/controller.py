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
import logging
import os
import re
from typing import Dict, Any, Optional, Callable

from adapters.base import BaseAdapter
from brain.llm import llm_client, get_llm_client
from brain.tools import ToolManager
from memory.rag import RAGEngine
from memory.image_store import ImageStore
from memory.message_history import MessageHistory
from skills.loader import SkillLoader
from core.cost_tracker import get_cost_tracker
from core.worker_status import WorkerStatus, BackendTaskQueue
from core.scheduler import ProactiveScheduler
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
    _SPLIT_MARKER_PATTERN = re.compile(r"\[(?:SPLIT)\]", re.IGNORECASE)
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

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def __init__(self, adapter: BaseAdapter, tui_callback: Optional[Callable[[str], None]] = None):
        self.adapter = adapter
        self.tui_callback = tui_callback
        self.llm = llm_client
        self.rag = RAGEngine()
        self.image_store = ImageStore()
        self.skill_loader = SkillLoader()
        self.sessions: Dict[str, Any] = {}
        self.generation_tasks: Dict[str, asyncio.Task] = {}
        
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
        
        if schedule_enabled:
            self.scheduler = ProactiveScheduler(
                cache_dir=workspace_mgr.cache_dir,
                timezone_str=scheduler_tz,
                generate_plan_callback=self._generate_daily_plan_via_llm,
                send_proactive_callback=self._send_proactive_message,
                daily_summary_callback=self._generate_daily_summary,
            )
            logger.info("主动消息调度器已初始化")
        else:
            self.scheduler = None
            logger.info("主动消息调度器已禁用")
        
        self.tool_manager = ToolManager(adapter, image_store=self.image_store, scheduler=self.scheduler)
        
        logger.info(
            f"NoraController 已初始化。RAG: {'Online' if self.rag.enabled else 'Offline'}, "
            f"MessageHistory: Online, Scheduler: {'Online' if self.scheduler else 'Offline'}"
        )

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

    # ------------------------------------------------------------------
    # LLM 调用包装（支持 per-chat 非流式开关）
    # ------------------------------------------------------------------

    def _should_use_non_stream(self, chat_id: str) -> bool:
        flag = self.non_stream_flags.get(chat_id)
        if flag is None:
            return bool(self.default_non_stream)
        return bool(flag)

    def _chat_stream_wrapper(self, model_client, chat_id: str, **kwargs):
        # 部分 provider 支持 non_stream 模式（返回一次性文本），用于特殊场景
        use_non_stream = self._should_use_non_stream(chat_id)
        if use_non_stream and hasattr(model_client, "chat"):
            async def _gen():
                text = await model_client.chat(**kwargs)
                yield {"type": "text", "content": text}
            return _gen()
        return model_client.chat_stream(**kwargs)

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
            await self.adapter.send_message(chat_id, safe_debug, parse_media=False)
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
    ):
        """异步保存占位图片元数据（无标签，不写向量）。"""
        try:
            await asyncio.to_thread(  # type: ignore[attr-defined]
                self.image_store.save_image_stub,
                image_id=image_id,
                file_path=file_path,
                user_id=user_id,
                chat_id=chat_id,
                platform=platform,
                platform_message_id=platform_message_id,
            )
            logger.debug(f"[{chat_id}] 图片占位已异步保存: {image_id}")
        except Exception as e:
            logger.error(f"保存图片占位失败: {image_id} - {e}")

    async def _async_update_image_tags(
        self,
        image_id: str,
        tags: str,
        ocr_text: str,
        user_id: str,
        chat_id: str,
        file_path: str,
        platform: str = "",
        platform_message_id: Optional[str] = None,
    ):
        """异步补充标签/OCR 并写向量。"""
        try:
            await asyncio.to_thread(  # type: ignore[attr-defined]
                self.image_store.update_image_tags,
                image_id=image_id,
                tags=tags,
                ocr_text=ocr_text,
                user_id=user_id,
                chat_id=chat_id,
                file_path=file_path,
                platform=platform,
                platform_message_id=platform_message_id,
            )
            logger.debug(f"[{chat_id}] 图片标签已异步更新: {image_id}")
        except Exception as e:
            logger.error(f"更新图片标签失败: {image_id} - {e}")
