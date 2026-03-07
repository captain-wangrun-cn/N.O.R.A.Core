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
from core.routing import has_image_input, parse_front_brain_response

logger = logging.getLogger(__name__)

# --- Tool Call Leak Detection & Cleaning ---
# 工具名列表，用于检测 LLM 以文本形式"假装"调用工具的泄漏
_TOOL_NAMES = r'(?:execute_skill|execute_tool_plan|execute_tool|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill|search|get_available_skills|delegate_to_coder)'

# 组合正则：匹配各种泄漏格式
_TOOL_LEAK_PATTERNS = [
    # 1. 函数调用格式: tool_name(...) 或 tool_name('...', {...})
    re.compile(_TOOL_NAMES + r"""\s*\(""", re.IGNORECASE),
    # 2. XML 标签格式: <execute_skill>...</execute_skill> 或 <execute_tool>
    re.compile(r'</?(?:execute_skill|execute_tool|tool_call|function_call)\b', re.IGNORECASE),
    # 3. execute_tool('tool_name', {...}) 格式
    re.compile(r"""execute_tool\s*\(\s*['"]""", re.IGNORECASE),
    # 4. JSON-like 工具调用块: {"name": "edit_file", "args": ...} 或 {"file_path": ..., "old_code": ..., "new_code": ...}
    re.compile(r"""['"](?:old_code|new_code|file_path|args_json)['"]\s*[:=]""", re.IGNORECASE),
    # 5. Python 关键字参数格式: old_code="""...""" 或 new_code="..." (截图中出现的模式)
    re.compile(r"""(?:old_code|new_code|file_path)\s*=\s*(?:['"]{1,3})""", re.IGNORECASE),
    # 6. 代码块中包含工具调用（如 ```python\n edit_file(...)）
    re.compile(r'```\s*(?:python)?\s*\n\s*' + _TOOL_NAMES, re.IGNORECASE),
]


def _is_tool_call_leak(text: str) -> bool:
    """检测文本是否包含工具调用泄漏（LLM 以文本输出工具调用而非真正使用 function calling）。"""
    for pattern in _TOOL_LEAK_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _try_parse_leaked_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """
    尝试从泄漏的文本中解析出工具调用的名称和参数。
    如果解析成功，返回 {"name": str, "args": dict}；否则返回 None。
    
    支持的格式：
    1. tool_name(path="...", old_code="...", new_code="...")
    2. tool_name("path", "content")
    3. execute_tool('tool_name', {...})
    4. tool_name(path, content) — 位置参数
    5. 三引号形式: new_code='''...''' 或 new_code=\"\"\"...\"\"\"
    """
    
    def _extract_string_arg(text: str, arg_name: str) -> Optional[str]:
        """提取命名参数值，支持单引号、双引号和三引号。"""
        # 三引号优先（最贪婪）
        for quote in ['"""', "'''"]:
            pattern = re.escape(arg_name) + r'\s*=\s*' + re.escape(quote) + r'([\s\S]*?)' + re.escape(quote)
            m = re.search(pattern, text)
            if m:
                return m.group(1)
        # 单引号/双引号
        for quote in ['"', "'"]:
            pattern = re.escape(arg_name) + r'\s*=\s*' + re.escape(quote) + r'((?:[^' + quote + r'\\]|\\.)*)' + re.escape(quote)
            m = re.search(pattern, text, re.DOTALL)
            if m:
                return m.group(1)
        return None
    
    # Pattern 1: edit_file(path="...", old_code="...", new_code="...")
    match = re.search(
        r'(edit_file|write_file|read_file)\s*\(',
        text, re.IGNORECASE
    )
    if match:
        tool_name = match.group(1).lower()
        call_text = text[match.start():]  # 从工具名开始的子串
        
        # 提取 path 参数
        file_path = _extract_string_arg(call_text, "path")
        if not file_path:
            # 尝试位置参数: tool_name("path", ...)
            pos_match = re.search(r'\(\s*["\']([^"\']+)["\']', call_text)
            if pos_match:
                file_path = pos_match.group(1)
        
        if file_path and tool_name == "edit_file":
            old_code = _extract_string_arg(call_text, "old_code")
            new_code = _extract_string_arg(call_text, "new_code")
            if old_code is not None and new_code is not None:
                return {
                    "name": "edit_file",
                    "args": {
                        "path": file_path,
                        "old_code": old_code,
                        "new_code": new_code,
                    }
                }
        elif file_path and tool_name == "write_file":
            content = _extract_string_arg(call_text, "content")
            if not content:
                # 位置参数形式: write_file("path", "content")  或三引号
                # 尝试匹配第二个参数
                after_path = call_text[call_text.find(file_path) + len(file_path):]
                for quote in ['"""', "'''", '"', "'"]:
                    esc = re.escape(quote)
                    if quote in ['"""', "'''"]:
                        m = re.search(esc + r'([\s\S]*?)' + esc, after_path)
                    else:
                        m = re.search(esc + r'((?:[^' + quote + r'\\]|\\.)*)' + esc, after_path, re.DOTALL)
                    if m:
                        content = m.group(1)
                        break
            if content is not None:
                return {
                    "name": "write_file",
                    "args": {"path": file_path, "content": content}
                }
    
    # Pattern 2: execute_tool('tool_name', {...})
    match = re.search(
        r"execute_tool\s*\(\s*['\"](\w+)['\"]\s*,\s*(\{[\s\S]*?\})\s*\)",
        text, re.IGNORECASE
    )
    if match:
        tool_name = match.group(1)
        try:
            args = json.loads(match.group(2))
            return {"name": tool_name, "args": args}
        except json.JSONDecodeError:
            pass
    
    # Pattern 3: execute_skill("skill_name", '{"key": "value"}')
    match = re.search(
        r'execute_skill\s*\(\s*["\'](\w+)["\']\s*,\s*["\'](\{.+?\})["\']',
        text, re.IGNORECASE | re.DOTALL
    )
    if match:
        skill_name = match.group(1)
        try:
            args = json.loads(match.group(2))
            return {"name": "execute_skill", "args": {"skill_name": skill_name, "args_json": json.dumps(args)}}
        except json.JSONDecodeError:
            pass
    
    return None


def _clean_tool_call_leaks(text: str) -> str:
    """
    从最终响应中清除工具调用泄漏文本，返回清理后的自然语言部分。
    如果整段文本都是泄漏，返回空字符串。
    """
    cleaned = text

    # 1. 移除 XML 标签块: <execute_skill>...</execute_skill> 等
    cleaned = re.sub(
        r'<execute_skill>[\s\S]*?</execute_skill>',
        '', cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(
        r'<execute_tool>[\s\S]*?</execute_tool>',
        '', cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(
        r'</?(?:execute_skill|execute_tool|tool_call|function_call)\b[^>]*>',
        '', cleaned, flags=re.IGNORECASE
    )

    # 2. 移除函数调用格式: execute_tool('edit_file', {...}) 等（可能跨多行）
    cleaned = re.sub(
        r"""execute_tool\s*\(\s*['"][^'"]*['"]\s*,\s*\{[^}]*\}\s*\)""",
        '', cleaned, flags=re.IGNORECASE | re.DOTALL
    )

    # 3. 移除 tool_name({...}) 格式的泄漏（跨多行）
    cleaned = re.sub(
        _TOOL_NAMES + r"""\s*\([\s\S]*?\)""",
        '', cleaned, flags=re.IGNORECASE
    )

    # 4. 移除 "返回结果:" + 代码块
    cleaned = re.sub(
        r'返回结果[：:]\s*```[\s\S]*?```', '', cleaned
    )

    # 5. 移除包含 old_code/new_code 等参数的 JSON 块
    cleaned = re.sub(
        r"""\{\s*['"](?:file_path|old_code|new_code|path|args_json)['"][\s\S]*?\}""",
        '', cleaned, flags=re.IGNORECASE
    )

    # 6. 移除 Python 关键字参数格式: old_code="""...""", new_code="..."
    cleaned = re.sub(
        r"""(?:old_code|new_code|file_path)\s*=\s*(?:'{3}[\s\S]*?'{3}|"{3}[\s\S]*?"{3}|"[^"]*"|'[^']*')""",
        '', cleaned, flags=re.IGNORECASE
    )

    # 7. 移除代码块中的工具调用
    cleaned = re.sub(
        r'```\s*(?:python)?\s*\n[\s\S]*?```',
        '', cleaned, flags=re.IGNORECASE
    )

    # 清理多余空行
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    return cleaned


# --- Tool Usage Nudge Detection ---

def _should_nudge_tool_use(user_text: str, response_text: str) -> tuple:
    """
    极简检测：LLM 是否应该用工具却只给了文本回复。
    
    唯一判据：回复中包含 ``` 代码块。
    当 LLM 拥有 function calling 能力时，如果它在回复中贴出代码块
    （命令/脚本/代码片段），几乎一定意味着它在"建议用户自己跑"
    而非通过工具直接执行。
    
    返回 (should_nudge: bool, reason: str)
    """
    if not response_text or len(response_text.strip()) < 50:
        return False, ""
    
    # 唯一信号：回复中出现代码块
    if '```' in response_text:
        return True, "response contains code block without tool call"
    
    return False, ""


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
        self.mode: str = "idle"  # idle | front_chat | backend_task

    def start(self, query: str, max_turns: int = 30):
        # 默认进入前脑对话流程，不占用“后端忙碌”状态
        # 仅在真正执行后端任务（工具/技能等）时再切到 busy=True
        self.busy = False
        self.mode = "front_chat"
        self.phase = "前脑回复"
        self.detail = "正在组织自然对话回复..."
        self.tool_history = []
        self.current_turn = 0
        self.max_turns = max_turns
        self.started_at = time.time()
        self.original_query = query

    def update(self, phase: str, detail: str = "", *, backend_busy: Optional[bool] = None):
        self.phase = phase
        self.detail = detail
        if backend_busy is not None:
            self.busy = backend_busy
            self.mode = "backend_task" if backend_busy else "front_chat"

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
        self.mode = "idle"
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
            f"🧠 当前模式: {'后脑任务' if self.busy else '前脑对话'}",
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
        self.pending_messages: Dict[str, List[Dict[str, Any]]] = {}
        
        # 快速模型（用于前端即时回复，轻量省 token）
        try:
            self.fast_llm = get_llm_client(model_alias="fast")
        except Exception:
            self.fast_llm = self.llm  # fallback 到主模型

        # 图片专用模型（可选）
        self.image_llm = None
        try:
            if config.get_model_name("image"):
                self.image_llm = get_llm_client(model_alias="image")
        except Exception:
            self.image_llm = None
        
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
        global_cfg = config.get_config() or {}
        cost_cfg = global_cfg.get("cost_tracking", {})
        self.cost_tracking_enabled = cost_cfg.get("enabled", True)
        if self.cost_tracking_enabled:
            custom_prices = cost_cfg.get("custom_prices", {})
            self.cost_tracker = get_cost_tracker(custom_prices=custom_prices)
            logger.info("成本跟踪已启用")
        else:
            self.cost_tracker = None
            logger.info("成本跟踪已禁用")
        
        logger.info(f"NoraController 已初始化。RAG: {'Online' if self.rag.enabled else 'Offline'}, MessageHistory: Online")

    def _select_generation_llm(self, text: str, default_alias: str = "smart"):
        """
        根据输入内容选择生成模型。
        - 检测到图片输入且 image 模型可用：返回 image
        - 否则返回 default_alias 对应模型（通常 smart/fast）
        """
        if has_image_input(text) and self.image_llm is not None:
            return self.image_llm, "image"

        if default_alias == "fast":
            return self.fast_llm, "fast"
        return self.llm, default_alias

    async def _start_routed_task(self, context: Dict[str, Any]):
        """所有消息先进前脑，由前脑自行判断是否需要后脑。"""
        chat_id = context["chat_id"]
        logger.info(f"[{chat_id}] 路由策略: 前脑优先 (front-brain-first)")
        task = asyncio.create_task(self._generate_front_chat_response(context))
        self.generation_tasks[chat_id] = task

    async def handle_new_message(self, context: Dict[str, Any]):
        """处理来自适配器的新消息/命令。"""
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")

        logger.debug(f"[{chat_id}] 收到新聚合消息: '{text[:50]}...'")
        
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

        # --- 前后端分离逻辑 ---
        status = self.worker_status.get(chat_id)
        if status and status.busy:
            # 后脑任务忙碌：用 fast 模型判断用户意图并生成回复
            logger.info(f"[{chat_id}] 后脑任务忙碌，分析用户意图...")
            
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

        # --- 正常流程：后端空闲，启动新任务 ---
        # 旧的抢占逻辑保留，以防万一有残留任务
        if chat_id in self.generation_tasks:
            logger.info(f"[{chat_id}] 抢占：取消正在进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        await self._start_routed_task(context)

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
                context = {
                    "chat_id": chat_id,
                    "user_id": chat_id, # Fallback, might need better user_id handling
                    "text": user_text,
                    "chat_type": "private" # Fallback
                }
                await self._start_routed_task(context)
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
                await self._start_routed_task(context)

    async def _generate_front_chat_response(self, context: Dict[str, Any]):
        """
        前脑优先路径：
        1. 先用快速模型生成自然对话回复，同时判断是否需要后脑（工具/技能）
        2. 若不需要后脑 → 直接发送回复，结束
        3. 若需要后脑 → 发送前脑回复（如"好的，马上处理~"）后启动后脑任务
        """
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")
        storage_id = chat_id if chat_type != "private" else user_id

        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        status = self.worker_status.setdefault(chat_id, WorkerStatus())
        status.start(query=text, max_turns=1)
        status.update("前脑回复", "正在组织自然对话回复...", backend_busy=False)

        generation_llm, generation_alias = self._select_generation_llm(text, default_alias="fast")

        final_response_buffer = ""
        try:
            if self.tui_callback:
                self.tui_callback("💬 Front chat reply...")

            # 保存用户消息
            message_content = f"{user_name}: {text}" if chat_type != "private" else text
            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="user",
                content=message_content,
                user_id=user_id,
            )

            # 轻量历史上下文
            db_context = self.message_history.get_context_messages("telegram", storage_id)
            temp_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in db_context[-12:]
                if msg["role"] in ("user", "assistant", "system")
            ]

            # 前脑 prompt：包含路由判断指令
            platform = getattr(self.adapter, 'platform_name', None) or 'telegram'
            system_prompt = get_system_prompt([
                "【前脑模式 — 路由判断】\n"
                "你是前脑，负责即时回复用户并判断是否需要启动后脑（工具/技能执行）。\n\n"
                "**规则：**\n"
                "1. **始终先回复用户** — 用自然、友好的语言直接回应用户的消息。\n"
                "2. **同时判断** 这条消息是否需要后脑介入（调用工具、执行技能、文件操作、命令执行等）。\n"
                "3. 如果需要后脑：在你的回复**末尾**附加标记 `[NEED_BACKEND]`。\n"
                "   - 例如：\"好的，让我来帮你查一下~ [NEED_BACKEND]\"\n"
                "   - 例如：\"马上处理 ✨ [NEED_BACKEND]\"\n"
                "4. 如果不需要后脑（纯聊天、问答、情绪回应等）：正常回复即可，不要加标记。\n"
                "5. **不要调用任何工具**，工具调用由后脑负责。\n"
                "6. **不要在回复中解释路由逻辑**，用户不需要知道前脑/后脑的存在。\n\n"
                "**需要后脑的典型场景：**\n"
                "- 用户要求执行操作（文件读写、代码修改、运行命令等）\n"
                "- 用户要求使用技能（搜索、下载、天气查询等）\n"
                "- 需要调用工具才能完成的任务\n"
                "- 用户要求创建、修改或分析文件/代码\n\n"
                "**不需要后脑的典型场景：**\n"
                "- 普通聊天、寒暄、情绪安慰\n"
                "- 知识问答（你能直接回答的）\n"
                "- 闲聊、讨论、观点交流\n"
                "- 对之前操作结果的追问（不需要新的工具调用时）"
            ], platform=platform)

            usage_data = None
            stream = generation_llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=text,
                history=temp_history,
                tools=[]
            )

            async for raw_chunk in stream:
                chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                if isinstance(chunk, dict):
                    if chunk.get("type") == "text":
                        final_response_buffer += chunk.get("content", "")
                    elif chunk.get("type") == "usage":
                        usage_data = {
                            "input_tokens": chunk.get("input_tokens", 0),
                            "output_tokens": chunk.get("output_tokens", 0),
                        }
                elif isinstance(chunk, str):
                    final_response_buffer += chunk

            if self.cost_tracking_enabled and self.cost_tracker and usage_data:
                provider = config.get_llm_provider()
                model = config.get_model_name(generation_alias) or config.get_model_name("fast")
                self.cost_tracker.log_usage(
                    provider=provider,
                    model=model,
                    input_tokens=usage_data["input_tokens"],
                    output_tokens=usage_data["output_tokens"],
                    model_alias=generation_alias,
                    context="front_chat"
                )

            # --- 解析前脑回复：提取路由信号 ---
            parsed = parse_front_brain_response(final_response_buffer)
            clean_response = _clean_tool_call_leaks(parsed["user_reply"]).strip()
            needs_backend = parsed["needs_backend"]

            if not clean_response:
                clean_response = "我在～你继续说。"

            # 发送前脑回复给用户
            await self.adapter.send_message(chat_id, clean_response)
            self.message_history.add_message(
                platform="telegram",
                chat_id=storage_id,
                role="assistant",
                content=clean_response,
                user_id="assistant",
            )

            session["history"] = temp_history + [{"role": "assistant", "content": clean_response}]

            # --- 路由决策：是否需要后脑 ---
            if needs_backend:
                logger.info(f"[{chat_id}] 前脑判断需要后脑介入，启动后脑任务")
                if self.tui_callback:
                    self.tui_callback("🧠 Front→Backend handoff...")
                # 先完成前脑状态清理
                status.finish()
                self.generation_tasks.pop(chat_id, None)
                # 启动后脑任务（context 不变，后脑能看到完整历史）
                backend_task = asyncio.create_task(self._generate_response(context))
                self.generation_tasks[chat_id] = backend_task
                return  # 前脑任务结束，后脑接管
            else:
                logger.info(f"[{chat_id}] 前脑直接回复完成，不需要后脑")

        except asyncio.CancelledError:
            status.finish()
            session["pending_text"] = text
            session["interrupted_thought"] = final_response_buffer if final_response_buffer else ""
            raise
        except Exception:
            logger.error(f"[{chat_id}] 前脑回复失败。", exc_info=True)
            try:
                await self.adapter.send_message(chat_id, "抱歉，刚刚没接住，你可以再说一次吗？")
            except Exception:
                pass
        finally:
            status.finish()
            if self.tui_callback:
                self.tui_callback("✅ Idle")
            self.generation_tasks.pop(chat_id, None)

            if not self._was_interrupted(chat_id):
                await self._process_pending_messages(chat_id)

    async def _generate_response(self, context: Dict[str, Any]):
        """内部生成逻辑。"""
        chat_id = context["chat_id"]
        user_id = context["user_id"]
        text = context["text"]
        chat_type = context.get("chat_type", "private")
        user_name = context.get("user_name", "User")

        # 确定用于存储和检索的唯一ID
        storage_id = chat_id if chat_type != "private" else user_id

        # Ensure session keys exist
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        logger.debug(f"[{chat_id}] 开始生成响应。History length: {len(session['history'])}")
        
        # --- 初始化后端状态追踪 ---
        MAX_TURNS = 30
        status = self.worker_status.setdefault(chat_id, WorkerStatus())
        status.start(query=text, max_turns=MAX_TURNS)
        generation_llm, generation_alias = self._select_generation_llm(text, default_alias="smart")
        
        try:
            if self.tui_callback: self.tui_callback(f"🏃 Handling new message...")
            
            # --- 0. 保存用户消息到数据库 ---
            status.update("保存消息", "正在保存用户消息...")
            message_content = f"{user_name}: {text}" if chat_type != "private" else text
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
                    f"⚠️ 执行技能时，**必须通过 function calling 机制**使用 execute_skill 工具（参数: skill_name, args_json）。\n"
                    f"**严禁**使用 exec_command 运行技能脚本。\n"
                    f"**严禁**在回复文本中写出调用语法——必须通过 function calling API 真正调用。"
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
                    f"【指令】请将我刚才未说完的思路与現在的新问题结合，给出一个连贯、完整的最终回复。不要让我感觉对话断裂了。"
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
            db_context = self.message_history.get_context_messages("telegram", storage_id)
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
                
                _tools_for_turn = [] if force_no_tools else self.tool_manager.get_tool_schemas()
                logger.debug(f"[{chat_id}] Passing {len(_tools_for_turn)} tool schemas to LLM")
                
                stream = generation_llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=full_user_prompt if current_turn == 1 else " (Continue processing tool outputs...)",
                    history=temp_history,
                    tools=_tools_for_turn
                )
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
                
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    # Handle new dict-based chunks
                    if isinstance(chunk, dict):
                        if chunk["type"] == "text":
                            content = chunk["content"]
                            
                            response_text_buffer += content
                            
                            # Real-time splitting logic
                            if "[SPLIT]" in response_text_buffer:
                                parts = response_text_buffer.split("[SPLIT]")
                                # Send and sync all complete parts
                                for part in parts[:-1]:
                                    text_to_send = part.strip()
                                    # 过滤掉包含工具调用语法的泄漏内容
                                    if text_to_send and not _is_tool_call_leak(text_to_send):
                                        await self.adapter.send_message(chat_id, text_to_send)
                                        # Important: keep final buffer synchronized
                                        temp_history.append({"role": "assistant", "content": text_to_send})
                                # Keep the last incomplete part in buffer
                                response_text_buffer = parts[-1]
                        
                        elif chunk["type"] == "tool_call":
                            tool_call = chunk
                            logger.debug(f"[{chat_id}] Received tool_call chunk from LLM: {chunk.get('name', '?')}")
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
                    model = config.get_model_name(generation_alias) or config.get_model_name("smart")
                    self.cost_tracker.log_usage(
                        provider=provider,
                        model=model,
                        input_tokens=usage_data["input_tokens"],
                        output_tokens=usage_data["output_tokens"],
                        model_alias=generation_alias,
                        context="chat"
                    )
                
                # Append text to final buffer (visible to user)
                if response_text_buffer:
                    final_response_buffer += response_text_buffer
                
                # --- Stream 结束后状态总结 ---
                logger.debug(
                    f"[{chat_id}] Turn {current_turn} stream ended: "
                    f"tool_call={'YES: ' + tool_call.get('name', '?') if tool_call else 'NO'}, "
                    f"text_len={len(final_response_buffer)}, "
                    f"usage={usage_data}"
                )
                
                # --- 泄漏拦截：如果 LLM 以文本输出了工具调用，尝试解析并真正执行 ---
                if not tool_call and final_response_buffer and _is_tool_call_leak(final_response_buffer):
                    parsed = _try_parse_leaked_tool_call(final_response_buffer)
                    if parsed:
                        logger.warning(
                            f"[{chat_id}] 检测到工具调用泄漏并成功解析: {parsed['name']}({parsed['args']}). "
                            f"将自动执行该工具调用。"
                        )
                        tool_call = {"type": "tool_call", "name": parsed["name"], "args": parsed["args"]}
                        # 清除泄漏文本，保留泄漏前的自然语言部分
                        natural_text = _clean_tool_call_leaks(final_response_buffer).strip()
                        final_response_buffer = natural_text
                        # 如果有自然语言前缀（如"好的，我来帮您修改~"），先发出去
                        if natural_text and not _is_tool_call_leak(natural_text):
                            await self.adapter.send_message(chat_id, natural_text)
                            temp_history.append({"role": "assistant", "content": natural_text})
                            final_response_buffer = ""
                    else:
                        # 解析失败，注入纠正提示让 LLM 重新用 function calling
                        logger.warning(
                            f"[{chat_id}] 检测到工具调用泄漏但无法解析。注入纠正提示。"
                        )
                        # 清理泄漏的文本
                        final_response_buffer = _clean_tool_call_leaks(final_response_buffer)
                        temp_history.append({
                            "role": "user",
                            "content": (
                                "【系统纠正】你刚才把工具调用以文本形式输出了，这是错误的！"
                                "用户看到了原始的调用代码。请使用 function calling 机制来真正调用工具，"
                                "不要在文本回复中写出 edit_file(...)、write_file(...) 等调用语法。"
                                "现在请重新执行你想做的操作——通过 function calling 真正调用工具。"
                            )
                        })
                        continue  # 让 LLM 重新生成
                
                # If tool call detected
                if tool_call:
                    tool_name = cast(Dict[str, Any], tool_call)["name"]
                    tool_args = cast(Dict[str, Any], tool_call)["args"]
                    if self.tui_callback: self.tui_callback(f"🔧 Executing Tool: {tool_name}")
                    logger.info(f"[{chat_id}] 🧠 AI Request Tool: {tool_name}({tool_args})")

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
                                    "请先 read_file 获取更完整上下文，并让 old_code 精确到目标片段。"
                                    "如果工具返回了 multiple matches，请根据返回的 [index] 重新调用 edit_file 并传入 match_index。"
                                    "除非你明确要重写整个文件，否则不要直接用 write_file 覆盖。"
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
                    status.update("工具调用", f"正在执行 {tool_name}...", backend_busy=True)
                    status.log_tool(tool_name, tool_args)
                    tool_result = await self.tool_manager.execute(tool_name, cast(Dict[str, Any], tool_args))
                    status.update("处理结果", f"{tool_name} 执行完毕，正在分析结果...", backend_busy=True)
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
                    
                    # Append history for tools — 使用结构化格式让 provider 正确转换
                    if current_turn == 1 and full_user_prompt not in [h.get('content', '') for h in temp_history]:
                        temp_history.append({"role": "user", "content": full_user_prompt})
                    
                    # 记录 tool_call（模型发出的调用请求）
                    temp_history.append({
                        "role": "tool_call",
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "content": f"Calling {tool_name}",
                    })
                    # 记录 tool_response（工具执行结果）
                    temp_history.append({
                        "role": "tool_response",
                        "tool_name": tool_name,
                        "content": truncated_result,
                    })
                    
                    # CRITICAL: Sync current progress back to the main session immediately to prevent state loss on preemption
                    session["history"] = list(temp_history)
                    
                    # Loop continues to let LLM process the result
                    continue
                else:
                    # No tool call -> Possibly final response, but check if LLM SHOULD have used tools
                    logger.debug(
                        f"[{chat_id}] Turn {current_turn} ended with no tool_call. "
                        f"Response buffer length: {len(final_response_buffer)}, "
                        f"preview: {final_response_buffer[:120]}..."
                    )
                    
                    # --- 工具使用自觉性检测 (Tool Usage Nudge) ---
                    # 仅在第一轮（LLM 首次回复时）检测：如果用户请求明显需要工具操作，
                    # 但 LLM 只给出了纯文本回复（如"建议您运行..."、给出代码块让用户自己跑），
                    # 则注入纠正提示让 LLM 重新生成并真正调用工具。
                    if current_turn == 1 and not session.get("_tool_nudge_done"):
                        should_nudge, nudge_reason = _should_nudge_tool_use(
                            user_text=text,
                            response_text=final_response_buffer,
                        )
                        if should_nudge:
                            logger.warning(
                                f"[{chat_id}] 检测到 LLM 应该使用工具但未调用 "
                                f"(reason={nudge_reason}). 注入纠正提示。"
                            )
                            session["_tool_nudge_done"] = True  # 只纠正一次，防止无限循环
                            # 把 LLM 的纯文本回复作为"思考过程"放入历史，但不发送给用户
                            if final_response_buffer.strip():
                                temp_history.append({
                                    "role": "assistant",
                                    "content": final_response_buffer
                                })
                            final_response_buffer = ""  # 清空，等 LLM 重新生成
                            temp_history.append({
                                "role": "user",
                                "content": (
                                    "【系统纠正】你刚才只用文本描述了你要做的操作，但没有真正执行！"
                                    "你拥有完整的工具权限（read_file, write_file, edit_file, exec_command, "
                                    "execute_skill 等）。请**立即通过 function calling 真正调用工具来执行操作**，"
                                    "不要只是告诉用户该怎么做。\n"
                                    "记住：你是执行者，不是建议者。用户期望你直接完成任务。"
                                )
                            })
                            continue  # 让 LLM 带着纠正提示重新生成
                    
                    break
            
            # If loop exhausted MAX_TURNS with pending tool work, inject a wrap-up prompt and do one final LLM call
            if current_turn >= MAX_TURNS and not final_response_buffer:
                logger.warning(f"[{chat_id}] Tool loop exhausted {MAX_TURNS} turns. Requesting LLM summary.")
                temp_history.append({
                    "role": "user",
                    "content": "【系统提示】工具调用轮次已达上限，请立即基于已有的工具结果，给用户一个简洁的最终回复。"
                })
                stream = generation_llm.chat_stream(
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
                # 过滤掉可能泄漏的工具调用语法（多种格式）
                clean_response = _clean_tool_call_leaks(final_response_buffer)
                if clean_response:
                    await self.adapter.send_message(chat_id, clean_response)
                elif latest_meaningful_output:
                    # 整条消息都是工具语法泄漏，用工具结果做 fallback
                    pass  # 由下面的 elif 分支处理
            elif latest_meaningful_output:
                # Fallback: LLM stayed silent; do one final LLM call to summarize the tool output
                temp_history.append({
                    "role": "user",
                    "content": "【系统提示】工具已执行完毕，请根据以上工具输出，用自然语言总结执行结果。不要直接输出原始命令行内容。"
                })
                stream = generation_llm.chat_stream(
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
                stream = generation_llm.chat_stream(
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
                    chat_id=storage_id,
                    role="assistant",
                    content=final_response_buffer,
                    user_id="assistant"
                )
            
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
        await self._start_routed_task(combined_context)

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

