import asyncio
import logging
import os
import re
import time
from typing import Dict, Any, Callable, Optional, List, cast

from adapters.base import BaseAdapter
from brain.llm import llm_client, get_llm_client
from brain.prompts import get_system_prompt
from memory.rag import RAGEngine
from memory.message_history import MessageHistory
from brain.tools import ToolManager
from skills.loader import SkillLoader
from core.cost_tracker import get_cost_tracker
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

    def __init__(self, adapter: BaseAdapter, tui_callback: Optional[Callable[[str], None]] = None):
        self.adapter = adapter
        self.tui_callback = tui_callback
        self.llm = llm_client
        self.rag = RAGEngine()
        self.tool_manager = ToolManager(adapter) # Pass adapter to ToolManager
        self.skill_loader = SkillLoader()
        self.sessions: Dict[str, Any] = {}
        self.generation_tasks: Dict[str, asyncio.Task] = {}
        
        # 前后端分离：每个 chat_id 一个 WorkerStatus
        self.worker_status: Dict[str, WorkerStatus] = {}
        # 消息队列：后端忙碌时暂存用户新消息
        self.pending_messages: Dict[str, List[str]] = {}
        
        # 快速模型（用于前端即时回复，轻量省 token）
        try:
            self.fast_llm = get_llm_client(model_alias="fast")
        except Exception:
            self.fast_llm = self.llm  # fallback 到主模型
        
        # 消息历史管理
        history_cfg = config.get_message_history_config()
        self.message_history = MessageHistory(
            db_path=history_cfg.get("db_path", "memory/message_history.db"),
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
        
        logger.info(f"NoraController 已初始化。RAG: {'Online' if self.rag.enabled else 'Offline'}, MessageHistory: Online")

    async def handle_new_message(self, chat_id: str, text: str):
        """处理来自聚合器的新消息/命令。"""
        logger.debug(f"[{chat_id}] 收到新聚合消息: '{text[:50]}...'")
        
        # 特殊处理 /start 命令
        if text.strip().startswith("/start"):
            # /start 始终抢占
            if chat_id in self.generation_tasks:
                self.generation_tasks[chat_id].cancel()
                await asyncio.sleep(0.1)
            self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
            self.message_history.clear_chat_history("telegram", chat_id, keep_pinned=True)
            status = self.worker_status.get(chat_id)
            if status:
                status.finish()
            self.pending_messages.pop(chat_id, None)
            await self.adapter.send_message(chat_id, "N.O.R.A. Core 已启动。")
            logger.info(f"[{chat_id}] Session已重置 by /start command.")
            return

        # --- 前后端分离逻辑 ---
        status = self.worker_status.get(chat_id)
        if status and status.busy:
            # 后端忙碌：用 fast 模型判断用户意图并生成回复
            logger.info(f"[{chat_id}] 后端忙碌，分析用户意图...")
            
            # 保存用户消息到数据库（不丢消息）
            self.message_history.add_message(
                platform="telegram",
                chat_id=chat_id,
                role="user",
                content=text
            )
            
            # LLM 判断意图并生成回复
            decision = await self._detect_interrupt_intent_and_reply(chat_id, text, status)
            action = decision["action"]
            reply = decision["reply"]
            
            # 发送回复
            await self.adapter.send_message(chat_id, reply)
            self.message_history.add_message(
                platform="telegram",
                chat_id=chat_id,
                role="assistant",
                content=reply
            )
            
            # 根据 action 执行操作
            if action == "stop":
                await self._interrupt_backend(chat_id, reason="stop", user_text=text, skip_reply=True)
                return
            elif action == "change":
                await self._interrupt_backend(chat_id, reason="change", user_text=text, skip_reply=True)
                return
            else:  # action == "queue"
                # 入队列，等后端完成后处理
                self.pending_messages.setdefault(chat_id, []).append(text)
                return

        # --- 正常流程：后端空闲，启动新任务 ---
        # 旧的抢占逻辑保留，以防万一有残留任务
        if chat_id in self.generation_tasks:
            logger.info(f"[{chat_id}] 抢占：取消正在进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        task = asyncio.create_task(self._generate_response(chat_id, text))
        self.generation_tasks[chat_id] = task

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
        
        prompt = (
            f"你是 Nora，用户发来新消息，但你正忙着处理之前的请求。分析用户意图并给出回复。\n\n"
            f"【你当前的工作状态】\n{progress}\n\n"
            f"【用户新消息】\"{text}\"\n\n"
            f"请判断用户意图并回复：\n"
            f"1. 如果用户要求**停止**当前工作（如'停'、'取消'、'不用了'、'算了'、'stop'等），\n"
            f"   输出: ACTION:stop | REPLY:（确认已停止的友好回复，1-2句话）\n\n"
            f"2. 如果用户要求**切换**到新任务（如'先别...了，帮我...'、'换一个'、'改成'等），\n"
            f"   输出: ACTION:change | REPLY:（确认切换的回复，如'好，马上切换～'）\n\n"
            f"3. 如果是**普通追加消息**（如询问进度、补充信息、闲聊等），\n"
            f"   输出: ACTION:queue | REPLY:（告知正在忙+会稍后处理的友好回复，1-3句话）\n\n"
            f"格式: ACTION:xxx | REPLY:yyy\n"
            f"用简短自然的口语回复，不要用 Markdown 格式。"
        )
        
        try:
            response = ""
            stream = self.fast_llm.chat_stream(
                system_prompt="你是 Nora，友好的 AI 助手。简短自然地回复。",
                user_prompt=prompt,
                history=[],
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
                task = asyncio.create_task(self._generate_response(chat_id, user_text))
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
                task = asyncio.create_task(self._generate_response(chat_id, user_text))
                self.generation_tasks[chat_id] = task

    async def _generate_response(self, chat_id: str, text: str):
        """内部生成逻辑。"""
        # Ensure session keys exist
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        logger.debug(f"[{chat_id}] 开始生成响应。History length: {len(session['history'])}")
        
        # --- 初始化后端状态追踪 ---
        MAX_TURNS = 30
        status = self.worker_status.setdefault(chat_id, WorkerStatus())
        status.start(query=text, max_turns=MAX_TURNS)
        
        try:
            if self.tui_callback: self.tui_callback(f"🏃 Handling new message...")
            
            # --- 0. 保存用户消息到数据库 ---
            status.update("保存消息", "正在保存用户消息...")
            self.message_history.add_message(
                platform="telegram",
                chat_id=chat_id,
                role="user",
                content=text
            )
            
            # --- 1. RAG 记忆检索 (Memory Retrieval) ---
            rag_context = ""
            if self.rag.enabled:
                status.update("记忆检索", "正在从大脑检索相关记忆 (RAG)...")
                if self.tui_callback: self.tui_callback("🧠 Retrieving memories (RAG)...")
                logger.debug(f"[{chat_id}] 正在从大脑检索相关记忆...")
                rag_context = self.rag.get_context_string(text, top_k=2)
                if rag_context:
                    logger.info(f"[{chat_id}] RAG 命中: {len(rag_context.splitlines())} lines.")
                else:
                    logger.debug(f"[{chat_id}] RAG 未检索到强相关记忆。")

            # --- 2. 构建 Prompt (Context Reconstruction) ---
            instructions = []
            
            # Inject Workspace Context
            from brain.tools import WORKSPACE_ROOT, SKILLS_DIR
            workspace_info = (
                f"【工作区信息 (Workspace Context)】\n"
                f"当前工作目录 (CWD): {WORKSPACE_ROOT}\n"
                f"技能目录: {SKILLS_DIR}\n"
                f"下载目录: {os.path.join(WORKSPACE_ROOT, 'downloads')}/\n"
                f"⚠️ 禁止读取: config.yml, .env (含 API 密钥)\n"
                f"💡 使用 list_dir 查看目录内容，不要用 exec_command ls"
            )
            instructions.append(workspace_info)
            
            # Inject Skills
            skills = self.skill_loader.scan_skills()
            if skills:
                skill_desc = "\n".join([f"- {s['name']}: {s['description']} (Path: {s.get('path', 'N/A')})" for s in skills])
                instructions.append(
                    f"【可用技能 (Available Skills)】\n{skill_desc}\n\n"
                    f"⚠️ 执行技能时，**必须**使用 `execute_skill(skill_name, args_json)` 工具。\n"
                    f"**严禁**使用 `exec_command` 运行技能脚本（如 `python3 skills/xxx/main.py`）。\n"
                    f"示例: execute_skill(\"pixiv_manager\", '{{\"keyword\": \"碧蓝航线\", \"limit\": \"5\"}}')"
                )

            if "\n" in text.strip(): 
                instructions.append("请仔细阅读并依次回答以下所有问题，不要遗漏。")
            
            # Inject RAG context into instructions or system prompt
            if rag_context:
                instructions.append(f"【相关回忆/背景知识】(请参考以下历史记忆来回答):\n{rag_context}")

            full_user_prompt = text
            
            # Check for salvaged context from previous interruption
            pending_text = session.get("pending_text", "")
            interrupted_thought = session.get("interrupted_thought", "")
            
            if pending_text or interrupted_thought:
                logger.info(f"[{chat_id}] 检测到抢占遗留上下文，正在合并...")
                # Construct a meta-prompt that includes the lost context
                full_user_prompt = (
                    f"（刚才您问了：'{pending_text}'，"
                    f"我正想回答：'{interrupted_thought}'... "
                    f"但还没说完就被新的消息打断了。）\n\n"
                    f"--- 新的消息 ---\n"
                    f"{text}\n\n"
                    f"【指令】请将我刚才未说完的思路与现在的新问题结合，给出一个连贯、完整的最终回复。不要让我感觉对话断裂了。"
                )
                # Clear buffers after consumption
                session["pending_text"] = ""
                session["interrupted_thought"] = ""
            
            system_prompt = get_system_prompt(instructions)

            # --- 3. 执行循环 (Tool Execution Loop) ---
            current_turn = 0
            final_response_buffer = ""
            latest_tool_output = ""  # Safeguard: fallback to show tool output if LLM stays silent
            latest_meaningful_output = ""  # Only meaningful outputs (execute_skill, etc.)
            force_no_tools = False  # 循环检测触发后，下一轮禁用工具
            
            # 临时历史，从数据库加载持久化上下文
            db_context = self.message_history.get_context_messages("telegram", chat_id)
            temp_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in db_context
                if msg["role"] in ("user", "assistant", "system")
            ]
            
            # Typing 状态跟踪（在所有 turns 中保持）
            typing_started = False
            
            status.update("思考中", "正在生成回复...")

            while current_turn < MAX_TURNS:
                current_turn += 1
                status.current_turn = current_turn
                status.update("思考中", f"第 {current_turn}/{MAX_TURNS} 轮推理...")
                if self.tui_callback: self.tui_callback(f"🤔 Thinking... (Turn {current_turn}/{MAX_TURNS})")
                logger.debug(f"[{chat_id}] Turn {current_turn}/{MAX_TURNS}")
                
                stream = self.llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=full_user_prompt if current_turn == 1 else " (Continue processing tool outputs...)",
                    history=temp_history,
                    tools=[] if force_no_tools else self.tool_manager.get_tool_schemas()
                )
                force_no_tools = False  # 重置，仅影响紧跟的一轮
                
                response_text_buffer = ""
                tool_call = None
                usage_data = None  # 用于收集 token 使用数据
                
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    # Handle new dict-based chunks
                    if isinstance(chunk, dict):
                        if chunk["type"] == "text":
                            content = chunk["content"]
                            
                            # 开始接收文本内容时，显示 typing 状态（表示在聊天）
                            if not typing_started:
                                await self.adapter.start_typing(chat_id)
                                typing_started = True
                            
                            response_text_buffer += content
                            
                            # Real-time splitting logic
                            if "[SPLIT]" in response_text_buffer:
                                parts = response_text_buffer.split("[SPLIT]")
                                # Send and sync all complete parts
                                for part in parts[:-1]:
                                    text_to_send = part.strip()
                                    # 过滤掉包含工具调用语法的泄漏内容
                                    if text_to_send and not re.search(
                                        r'(?:execute_skill|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill)\s*\(',
                                        text_to_send
                                    ):
                                        await self.adapter.send_message(chat_id, text_to_send)
                                        # Important: keep final buffer synchronized
                                        temp_history.append({"role": "assistant", "content": text_to_send})
                                # Keep the last incomplete part in buffer
                                response_text_buffer = parts[-1]
                        
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
                            
                    elif isinstance(chunk, str):
                        # Fallback for legacy providers
                        response_text_buffer += chunk
                
                # 记录成本（如果启用）
                if self.cost_tracking_enabled and self.cost_tracker and usage_data:
                    provider = config.get_llm_provider()
                    model = config.get_model_name("smart")  # 默认使用 smart 模型
                    self.cost_tracker.log_usage(
                        provider=provider,
                        model=model,
                        input_tokens=usage_data["input_tokens"],
                        output_tokens=usage_data["output_tokens"],
                        model_alias="smart",
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

                    # --- Loop Detection and Intervention ---
                    # Build a loop key from tool name + target (path/command) for file ops.
                    # This avoids false positives when LLM writes to different files sequentially.
                    _file_tools = {"write_file", "edit_file", "read_file", "create_new_skill", "list_dir"}
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
                                "content": (
                                    f"【系统提示】你已经对 {edit_target} 进行了 {file_edit_counts[edit_target]} 次 edit_file 操作。"
                                    f"这太多了！请停止使用 edit_file。如果还需要修改这个文件，请先用 read_file 读取完整内容，"
                                    f"然后用 write_file 一次性写入所有修改后的完整内容。"
                                    f"如果修改已经完成，请直接给用户回复结果。"
                                )
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
                                "content": (
                                    "【系统提示】你已经多次读取同一个文件了。文件内容已截断是系统限制，无法通过重复调用获取更多内容。"
                                    "请基于已读取到的部分来工作。"
                                    "回复用户时，不要复述文件的代码内容，只需用简短的自然语言总结你的发现或下一步操作。"
                                )
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                        elif tool_name == "edit_file":
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。")
                            temp_history.append({
                                "role": "user",
                                "content": (
                                    "【系统提示】你已经连续多次对同一个文件使用 edit_file。这是低效的做法。"
                                    "请停止 edit_file，改用 read_file 读取完整内容后，用 write_file 一次性写入所有修改。"
                                    "如果不需要继续修改，请直接给用户回复结果。"
                                )
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                        elif tool_name == "execute_skill":
                            logger.warning(f"[{chat_id}] 检测到 execute_skill 工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在引导自查。")
                            temp_history.append({
                                "role": "user",
                                "content": (
                                    "【系统提示】你已经多次调用同一个 skill 但均未成功。请停止重复调用，"
                                    "现在请你自己检查 skill 的代码实现，分析失败原因，尝试修复 bug 或给出诊断建议。"
                                    "可以结合工具输出、报错信息和 skill 代码内容进行自查。"
                                    "最后用简短自然语言总结你的分析和建议，不要复述原始输出。"
                                )
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                        else:
                            # 其他工具
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在引导总结。")
                            temp_history.append({
                                "role": "user",
                                "content": "【系统提示】你已经多次重复调用同一个工具。请停止重复调用，基于已有的结果用简短的自然语言直接给用户回复。不要复述原始输出内容。"
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue  # 不 break，让 LLM 生成总结
                    
                    
                    # Execute Tool
                    status.update("工具调用", f"正在执行 {tool_name}...")
                    status.log_tool(tool_name, tool_args)
                    tool_result = await self.tool_manager.execute(tool_name, cast(Dict[str, Any], tool_args))
                    status.update("处理结果", f"{tool_name} 执行完毕，正在分析结果...")
                    logger.info(f"[{chat_id}] 🔧 Tool Result for {tool_name}: {tool_result[:100]}...")

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
                            truncated_result += (
                                f"\n\n... 【文件已截断】显示了前 {shown_lines}/{total_lines} 行 "
                                f"({TRUNCATE_LIMIT}/{total_len} 字符)。"
                                f"\n\n💡 提示：如需查看后续内容，请使用行范围参数："
                                f"\n   read_file(path=\"{file_path}\", start_line={shown_lines + 1}, end_line={total_lines})"
                                f"\n或分段读取："
                                f"\n   read_file(path=\"{file_path}\", start_line={shown_lines + 1}, end_line={shown_lines + 100})"
                                f"\n\n请不要重复调用 read_file(\"{file_path}\") 而不带行号参数，那只会返回同样的截断内容。"
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
                    "content": "【系统提示】工具调用轮次已达上限，请立即基于已有的工具结果，给用户一个简洁的最终回复。"
                })
                stream = self.llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt="请总结以上工具操作的结果，给出最终回答。",
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
            
            # --- 4. 发送响应 ---
            # If the loop completes and there's still text in the buffer, it's the final message.
            if final_response_buffer:
                # 过滤掉可能泄漏的工具调用语法
                clean_response = re.sub(
                    r'(?:execute_skill|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill)\s*\([^)]*\)',
                    '', final_response_buffer
                ).strip()
                # 过滤掉 "返回结果:" + 原始 JSON/代码块
                clean_response = re.sub(
                    r'返回结果[：:]\s*```[\s\S]*?```', '', clean_response
                ).strip()
                if clean_response:
                    await self.adapter.send_message(chat_id, clean_response)
                elif latest_meaningful_output:
                    # 整条消息都是工具语法泄漏，用工具结果做 fallback
                    pass  # 由下面的 elif 分支处理
            elif latest_meaningful_output:
                # Fallback: LLM stayed silent; do one final LLM call to summarize the tool output
                temp_history.append({
                    "role": "user",
                    "content": "【系统提示】工具已执行完毕，请根据以上工具输出，用自然语言向用户总结执行结果。不要直接输出原始命令行内容。"
                })
                stream = self.llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt="请总结以上操作的执行结果。",
                    history=temp_history,
                    tools=[]
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
                
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
                    "content": f"【系统提示】工具输出如下，请用自然语言简要告诉用户结果：\n{latest_tool_output[:1000]}"
                })
                stream = self.llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt="请简要总结操作结果。",
                    history=temp_history,
                    tools=[]
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
                
                if final_response_buffer:
                    await self.adapter.send_message(chat_id, final_response_buffer)
                else:
                    final_response_buffer = "任务已完成。"
                    await self.adapter.send_message(chat_id, final_response_buffer)
            
            # --- 4.5 保存助手回复到数据库 ---
            if final_response_buffer:
                self.message_history.add_message(
                    platform="telegram",
                    chat_id=chat_id,
                    role="assistant",
                    content=final_response_buffer
                )
            
            # --- 5. RAG 记忆存储 (Memory Storage) ---
            if self.rag.enabled:
                # Store the initial user prompt
                asyncio.create_task(self._async_save_memory(text, {"role": "user", "chat_id": chat_id}))
                
                # Combine all parts of the assistant's turn for a complete memory
                full_assistant_turn = " ".join([h['content'] for h in temp_history if h['role'] == 'assistant'])
                if final_response_buffer and final_response_buffer not in full_assistant_turn:
                    full_assistant_turn += " " + final_response_buffer
                
                if full_assistant_turn.strip():
                     asyncio.create_task(self._async_save_memory(full_assistant_turn.strip(), {"role": "assistant", "chat_id": chat_id}))

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

    async def _process_pending_messages(self, chat_id: str):
        """后端完成后，处理队列中积攒的用户消息。"""
        pending = self.pending_messages.pop(chat_id, [])
        if not pending:
            return
        
        # 合并所有排队消息为一条（避免逐条触发完整的工具循环）
        if len(pending) == 1:
            combined = pending[0]
        else:
            combined = "（以下是我之前排队的多条消息，请一起处理）\n" + "\n".join(
                f"- {msg}" for msg in pending
            )
        
        logger.info(f"[{chat_id}] 开始处理 {len(pending)} 条排队消息。")
        
        # 递归调用，走正常流程
        task = asyncio.create_task(self._generate_response(chat_id, combined))
        self.generation_tasks[chat_id] = task

    async def _async_save_memory(self, text: str, metadata: Dict[str, Any]):
        """异步保存记忆，避免阻塞主线程。"""
        try:
            # 由于 RAG.add_memory 是同步的 (requests 是同步的)，我们需要在 executor 中运行它
            # 或者如果 RAG 内部实现了异步更好。目前先简单用 to_thread
            await asyncio.to_thread(self.rag.add_memory, text, metadata)
            # 降级为 DEBUG，因为每次对话都会触发两次，太吵了
            logger.debug(f"[{metadata.get('chat_id')}] 记忆已异步保存 (Role: {metadata.get('role')})")
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

