import asyncio
import logging
import os
import re
import time
from typing import Dict, Any, Callable, Optional, List, cast

from adapters.base import BaseAdapter
from brain.llm import llm_client, get_llm_client
from brain.prompts import get_system_prompt, render_template, get_soul_prompt, load_identity_context, _read_file_safe, WORKSPACE_SCHEDULE_FILE, WORKSPACE_SOUL_FILE, WORKSPACE_USER_FILE, WORKSPACE_MEMORY_FILE, _resolve_memory_file
from memory.rag import RAGEngine
from memory.image_store import ImageStore
from memory.message_history import MessageHistory
from brain.tools import ToolManager
from skills.loader import SkillLoader
from core.cost_tracker import get_cost_tracker
from core.routing import has_image_input, parse_front_brain_response
from core.scheduler import ProactiveScheduler, AIPresence, set_ai_presence, set_ai_generating, set_ai_backend_busy, record_user_activity, get_user_idle_seconds, record_ai_followup, get_followup_count, clear_conversation_tracking, get_ai_presence, get_ai_state_summary
from brain.multimodal import extract_image_payloads
import config
import json

logger = logging.getLogger(__name__)


class WorkerStatus:
    """后端工作状态追踪器 —— 记录当前正在做什么，供前端查询。"""

    def __init__(self):
        self.busy: bool = False
        self.phase: str = ""          # 当前阶段名 (e.g. "RAG检索", "工具调用")
        self.detail: str = ""         # 更详细描述 (e.g. "正在执行 execute_skill(web_search)")
        self.tool_history: List[str] = []  # 本次任务执行过的工具步骤摘要
        self.current_turn: int = 0
        self.max_turns: int = 0
        self.started_at: float = 0.0
        self.original_query: str = ""  # 用户最初问的问题

    def start(self, query: str, max_turns: int = 30):
        self.busy = True
        self.phase = "初始化"
        self.detail = "正在准备处理请求..."
        self.tool_history = []
        self.current_turn = 0
        self.max_turns = max_turns
        self.started_at = time.time()
        self.original_query = query

    def update(self, phase: str, detail: str = ""):
        self.phase = phase
        self.detail = detail

    def log_tool(self, tool_name: str, tool_args: Any):
        """记录一次工具调用的简略描述。"""
        if isinstance(tool_args, dict):
            # 只取关键参数做摘要
            short_args = {k: (str(v)[:60] + "..." if len(str(v)) > 60 else str(v))
                          for k, v in list(tool_args.items())[:3]}
            self.tool_history.append(f"{tool_name}({short_args})")
        else:
            self.tool_history.append(f"{tool_name}(...)")

    def finish(self):
        self.busy = False
        self.phase = "完成"
        self.detail = ""

    def get_summary(self) -> str:
        """生成一段给用户看的进度概括。"""
        if not self.busy:
            return ""
        elapsed = int(time.time() - self.started_at)
        lines = [
            f"📌 正在处理: \"{self.original_query[:80]}\"",
            f"⏱ 已用时 {elapsed} 秒 | 阶段: {self.phase}",
        ]
        if self.detail:
            lines.append(f"🔧 {self.detail}")
        if self.tool_history:
            recent = self.tool_history[-3:]  # 最近3步
            lines.append("📝 最近步骤:")
            for step in recent:
                lines.append(f"  • {step}")
        lines.append(f"🔄 进度: 第 {self.current_turn}/{self.max_turns} 轮")
        return "\n".join(lines)

class NoraController:
    """处理机器人的核心业务逻辑。"""

    _SPLIT_MARKER_PATTERN = re.compile(r"\[(?:SPLIT)\]", re.IGNORECASE)
    _THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
    _THINK_INLINE_PATTERN = re.compile(r"</?think>", re.IGNORECASE)
    # 匹配 LLM 返回的图片标签块: [IMAGE_TAGS:img_xxx] ... [/IMAGE_TAGS]
    # 注意：image_id 偶发会出现非十六进制字符（如 img_1DgqG），这里放宽匹配避免泄漏到用户侧。
    _IMAGE_TAGS_PATTERN = re.compile(
        r'\[IMAGE_TAGS:([^\]\s]+)\](.*?)\[/IMAGE_TAGS\]',
        re.IGNORECASE | re.DOTALL,
    )

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
        # 消息队列：后端忙碌时暂存用户新消息
        self.pending_messages: Dict[str, List[Dict[str, Any]]] = {}
        # Debug 模式：per-chat 开关，开启后实时推送工具调用、思考等信息
        self.debug_mode: Dict[str, bool] = {}
        
        # 对话延续定时器：per-chat asyncio.Task
        self._followup_timers: Dict[str, asyncio.Task] = {}
        
        # 对话延续配置
        self.FOLLOWUP_INITIAL_DELAY = 120    # 用户停止发言后 120 秒（2分钟）首次检测
        self.FOLLOWUP_RECHECK_INTERVAL = 60  # 此后每 60 秒（1分钟）复查
        self.FOLLOWUP_MAX_COUNT = 3          # 最多连续追话 3 次，之后引导结束对话
        
        # 快速模型（用于后端忙碌时的意图检测，轻量省 token）
        try:
            self.fast_llm = get_llm_client(model_alias="fast")
        except Exception:
            self.fast_llm = self.llm  # fallback 到主模型

        # 后脑模型（用于工具循环 / 深度思考，默认 coder）
        try:
            self.coder_llm = get_llm_client(model_alias="coder")
        except Exception:
            self.coder_llm = self.llm  # fallback 到 smart 模型

        # 图片输入专用模型（若未配置则回退到主模型）
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
            compress_window=history_cfg.get("compress_window", 200),
            compress_ratio=history_cfg.get("compress_ratio", 10),
            archive_threshold=history_cfg.get("archive_threshold", 500),
            timezone=history_cfg.get("timezone", "Asia/Shanghai"),
        )
        
        # 成本跟踪
        cost_cfg = config.get_config().get("cost_tracking", {})
        self.cost_tracking_enabled = cost_cfg.get("enabled", True)
        if self.cost_tracking_enabled:
            custom_prices = cost_cfg.get("custom_prices", {})
            self.cost_tracker = get_cost_tracker(custom_prices=custom_prices)
            logger.info("成本跟踪已启用")
        else:
            self.cost_tracker = None
            logger.info("成本跟踪已禁用")
        
        # --- 主动消息调度器 (Proactive Scheduler) ---
        from workspace_config import get_workspace_manager
        workspace_mgr = get_workspace_manager()
        schedule_cfg = config.get_config().get("schedule", {})
        schedule_enabled = schedule_cfg.get("enabled", True)
        scheduler_tz = history_cfg.get("timezone", "Asia/Shanghai")
        
        if schedule_enabled:
            self.scheduler = ProactiveScheduler(
                cache_dir=workspace_mgr.cache_dir,
                timezone_str=scheduler_tz,
                generate_plan_callback=self._generate_daily_plan_via_llm,
                send_proactive_callback=self._send_proactive_message,
            )
            logger.info("主动消息调度器已初始化")
        else:
            self.scheduler = None
            logger.info("主动消息调度器已禁用")
        
        self.tool_manager = ToolManager(adapter, image_store=self.image_store, scheduler=self.scheduler)
        
        logger.info(f"NoraController 已初始化。RAG: {'Online' if self.rag.enabled else 'Offline'}, MessageHistory: Online, Scheduler: {'Online' if self.scheduler else 'Offline'}")

    async def handle_new_message(self, context: Dict[str, Any]):
        """处理来自适配器的新消息/命令。"""
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")

        # 解析多模态输入：从 [image: ...] 中提取真实图片内容
        clean_text, multimodal_images = extract_image_payloads(text)
        if clean_text:
            text = clean_text

        logger.debug(f"[{chat_id}] 收到新聚合消息: '{text[:50]}...'")
        
        # --- 更新 AI 在线状态：收到消息 → AI 进入在线 ---
        set_ai_presence(AIPresence.ONLINE)
        _activity_id = chat_id if chat_type != "private" else user_id
        record_user_activity(_activity_id)
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
        
        # 特殊处理 /start 命令
        if text.strip().startswith("/start"):
            # /start 始终抢占
            if chat_id in self.generation_tasks:
                self.generation_tasks[chat_id].cancel()
                await asyncio.sleep(0.1)
            self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
            storage_id = chat_id if chat_type != "private" else user_id
            self.message_history.clear_chat_history("telegram", storage_id, keep_pinned=True)
            status = self.worker_status.get(chat_id)
            if status:
                status.finish()
            self.pending_messages.pop(chat_id, None)
            await self.adapter.send_message(chat_id, "N.O.R.A. Core 已启动。")
            logger.info(f"[{chat_id}] Session已重置 by /start command.")
            return

        # 特殊处理 /regenerate_proactive 指令
        # 用法:
        #   /regenerate_proactive
        #   /regenerate_proactive append
        #   /regenerate_proactive replace
        if text.strip().startswith("/regenerate_proactive"):
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
                        f"日期: {result.get('date', '-') }\n"
                        f"本次新增: {result.get('count', 0)}\n"
                        f"今日总未触发: {result.get('total_today', 0)}"
                    )
                else:
                    msg = f"❌ 重新生成失败: {result.get('message', 'unknown error')}"
                await self.adapter.send_message(chat_id, msg)
            except Exception as e:
                logger.error(f"[{chat_id}] /regenerate_proactive 执行失败: {e}", exc_info=True)
                await self.adapter.send_message(chat_id, f"❌ 重新生成失败: {e}")
            return

        # 特殊处理 /schedule_today 指令
        if text.strip().startswith("/schedule_today"):
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
            return

        # 特殊处理 /status 指令
        if text.strip().startswith("/status"):
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
                lines.append(f"⚡ 前脑 (Fast Brain): 🟢 就绪")

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
                pending = self.pending_messages.get(chat_id, [])
                if pending:
                    lines.append(f"\n📬 排队消息: {len(pending)} 条")
                
                # RAG / 消息历史
                lines.append("")
                lines.append(f"🗄️ RAG: {'🟢 启用' if self.rag.enabled else '🔴 离线'}")
                lines.append(f"💰 成本跟踪: {'🟢 启用' if self.cost_tracking_enabled else '🔴 禁用'}")
                
                await self.adapter.send_message(chat_id, "\n".join(lines))
            except Exception as e:
                logger.error(f"[{chat_id}] /status 执行失败: {e}", exc_info=True)
                await self.adapter.send_message(chat_id, f"❌ 获取状态失败: {e}")
            return

        # 特殊处理 /debug 指令
        if text.strip().startswith("/debug"):
            current = self.debug_mode.get(chat_id, False)
            self.debug_mode[chat_id] = not current
            state_str = "🟢 已开启" if self.debug_mode[chat_id] else "🔴 已关闭"
            await self.adapter.send_message(chat_id, f"🐛 Debug 模式: {state_str}\n开启后每次工具调用、技能执行、思考阶段都会实时推送详情。")
            logger.info(f"[{chat_id}] Debug 模式切换为: {self.debug_mode[chat_id]}")
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
            else:  # action == "queue"
                # 入队列，等后端完成后处理
                self.pending_messages.setdefault(chat_id, []).append(context)
                return

        # --- 正常流程：后端空闲 ---
        # 旧的抢占逻辑保留，以防万一有残留任务
        if chat_id in self.generation_tasks:
            logger.info(f"[{chat_id}] 抢占：取消正在进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        # 图片消息直接走后脑（需要 image 模型 + 工具能力）
        if has_image_input(text):
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

        # 2) 前脑生成即时回复 + 路由判断
        front_result = await self._generate_front_chat_response(context)

        # 3) 发送前脑回复给用户（如果有的话）
        user_reply = front_result["user_reply"]
        if user_reply:
            # 使用 [SPLIT] 分段发送
            parts = self._SPLIT_MARKER_PATTERN.split(user_reply)
            for part in parts:
                part = part.strip()
                if part:
                    await self.adapter.send_message(chat_id, part)

            # 保存前脑回复到数据库
            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="assistant",
                content=user_reply,
                user_id="assistant",
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
            logger.info(f"[{chat_id}] 前脑判定需要后脑，启动后脑任务...")
            # 标记上下文：后脑应跳过用户消息保存（前脑已保存）
            backend_context = context.copy()
            backend_context["_front_brain_handled"] = True
            backend_context["_front_brain_reply"] = user_reply
            task = asyncio.create_task(self._generate_response(backend_context))
            self.generation_tasks[chat_id] = task
        else:
            logger.info(f"[{chat_id}] 前脑判定纯聊天，无需后脑。")
            # 纯聊天完成后，也需要标记空闲并启动 followup 计时
            self._mark_scheduler_idle(chat_id)

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
        
        soul = get_soul_prompt()
        prompt = render_template('interrupt_detect.jinja', 'user',
                                 progress=progress, user_text=text)
        
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
            response = ""
            stream = self.fast_llm.chat_stream(
                system_prompt=render_template('interrupt_detect.jinja', 'system', soul=soul),
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
                
                # 提取 ACTION
                action = "queue"  # 默认
                if "ACTION:" in action_part.upper():
                    action_text = action_part.split(":", 1)[1].strip().lower()
                    if "stop" in action_text:
                        action = "stop"
                    elif "change" in action_text:
                        action = "change"
                
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
                    else:
                        reply = f"收到～ 我正在处理之前的请求（{status.phase}），完成后马上看你的新消息！"
                
                logger.info(f"[{chat_id}] LLM判断意图: action={action}, reply='{reply[:50]}'")
                return {"action": action, "reply": reply}
            
            else:
                # 解析失败，fallback 到 queue
                logger.warning(f"[{chat_id}] 意图解析失败，fallback 到 queue。LLM输出: {response[:100]}")
                return {
                    "action": "queue",
                    "reply": f"收到～ 我正在处理之前的请求（{status.phase}），完成后马上看你的新消息！"
                }
        
        except Exception as e:
            logger.warning(f"[{chat_id}] 意图检测失败: {e}，fallback 到 queue")
            return {
                "action": "queue",
                "reply": f"收到～ 我正在忙着处理之前的请求，完成后会看你的新消息！"
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
            # 等待任务真正结束
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        
        # 2. 清理状态
        status = self.worker_status.get(chat_id)
        if status:
            status.finish()
        
        # 3. 清空排队消息（用户已改变意图，旧队列失效）
        self.pending_messages.pop(chat_id, None)
        
        # 4. 根据 reason 决定下一步（如果调用方已回复，则跳过）
        if skip_reply:
            # 调用方已发送回复，直接执行后续操作
            if reason == "change":
                # 启动新任务处理 user_text
                context = {
                    "chat_id": chat_id,
                    "user_id": chat_id, # Fallback, might need better user_id handling
                    "text": user_text,
                    "chat_type": "private" # Fallback
                }
                task = asyncio.create_task(self._generate_response(context))
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
                    "user_id": chat_id, # Fallback
                    "text": user_text,
                    "chat_type": "private" # Fallback
                }
                task = asyncio.create_task(self._generate_response(context))
                self.generation_tasks[chat_id] = task

    async def _send_debug(self, chat_id: str, message: str):
        """如果 chat_id 开启了 debug 模式，发送调试信息给用户。"""
        if not self.debug_mode.get(chat_id, False):
            return
        try:
            # 截断过长的 debug 消息
            if len(message) > 2000:
                message = message[:2000] + "\n... (truncated)"
            safe_debug = f"```\n🐛 {message}\n```"
            await self.adapter.send_message(chat_id, safe_debug, parse_media=False)
        except Exception as e:
            logger.debug(f"[{chat_id}] 发送 debug 消息失败: {e}")

    async def _generate_front_chat_response(self, context: Dict[str, Any]) -> dict:
        """
        前脑即时回复：使用 smart 模型生成自然对话回复 + 路由判断。
        
        不涉及工具调用、RAG 检索等重操作。
        
        Returns:
            {
                "needs_backend": bool,   # 是否需要后脑接管
                "user_reply": str,       # 发给用户的清洁回复（已移除路由信号）
                "raw_response": str,     # LLM 原始输出（含信号，用于日志）
            }
        """
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")
        storage_id = chat_id if chat_type != "private" else user_id

        logger.info(f"[{chat_id}] 前脑处理: '{text[:80]}'")

        # --- 构建前脑 prompt ---
        soul_prompt = get_soul_prompt()
        system_prompt = render_template('front_brain.jinja', 'system', soul_prompt=soul_prompt)
        user_prompt = render_template('front_brain.jinja', 'user', user_message=text)

        # --- 前脑也接入 RAG 记忆检索 ---
        if self.rag.enabled:
            try:
                rag_context = self.rag.get_context_string(text, user_id=storage_id, top_k=2)
                if rag_context:
                    system_prompt = (
                        system_prompt
                        + "\n\n"
                        + render_template('context_injection.jinja', 'rag', rag_context=rag_context)
                    )
                    await self._send_debug(chat_id, f"🧠 前脑RAG命中: {len(rag_context.splitlines())} 行记忆")
            except Exception:
                logger.warning(f"[{chat_id}] 前脑 RAG 检索失败，已降级继续。", exc_info=True)

        # --- 加载对话历史（轻量，仅用于上下文连贯） ---
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        # 只取最近的消息，前脑不需要太多历史
        if len(db_context) > 20:
            db_context = db_context[-20:]
        
        # 去掉最后一条 user 消息（避免和 user_prompt 重复）
        message_content = f"{user_name}: {text}" if chat_type != "private" else text
        if db_context:
            last_msg = db_context[-1]
            if last_msg.get("role") == "user" and str(last_msg.get("content", "")) == message_content:
                db_context = db_context[:-1]

        history = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in db_context
            if msg["role"] in ("user", "assistant")
        ]

        # --- 调用 smart 模型生成回复（流式收集完整文本） ---
        response_text = ""
        usage_data = None
        try:
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=history,
                tools=None,  # 前脑不使用工具
            )
            async for raw_chunk in stream:
                chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                if isinstance(chunk, dict):
                    if chunk["type"] == "text":
                        response_text += chunk["content"]
                    elif chunk["type"] == "usage":
                        usage_data = {
                            "input_tokens": chunk.get("input_tokens", 0),
                            "output_tokens": chunk.get("output_tokens", 0),
                        }
                elif isinstance(chunk, str):
                    response_text += chunk
        except Exception as e:
            logger.error(f"[{chat_id}] 前脑生成失败: {e}", exc_info=True)
            # 前脑失败时回退到后脑
            return {"needs_backend": True, "user_reply": "", "raw_response": ""}

        # 清理思考标签
        response_text = self._strip_thinking_content(response_text)

        # 记录成本
        if self.cost_tracking_enabled and self.cost_tracker and usage_data:
            provider_name = config.get_model_provider("smart")
            provider = config.get_provider_type(provider_name)
            model = config.get_model_name("smart")
            self.cost_tracker.log_usage(
                provider=provider,
                model=model,
                input_tokens=usage_data["input_tokens"],
                output_tokens=usage_data["output_tokens"],
                model_alias="smart",
                context="front_brain",
            )

        # 解析路由信号
        parsed = parse_front_brain_response(response_text)
        logger.info(
            f"[{chat_id}] 前脑结果: needs_backend={parsed['needs_backend']}, "
            f"reply='{parsed['user_reply'][:80]}...'"
        )
        await self._send_debug(
            chat_id,
            f"⚡ 前脑: needs_backend={parsed['needs_backend']}\n回复: {parsed['user_reply'][:200]}"
        )

        return {
            "needs_backend": parsed["needs_backend"],
            "user_reply": parsed["user_reply"],
            "raw_response": response_text,
        }

    async def _generate_response(self, context: Dict[str, Any]):
        """内部生成逻辑。"""
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")

        # 解析多模态输入：把 [image: ...] 对应的本地图片读取为模型可用内容
        clean_text, multimodal_images = extract_image_payloads(text)
        if clean_text:
            text = clean_text

        # 确定用于存储和检索的唯一ID
        storage_id = chat_id if chat_type != "private" else user_id

        # Ensure session keys exist
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        logger.debug(f"[{chat_id}] 开始生成响应。History length: {len(session['history'])}")
        
        # --- 初始化后端状态追踪 ---
        MAX_TURNS = 30
        status = self.worker_status.setdefault(chat_id, WorkerStatus())
        status.start(query=text, max_turns=MAX_TURNS)
        
        # 通知 scheduler: 后端开始忙碌
        self._update_scheduler_state(chat_id, busy=True)

        # 根据输入类型选择模型：图片消息优先走 image 模型，其余走 coder 模型
        model_alias_in_use = "image" if multimodal_images else "coder"
        active_llm = self.image_llm if model_alias_in_use == "image" else self.coder_llm
        
        try:
            if self.tui_callback: self.tui_callback(f"🏃 Handling new message...")
            
            # --- 0. 保存用户消息到数据库 ---
            # 如果前脑已经保存过，跳过重复写入
            front_brain_handled = context.get("_front_brain_handled", False)
            status.update("保存消息", "正在保存用户消息...")
            message_content = f"{user_name}: {text}" if chat_type != "private" else text
            if not front_brain_handled:
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="user",
                    content=message_content,
                    user_id=user_id
                )
            
            # --- 1. RAG 记忆检索 (Memory Retrieval) ---
            rag_context = ""
            if self.rag.enabled:
                status.update("记忆检索", "正在从大脑检索相关记忆 (RAG)...")
                if self.tui_callback: self.tui_callback("🧠 Retrieving memories (RAG)...")
                logger.debug(f"[{chat_id}] 正在从大脑检索相关记忆...")
                rag_context = self.rag.get_context_string(text, user_id=storage_id, top_k=2)
                if rag_context:
                    logger.info(f"[{chat_id}] RAG 命中: {len(rag_context.splitlines())} lines.")
                    await self._send_debug(chat_id, f"🧠 RAG 命中: {len(rag_context.splitlines())} 行记忆")
                else:
                    logger.debug(f"[{chat_id}] RAG 未检索到强相关记忆。")
                    await self._send_debug(chat_id, "🧠 RAG: 未检索到强相关记忆")

            # --- 2. 构建 Prompt (Context Reconstruction) ---
            instructions = []
            
            # Inject Workspace Context
            from brain.tools import WORKSPACE_ROOT, SKILLS_DIR
            workspace_info = render_template('context_injection.jinja', 'workspace',
                                             workspace_root=WORKSPACE_ROOT,
                                             skills_dir=SKILLS_DIR,
                                             downloads_dir=os.path.join(WORKSPACE_ROOT, 'downloads'))
            instructions.append(workspace_info)
            
            # Inject Skills
            skills = self.skill_loader.scan_skills()
            if skills:
                skill_desc = "\n".join([f"- {s['name']}: {s['description']} (Path: {s.get('path', 'N/A')})" for s in skills])
                instructions.append(
                    render_template('context_injection.jinja', 'skills', skill_desc=skill_desc)
                )

            if "\n" in text.strip(): 
                instructions.append(render_template('context_injection.jinja', 'multiline'))
            
            # Inject RAG context into instructions or system prompt
            if rag_context:
                instructions.append(
                    render_template('context_injection.jinja', 'rag', rag_context=rag_context)
                )

            full_user_prompt = text
            
            # 如果有图片，将图片 ID 信息注入到用户 prompt 中，并告知 LLM 返回图片标签
            if multimodal_images:
                image_id_lines = []
                for img in multimodal_images:
                    image_id_lines.append(f"- 图片 ID: {img['image_id']}  文件: {os.path.basename(img['path'])}")
                image_hint = "\n\n" + render_template(
                    'image_tags.jinja',
                    'image_tags_prompt',
                    image_id_lines="\n".join(image_id_lines)
                )
                full_user_prompt = full_user_prompt + image_hint
            
            # Check for salvaged context from previous interruption
            pending_text = session.get("pending_text", "")
            interrupted_thought = session.get("interrupted_thought", "")
            
            if pending_text or interrupted_thought:
                logger.info(f"[{chat_id}] 检测到抢占遗留上下文，正在合并...")
                # Construct a meta-prompt that includes the lost context
                full_user_prompt = render_template('context_injection.jinja', 'preemption_resume',
                                                   pending_text=pending_text,
                                                   interrupted_thought=interrupted_thought,
                                                   new_text=text)
                # Clear buffers after consumption
                session["pending_text"] = ""
                session["interrupted_thought"] = ""
            
            system_prompt = get_system_prompt(instructions, platform=self.adapter.platform_name)

            # --- 3. 执行循环 (Tool Execution Loop) ---
            current_turn = 0
            final_response_buffer = ""
            latest_tool_output = ""  # Safeguard: fallback to show tool output if LLM stays silent
            latest_meaningful_output = ""  # Only meaningful outputs (execute_skill, etc.)
            force_no_tools = False  # 循环检测触发后，下一轮禁用工具
            
            # 临时历史，从数据库加载持久化上下文
            db_context = self.message_history.get_context_messages("telegram", storage_id)
            # 避免重复注入：当前轮用户消息已作为 user_prompt 传入，不应再在 history 中重复一遍
            if db_context:
                last_msg = db_context[-1]
                if last_msg.get("role") == "user" and str(last_msg.get("content", "")) == message_content:
                    db_context = db_context[:-1]
            # 如果前脑已处理，历史尾部可能是 [user, assistant(前脑)]，需要都去掉避免重复
            if front_brain_handled and db_context:
                # 去掉前脑的 assistant 回复
                if db_context[-1].get("role") == "assistant":
                    db_context = db_context[:-1]
                # 再去掉已作为 user_prompt 传入的用户消息
                if db_context and db_context[-1].get("role") == "user" and str(db_context[-1].get("content", "")) == message_content:
                    db_context = db_context[:-1]

            temp_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in db_context
                if msg["role"] in ("user", "assistant", "system")
            ]
            
            # Typing 状态跟踪（在所有 turns 中保持）
            typing_started = False
            # 工具回查图片（view_image return_image=true）注入到下一轮多模态输入
            pending_tool_multimodal_images: List[Dict[str, Any]] = []
            
            status.update("思考中", "正在生成回复...")

            while current_turn < MAX_TURNS:
                current_turn += 1
                status.current_turn = current_turn
                status.update("思考中", f"第 {current_turn}/{MAX_TURNS} 轮推理...")
                if self.tui_callback: self.tui_callback(f"🤔 Thinking... (Turn {current_turn}/{MAX_TURNS})")
                logger.debug(f"[{chat_id}] Turn {current_turn}/{MAX_TURNS}")
                await self._send_debug(chat_id, f"💭 开始第 {current_turn}/{MAX_TURNS} 轮推理...")
                
                turn_multimodal_images = multimodal_images if current_turn == 1 else pending_tool_multimodal_images
                turn_llm = self.image_llm if turn_multimodal_images else active_llm
                stream = turn_llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=full_user_prompt if current_turn == 1 else " (Continue processing tool outputs...)",
                    history=temp_history,
                    tools=[] if force_no_tools else self.tool_manager.get_tool_schemas(),
                    multimodal_images=turn_multimodal_images if turn_multimodal_images else None
                )
                # 仅消费一轮，避免重复注入同一批工具图片
                if current_turn > 1 and pending_tool_multimodal_images:
                    pending_tool_multimodal_images = []
                force_no_tools = False  # 重置，仅影响紧跟的一轮

                # 一进入生成阶段即提示 typing，避免 Telegram 输入状态过短
                if (not typing_started) and getattr(self.adapter.platform_features, "supports_typing_indicator", False):
                    try:
                        await self.adapter.start_typing(chat_id)
                        typing_started = True
                    except Exception:
                        logger.debug("typing indicator failed to start", exc_info=True)
                
                response_text_buffer = ""
                tool_call = None
                usage_data = None  # 用于收集 token 使用数据
                in_think_block = False
                
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    # Handle new dict-based chunks
                    if isinstance(chunk, dict):
                        if chunk["type"] == "text":
                            content = chunk["content"]
                            
                            response_text_buffer += content
                            
                            # Real-time splitting logic
                            if self._SPLIT_MARKER_PATTERN.search(response_text_buffer):
                                ready_parts, response_text_buffer, in_think_block = self._extract_split_ready_parts(
                                    response_text_buffer,
                                    in_think_block,
                                )
                                # Send and sync all complete parts
                                for part in ready_parts:
                                    text_to_send = self._strip_thinking_content(part.strip())
                                    # 过滤掉包含工具调用语法的泄漏内容
                                    if text_to_send and not re.search(
                                        r'(?:execute_skill|execute_tool_plan|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill|search)\s*\(',
                                        text_to_send
                                    ):
                                        await self.adapter.send_message(chat_id, text_to_send)
                                        # Important: keep final buffer synchronized
                                        temp_history.append({"role": "assistant", "content": text_to_send})
                        
                        elif chunk["type"] == "tool_call":
                            tool_call = chunk
                            # 检测到工具调用，停止 typing（不再是聊天状态）
                            # Telegram 的 typing 状态会自动过期，无需显式停止
                            # IMMEDIATELY clear text buffer if tool call is detected, to prevent leaking thoughts.
                            if response_text_buffer:
                                # final_response_buffer += response_text_buffer # This was the source of the leak
                                response_text_buffer = ""
                        
                        elif chunk["type"] == "usage":
                            # 收集 usage 信息
                            usage_data = {
                                "input_tokens": chunk.get("input_tokens", 0),
                                "output_tokens": chunk.get("output_tokens", 0)
                            }
                            await self._send_debug(chat_id, f"📊 Token 用量: input={usage_data['input_tokens']}, output={usage_data['output_tokens']}")
                            
                    elif isinstance(chunk, str):
                        # Fallback for legacy providers
                        response_text_buffer += chunk
                
                # 记录成本（如果启用）
                if self.cost_tracking_enabled and self.cost_tracker and usage_data:
                    provider_name = config.get_model_provider(model_alias_in_use)
                    provider = config.get_provider_type(provider_name)
                    model = config.get_model_name(model_alias_in_use)
                    self.cost_tracker.log_usage(
                        provider=provider,
                        model=model,
                        input_tokens=usage_data["input_tokens"],
                        output_tokens=usage_data["output_tokens"],
                        model_alias=model_alias_in_use,
                        context="chat"
                    )
                
                # Append text to final buffer (visible to user)
                if response_text_buffer:
                    final_response_buffer += response_text_buffer
                    # In a real app, we might send this incrementally. 
                    # For now, we wait for atomic send at the end (as per design), 
                    # OR we could send partials if we supported it.
                    # Current design: Atomic Output.
                
                # If tool call detected
                if tool_call:
                    tool_name = cast(Dict[str, Any], tool_call)["name"]
                    tool_args = cast(Dict[str, Any], tool_call)["args"]
                    if self.tui_callback: self.tui_callback(f"🔧 Executing Tool: {tool_name}")
                    logger.info(f"[{chat_id}] 🧠 AI Request Tool: {tool_name}({tool_args})")
                    # Debug: 工具调用详情
                    args_preview = str(tool_args)[:300] if tool_args else "(无参数)"
                    await self._send_debug(chat_id, f"🔧 调用工具: {tool_name}\n参数: {args_preview}")

                    # --- Loop Detection and Intervention ---
                    # Build a loop key from tool name + target (path/command) for file ops.
                    # This avoids false positives when LLM writes to different files sequentially.
                    _file_tools = {"write_file", "edit_file", "read_file", "create_new_skill", "list_dir", "search"}
                    if tool_name in _file_tools and isinstance(tool_args, dict):
                        _target = tool_args.get("path", tool_args.get("skill_name", ""))
                        # For edit_file, use a broader key: any edit on the same file counts together
                        if tool_name == "edit_file":
                            loop_key = f"edit_file:{_target}"
                        else:
                            loop_key = f"{tool_name}:{_target}"
                    elif tool_name == "execute_skill" and isinstance(tool_args, dict):
                        loop_key = f"execute_skill:{tool_args.get('skill_name', '')}"
                    else:
                        loop_key = tool_name
                    
                    # Also track cumulative edits to the same file (regardless of consecutive or not)
                    file_edit_counts = session.setdefault("file_edit_counts", {})
                    if tool_name == "edit_file" and isinstance(tool_args, dict):
                        edit_target = tool_args.get("path", "")
                        file_edit_counts[edit_target] = file_edit_counts.get(edit_target, 0) + 1
                        if file_edit_counts[edit_target] >= 3:
                            logger.warning(f"[{chat_id}] 对文件 {edit_target} 的 edit_file 调用已达 {file_edit_counts[edit_target]} 次，强制引导使用 write_file。")
                            temp_history.append({
                                "role": "user",
                                "content": render_template('loop_interventions.jinja', 'edit_file_overuse',
                                                           file_path=edit_target, count=file_edit_counts[edit_target])
                            })
                            file_edit_counts[edit_target] = 0  # Reset for potential future edits
                            force_no_tools = True
                            continue
                    
                    last_loop_key = session.get("last_loop_key")
                    
                    if last_loop_key and last_loop_key == loop_key:
                        session["tool_call_loop_counter"] = session.get("tool_call_loop_counter", 0) + 1
                    else:
                        session["tool_call_loop_counter"] = 0
                    
                    session["last_loop_key"] = loop_key

                    if session["tool_call_loop_counter"] >= 2:
                        # execute_skill 允许更高阈值（翻页、重试是合理操作）
                        _high_tolerance_tools = {"execute_skill"}
                        _threshold = 5 if tool_name in _high_tolerance_tools else 2

                        if session["tool_call_loop_counter"] < _threshold:
                            pass  # 未达到该工具的实际阈值，放行
                        elif tool_name == "read_file":
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。")
                            temp_history.append({
                                "role": "user",
                                "content": render_template('loop_interventions.jinja', 'read_file_loop')
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                        elif tool_name == "edit_file":
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。")
                            temp_history.append({
                                "role": "user",
                                "content": render_template('loop_interventions.jinja', 'edit_file_loop')
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                        elif tool_name == "execute_skill":
                            logger.warning(f"[{chat_id}] 检测到 execute_skill 工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在引导自查。")
                            temp_history.append({
                                "role": "user",
                                "content": render_template('loop_interventions.jinja', 'execute_skill_loop')
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                        else:
                            # 其他工具
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在引导总结。")
                            temp_history.append({
                                "role": "user",
                                "content": render_template('loop_interventions.jinja', 'generic_tool_loop')
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue  # 不 break，让 LLM 生成总结
                    
                    
                    # Execute Tool
                    status.update("工具调用", f"正在执行 {tool_name}...")
                    status.log_tool(tool_name, tool_args)
                    if tool_name == "view_image" and isinstance(tool_args, dict):
                        # 兜底注入当前会话 user_id，避免 LLM 漏传导致查不到图片
                        if not str(tool_args.get("user_id", "")).strip():
                            tool_args["user_id"] = storage_id
                    tool_result = await self.tool_manager.execute(tool_name, cast(Dict[str, Any], tool_args))
                    # 如果工具请求了 return_image，提取返回中的 [image: ...] 作为下一轮多模态输入
                    if (
                        tool_name in {"view_image", "crop_image_for_llm"}
                        and isinstance(tool_args, dict)
                        and bool(tool_args.get("return_image", False))
                    ):
                        try:
                            _clean, tool_images = extract_image_payloads(tool_result)
                            if tool_images:
                                pending_tool_multimodal_images = tool_images
                                logger.info(f"[{chat_id}] {tool_name} 返回 {len(tool_images)} 张图片，下一轮切换 image 模型进行分析。")
                        except Exception:
                            logger.warning(f"[{chat_id}] 解析 {tool_name} 返回图片失败，已跳过多模态注入。", exc_info=True)
                    status.update("处理结果", f"{tool_name} 执行完毕，正在分析结果...")
                    logger.info(f"[{chat_id}] 🔧 Tool Result for {tool_name}: {tool_result[:100]}...")
                    # Debug: 工具执行结果
                    result_preview = tool_result[:500] if len(tool_result) > 500 else tool_result
                    await self._send_debug(chat_id, f"📋 {tool_name} 结果 ({len(tool_result)} 字符):\n{result_preview}")

                    # --- TRUNCATION SAFETY VALVE ---
                    # read_file needs higher limit to avoid LLM re-reading in loops
                    TRUNCATE_LIMIT = 8000 if tool_name == "read_file" else 4000
                    if len(tool_result) > TRUNCATE_LIMIT:
                        total_len = len(tool_result)
                        truncated_result = tool_result[:TRUNCATE_LIMIT]
                        if tool_name == "read_file":
                            # 计算行数信息帮助 LLM 理解截断范围
                            shown_lines = truncated_result.count('\n') + 1
                            total_lines = tool_result.count('\n') + 1
                            # 提取文件路径（如果有的话）
                            file_path_match = re.search(r'read_file.*?["\']([^"\']+)["\']', str(tool_args))
                            file_path = file_path_match.group(1) if file_path_match else tool_args.get("path", "该文件")
                            truncated_result += "\n\n" + render_template(
                                'context_injection.jinja', 'read_file_truncated',
                                shown_lines=shown_lines, total_lines=total_lines,
                                truncate_limit=TRUNCATE_LIMIT, total_len=total_len,
                                file_path=file_path, next_start=shown_lines + 1
                            )
                        else:
                            truncated_result += f"\n\n... (Output truncated from {total_len} chars. Use write_file to overwrite the full file if needed.)"
                        logger.warning(f"Tool output for {tool_name} was truncated from {total_len} chars.")
                    else:
                        truncated_result = tool_result
                    latest_tool_output = truncated_result  # remember latest tool output for fallback delivery
                    # Only track successful, meaningful outputs for user-facing fallback
                    # Skip errors/refusals/internal diagnostics — those are for LLM, not the user
                    _user_facing_tools = {"execute_skill", "create_new_skill"}
                    _is_error = any(kw in truncated_result[:100] for kw in ["Error:", "REFUSED:", "failed", "not found"])
                    if tool_name in _user_facing_tools and not _is_error:
                        latest_meaningful_output = truncated_result
                    
                    # Append history for tools
                    if current_turn == 1 and full_user_prompt not in [h['content'] for h in temp_history]:
                        temp_history.append({"role": "user", "content": full_user_prompt})
                    
                    # Store tool invocation in history, but do NOT leak placeholder text to the session history content
                    # If there's real text in response_text_buffer, we keep it; otherwise, we use a internal label
                    # assistant_msg = response_text_buffer if response_text_buffer else f"（正在调用工具: {tool_name}）"
                    # temp_history.append({"role": "assistant", "content": assistant_msg})
                    temp_history.append({"role": "user", "content": f"【Tool Output for {tool_name}】\n{truncated_result}"})
                    
                    # CRITICAL: Sync current progress back to the main session immediately to prevent state loss on preemption
                    session["history"] = list(temp_history)
                    
                    # Loop continues to let LLM process the result
                    continue
                else:
                    # No tool call -> Final response
                    break
            
            # If loop exhausted MAX_TURNS with pending tool work, inject a wrap-up prompt and do one final LLM call
            if current_turn >= MAX_TURNS and not final_response_buffer:
                logger.warning(f"[{chat_id}] Tool loop exhausted {MAX_TURNS} turns. Requesting LLM summary.")
                temp_history.append({
                    "role": "user",
                    "content": render_template('loop_interventions.jinja', 'turns_exhausted')
                })
                stream = active_llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=render_template('loop_interventions.jinja', 'summarize_turns_exhausted'),
                    history=temp_history,
                    tools=[]  # No tools for final summary
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
            
            logger.debug(f"[{chat_id}] 生成完成。")
            await self._send_debug(chat_id, f"✅ 生成完成，共 {current_turn} 轮，最终回复 {len(final_response_buffer)} 字符")
            
            # --- 4. 发送响应 ---
            # 先提取图片标签（在任何清理之前），供后续存储
            image_tags_extracted: Dict[str, str] = {}  # image_id -> tags
            if multimodal_images and final_response_buffer:
                for m in self._IMAGE_TAGS_PATTERN.finditer(final_response_buffer):
                    img_id = m.group(1).strip()
                    tags_text = m.group(2).strip()
                    if img_id and tags_text:
                        image_tags_extracted[img_id] = tags_text
                logger.debug(f"[{chat_id}] 已提取 {len(image_tags_extracted)} 个图片标签块。")

            # If the loop completes and there's still text in the buffer, it's the final message.
            if final_response_buffer:
                # 过滤掉可能泄漏的工具调用语法
                clean_response = re.sub(
                    r'(?:execute_skill|execute_tool_plan|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill|search)\s*\([^)]*\)',
                    '', final_response_buffer
                ).strip()
                # 过滤掉 "返回结果:" + 原始 JSON/代码块
                clean_response = re.sub(
                    r'返回结果[：:]\s*```[\s\S]*?```', '', clean_response
                ).strip()
                clean_response = self._strip_thinking_content(clean_response)
                if clean_response:
                    await self.adapter.send_message(chat_id, clean_response)
                elif latest_meaningful_output:
                    # 整条消息都是工具语法泄漏，用工具结果做 fallback
                    pass  # 由下面的 elif 分支处理
            elif latest_meaningful_output:
                # Fallback: LLM stayed silent; do one final LLM call to summarize the tool output
                temp_history.append({
                    "role": "user",
                    "content": render_template('loop_interventions.jinja', 'llm_silent_meaningful')
                })
                stream = active_llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=render_template('loop_interventions.jinja', 'summarize_meaningful'),
                    history=temp_history,
                    tools=[]
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
                
                final_response_buffer = self._strip_thinking_content(final_response_buffer)
                if final_response_buffer:
                    await self.adapter.send_message(chat_id, final_response_buffer)
                else:
                    # 真的没生成内容，发简短提示
                    final_response_buffer = "任务已完成。"
                    await self.adapter.send_message(chat_id, final_response_buffer)
            elif latest_tool_output:
                # Last resort: LLM truly silent, summarize via one more call
                temp_history.append({
                    "role": "user",
                    "content": render_template('loop_interventions.jinja', 'llm_silent_fallback',
                                               tool_output=latest_tool_output[:1000])
                })
                stream = active_llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=render_template('loop_interventions.jinja', 'summarize_fallback'),
                    history=temp_history,
                    tools=[]
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
                
                final_response_buffer = self._strip_thinking_content(final_response_buffer)
                if final_response_buffer:
                    await self.adapter.send_message(chat_id, final_response_buffer)
                else:
                    final_response_buffer = "任务已完成。"
                    await self.adapter.send_message(chat_id, final_response_buffer)
            
            # --- 4.5 保存助手回复到数据库 ---
            if final_response_buffer:
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=final_response_buffer,
                    user_id="assistant"
                )
            
            # --- 4.6 图片记忆存储 (Image Memory Storage) ---
            if multimodal_images and self.image_store.enabled:
                for img in multimodal_images:
                    img_id = img["image_id"]
                    tags = image_tags_extracted.get(img_id, "")
                    if not tags:
                        # LLM 没有按格式返回标签，用简单的文件名作为 fallback
                        tags = f"用户发送的图片: {os.path.basename(img['path'])}"
                    asyncio.create_task(
                        self._async_save_image_metadata(
                            image_id=img_id,
                            file_path=img["path"],
                            tags=tags,
                            user_id=storage_id,
                            chat_id=chat_id,
                        )
                    )
                    logger.info(f"[{chat_id}] 图片 {img_id} 标签已提取并入库: '{tags[:60]}...'")
            
            # --- 5. RAG 记忆存储 (Memory Storage) ---
            if self.rag.enabled:
                # Store the initial user prompt
                asyncio.create_task(self._async_save_memory(message_content, storage_id, {"role": "user", "chat_id": chat_id}))
                
                # Combine all parts of the assistant's turn for a complete memory
                full_assistant_turn = " ".join([h['content'] for h in temp_history if h['role'] == 'assistant'])
                if final_response_buffer and final_response_buffer not in full_assistant_turn:
                    full_assistant_turn += " " + final_response_buffer
                
                if full_assistant_turn.strip():
                     asyncio.create_task(self._async_save_memory(full_assistant_turn.strip(), storage_id, {"role": "assistant", "chat_id": chat_id}))

            # Update session history (in-memory cache, DB is source of truth)
            session["history"] = temp_history
            if final_response_buffer:
                 if not any(final_response_buffer in str(h.get('content', '')) for h in session["history"]):
                    session["history"].append({"role": "assistant", "content": final_response_buffer})

            if len(session["history"]) > 20:
                session["history"] = session["history"][-20:]
            logger.debug(f"[{chat_id}] History updated. New length: {len(session['history'])}")

        except asyncio.CancelledError:
            # --- 抢占/打断发生：抢救现场 ---
            # 标记后端完成（打断方也会调 finish，但这里确保一定执行）
            status.finish()
            
            session["pending_text"] = text
            
            if 'final_response_buffer' in locals() and final_response_buffer:
                session["interrupted_thought"] = final_response_buffer
                logger.info(f"[{chat_id}] 任务被打断：保存了部分思路 ('{final_response_buffer[:30]}...') 和用户输入。")
            else:
                session["interrupted_thought"] = ""
                logger.info(f"[{chat_id}] 任务被打断：尚未生成内容，仅保存了用户输入。")
        
        except Exception as e:
            if self.tui_callback: self.tui_callback(f"❌ Error: {e.__class__.__name__}")
            logger.error(f"[{chat_id}] 生成响应时出现严重错误。", exc_info=True)
            try:
                await self.adapter.send_message(chat_id, "抱歉，处理您的请求时出现内部错误。")
            except Exception:
                pass  # 发送失败也不能再抛
            
        finally:
            # 确保后端状态被清理（CancelledError 块可能已经 finish 了，重复调用无害）
            status.finish()
            # 通知 scheduler: 后端空闲
            self._mark_scheduler_idle(chat_id)
            if self.tui_callback: self.tui_callback("✅ Idle")
            self.generation_tasks.pop(chat_id, None)
            logger.debug(f"[{chat_id}] 本次生成任务结束。")
            
            # --- 处理排队消息（仅在非取消情况下） ---
            # 如果任务是被打断的（CancelledError），排队消息由 _interrupt_backend 负责处理
            # 只在正常完成或异常完成时处理队列
            if not self._was_interrupted(chat_id):
                await self._process_pending_messages(chat_id)

    def _was_interrupted(self, chat_id: str) -> bool:
        """检查本次任务是否是被前端打断的（通过检查 session 标记）。"""
        session = self.sessions.get(chat_id, {})
        was = session.pop("_interrupted_by_frontend", False)
        return was

    @classmethod
    def _strip_thinking_content(cls, text: str) -> str:
        """移除模型可能泄漏的思维链标签内容（如 <think>...</think>）和图片标签块。"""
        if not text:
            return ""
        cleaned = cls._THINK_BLOCK_PATTERN.sub("", text)
        cleaned = cls._THINK_INLINE_PATTERN.sub("", cleaned)
        # 移除 IMAGE_TAGS 块（不应展示给用户，仅后台存储用）
        cleaned = cls._IMAGE_TAGS_PATTERN.sub("", cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        return cleaned

    @classmethod
    def _strip_stream_think_segment(cls, text: str, in_think_block: bool) -> tuple[str, bool]:
        """按流式片段清理 <think> 内容，支持跨分段保持状态，避免泄漏到用户侧。"""
        if not text:
            return "", in_think_block

        lower_text = text.lower()
        out: List[str] = []
        i = 0

        while i < len(text):
            if in_think_block:
                close_idx = lower_text.find("</think>", i)
                if close_idx == -1:
                    # 整段都处于思考块内，不输出任何内容
                    return "".join(out), True
                i = close_idx + len("</think>")
                in_think_block = False
                continue

            open_idx = lower_text.find("<think>", i)
            close_idx = lower_text.find("</think>", i)

            if open_idx == -1 and close_idx == -1:
                out.append(text[i:])
                break

            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                # 孤立闭合标签，跳过标签本身
                out.append(text[i:close_idx])
                i = close_idx + len("</think>")
                continue

            if open_idx != -1:
                out.append(text[i:open_idx])
                i = open_idx + len("<think>")
                in_think_block = True
                continue

        cleaned = "".join(out)
        return cleaned, in_think_block

    @classmethod
    def _extract_split_ready_parts(cls, buffer: str, in_think_block: bool) -> tuple[List[str], str, bool]:
        """从流式缓冲中提取被 [SPLIT] 完整分隔的片段，并做状态化思考内容清洗。"""
        ready_parts: List[str] = []
        last_idx = 0

        for marker in cls._SPLIT_MARKER_PATTERN.finditer(buffer):
            raw_part = buffer[last_idx:marker.start()]
            visible_part, in_think_block = cls._strip_stream_think_segment(raw_part, in_think_block)
            if visible_part.strip():
                ready_parts.append(visible_part)
            last_idx = marker.end()

        remaining = buffer[last_idx:]
        return ready_parts, remaining, in_think_block

    async def _process_pending_messages(self, chat_id: str):
        """后端完成后，处理队列中积攒的用户消息。"""
        pending = self.pending_messages.pop(chat_id, [])
        if not pending:
            return
        
        # 合并所有排队消息为一条（避免逐条触发完整的工具循环）
        if len(pending) == 1:
            combined_context = pending[0]
        else:
            # 合并文本，保留第一个消息的上下文
            combined_text = "（以下是我之前排队的多条消息，请一起处理）\n" + "\n".join(
                f"- {p['text']}" for p in pending
            )
            combined_context = pending[0].copy()
            combined_context["text"] = combined_text
        
        logger.info(f"[{chat_id}] 开始处理 {len(pending)} 条排队消息。")
        
        # 递归调用，走正常流程
        task = asyncio.create_task(self._generate_response(combined_context))
        self.generation_tasks[chat_id] = task

    async def _async_save_memory(self, text: str, user_id: str, metadata: Dict[str, Any]):
        """异步保存记忆，避免阻塞主线程。"""
        try:
            # 由于 RAG.add_memory 是同步的 (requests 是同步的)，我们需要在 executor 中运行它
            # 或者如果 RAG 内部实现了异步更好。目前先简单用 to_thread
            await asyncio.to_thread(self.rag.add_memory, text, user_id, metadata)
            # 降级为 DEBUG，因为每次对话都会触发两次，太吵了
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
    ):
        """异步保存图片元数据到 MongoDB + Qdrant。"""
        try:
            await asyncio.to_thread(
                self.image_store.save_image_metadata,
                image_id=image_id,
                file_path=file_path,
                tags=tags,
                user_id=user_id,
                chat_id=chat_id,
            )
            logger.debug(f"[{chat_id}] 图片元数据已异步保存: {image_id}")
        except Exception as e:
            logger.error(f"保存图片元数据失败: {image_id} - {e}")

    # ------------------------------------------------------------------
    # 主动消息调度系统 (Proactive Scheduler Integration)
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
        # 保持 ONLINE 状态 — 对话延续定时器会在 2 分钟后触发追话检测
        # 只有当对话被判定为结束（END）或追话超限时，才降为 SEMI_ONLINE
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
            # 首次延迟
            await asyncio.sleep(self.FOLLOWUP_INITIAL_DELAY)

            while True:
                # 如果 AI 状态已不是 ONLINE（比如被其他逻辑切走了），退出
                if get_ai_presence() != AIPresence.ONLINE:
                    logger.debug(f"[{chat_id}] AI 状态非 ONLINE，对话延续循环退出")
                    return

                # 如果后端正忙（在处理新消息），退出（新消息会重置此定时器）
                status = self.worker_status.get(chat_id)
                if status and status.busy:
                    logger.debug(f"[{chat_id}] 后端忙碌，对话延续循环退出")
                    return

                idle_secs = get_user_idle_seconds(chat_id)
                count = get_followup_count(chat_id)

                # 安全检查: 如果追话次数已超限，直接结束对话
                if count >= self.FOLLOWUP_MAX_COUNT:
                    logger.info(f"[{chat_id}] 连续追话已达 {count} 次，引导结束对话")
                    await self._send_wrapup_message(chat_id)
                    self._transition_to_semi_online(chat_id)
                    return

                # 用 fast 模型检测
                decision = await self._detect_followup_intent(chat_id, idle_secs, count)
                logger.info(f"[{chat_id}] 对话延续检测结果: {decision} (idle={idle_secs:.0f}s, count={count})")

                if decision == "FOLLOWUP":
                    await self._send_followup_message(chat_id, idle_secs)
                    record_ai_followup(chat_id)
                    # 等待下一次检测间隔
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

                elif decision == "WAIT":
                    # 等一段时间再次检测
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

                elif decision == "END":
                    # 如果已经追过话但用户没回，发送结束消息
                    if count > 0:
                        await self._send_wrapup_message(chat_id)
                    self._transition_to_semi_online(chat_id)
                    return

                else:
                    # 未知结果，保守处理：等一会再看
                    logger.warning(f"[{chat_id}] 对话延续检测返回未知结果: {decision}")
                    await asyncio.sleep(self.FOLLOWUP_RECHECK_INTERVAL)

        except asyncio.CancelledError:
            logger.debug(f"[{chat_id}] 对话延续循环被取消（用户回来了或新任务开始）")
        except Exception as e:
            logger.error(f"[{chat_id}] 对话延续循环异常: {e}", exc_info=True)
            # 出错时安全降级到 SEMI_ONLINE
            self._transition_to_semi_online(chat_id)

    def _transition_to_semi_online(self, chat_id: str):
        """安全地从 ONLINE 过渡到 SEMI_ONLINE。"""
        clear_conversation_tracking(chat_id)
        set_ai_presence(AIPresence.SEMI_ONLINE)
        self._followup_timers.pop(chat_id, None)
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

        # 获取最近对话记录
        storage_id = chat_id  # 对话延续上下文使用 chat_id
        db_context = self.message_history.get_context_messages("telegram", storage_id)
        recent_msgs = db_context[-10:] if db_context else []
        recent_conversation = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
            for m in recent_msgs
            if m.get("role") in ("user", "assistant") and m.get("content")
        )

        if not recent_conversation:
            return "END"  # 没有对话记录，直接结束

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
            # 提取关键字
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

        # 获取最近对话记录
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

            # 用正常模型生成追话
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
                # 写入消息历史（主动追话也要保存到上下文）
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant"
                )
                # 同步到 session history
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
                # 写入消息历史
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant"
                )
                # 同步到 session history
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

    async def _generate_daily_plan_via_llm(self) -> List[Dict[str, str]]:
        """
        LLM 回调：读取 SCHEDULE.md + SOUL.md + USER.md + MEMORY.md，
        生成今日主动消息触发计划。
        
        Returns:
            [{"time": "08:00", "reason": "早安问候"}, {"time": "12:30"}, ...]
            reason 字段可选。
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

        # 使用 Jinja2 模板渲染 prompt
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

            # 解析 JSON
            response = response.strip()
            # 去掉可能的 markdown 代码块
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
        
        Args:
            chat_id: 发送目标
            reason: 触发缘由（可能为空字符串）
            event_type: "proactive" (每日计划) 或 "alarm" (动态闹钟)
        """
        reason_display = reason if reason else "(无缘由)"
        logger.info(f"[{chat_id}] 触发主动消息: type={event_type}, reason={reason_display}")

        # 获取简短的用户上下文
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

        # 主动消息也应参考当前会话上下文（近期对话）
        db_context = self.message_history.get_context_messages("telegram", chat_id)
        proactive_history = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in db_context
            if msg.get("role") in ("user", "assistant", "system") and msg.get("content")
        ]
        if len(proactive_history) > 12:
            proactive_history = proactive_history[-12:]

        # 若 DB 上下文较少，补充 session 内存中的最近内容
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
                # 保存到消息历史
                storage_id = chat_id
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=response,
                    user_id="assistant"
                )

                # 同步到会话上下文（in-memory history），让后续轮次也能感知主动消息内容
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

