import asyncio
import logging
import os
import re
from typing import Dict, Any, Callable, Optional, cast

from adapters.base import BaseAdapter
from brain.llm import llm_client
from brain.prompts import get_system_prompt
from memory.rag import RAGEngine
from memory.message_history import MessageHistory
from brain.tools import ToolManager
from skills.loader import SkillLoader
from core.cost_tracker import get_cost_tracker
import config
import json

logger = logging.getLogger(__name__)

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
        # 降级为 DEBUG，避免刷屏
        logger.debug(f"[{chat_id}] 收到新聚合消息: '{text[:50]}...'")
        
        # 特殊处理 /start 命令
        if text.strip().startswith("/start"):
            self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
            self.message_history.clear_chat_history("telegram", chat_id, keep_pinned=True)
            await self.adapter.send_message(chat_id, "N.O.R.A. Core 已启动。")
            logger.info(f"[{chat_id}] Session已重置 by /start command.")
            return

        # --- 抢占逻辑 ---
        if chat_id in self.generation_tasks:
            logger.info(f"[{chat_id}] 抢占：取消正在进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        task = asyncio.create_task(self._generate_response(chat_id, text))
        self.generation_tasks[chat_id] = task

    async def _generate_response(self, chat_id: str, text: str):
        """内部生成逻辑。"""
        # Ensure session keys exist
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        # 降级为 DEBUG
        logger.debug(f"[{chat_id}] 开始生成响应。History length: {len(session['history'])}")
        
        try:
            if self.tui_callback: self.tui_callback(f"🏃 Handling new message...")
            # 初始不显示 typing，等待判断是否需要工具调用
            
            # --- 0. 保存用户消息到数据库 ---
            self.message_history.add_message(
                platform="telegram",
                chat_id=chat_id,
                role="user",
                content=text
            )
            
            # --- 1. RAG 记忆检索 (Memory Retrieval) ---
            # 只在没有被抢占的情况下检索，或者对最新的 text 进行检索
            rag_context = ""
            if self.rag.enabled:
                if self.tui_callback: self.tui_callback("🧠 Retrieving memories (RAG)...")
                logger.debug(f"[{chat_id}] 正在从大脑检索相关记忆...")
                rag_context = self.rag.get_context_string(text, top_k=2)
                if rag_context:
                    # 保留 INFO，但精简内容
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
            MAX_TURNS = 30
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

            while current_turn < MAX_TURNS:
                current_turn += 1
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
                                    "【系统提示】你已经多次读取同一个文件了。如果你需要修改它，请直接使用 write_file 写入完整的新内容，"
                                    "不要再尝试 read_file。如果你已经完成了所有修改，请直接给用户回复最终结果。"
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
                        else:
                            # 其他工具（含 execute_skill 达到高阈值后）
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在引导总结。")
                            temp_history.append({
                                "role": "user",
                                "content": "【系统提示】你已经多次重复调用同一个工具。请停止重复调用，基于已有的结果直接给用户回复。"
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue  # 不 break，让 LLM 生成总结
                    
                    
                    # Execute Tool
                    tool_result = await self.tool_manager.execute(tool_name, cast(Dict[str, Any], tool_args))
                    logger.info(f"[{chat_id}] 🔧 Tool Result for {tool_name}: {tool_result[:100]}...")

                    # --- TRUNCATION SAFETY VALVE ---
                    # read_file needs higher limit to avoid LLM re-reading in loops
                    TRUNCATE_LIMIT = 8000 if tool_name == "read_file" else 4000
                    if len(tool_result) > TRUNCATE_LIMIT:
                        truncated_result = tool_result[:TRUNCATE_LIMIT] + f"\n\n... (Output truncated from {len(tool_result)} chars. Use write_file to overwrite the full file if needed.)"
                        logger.warning(f"Tool output for {tool_name} was truncated from {len(tool_result)} chars.")
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
            # --- 抢占发生：抢救现场 ---
            # 1. Save the User's input that triggered this specific task (it hasn't been added to history yet!)
            session["pending_text"] = text
            
            # 2. Save the AI's partial thought
            if 'final_response_buffer' in locals() and final_response_buffer:
                session["interrupted_thought"] = final_response_buffer
                logger.info(f"[{chat_id}] 抢占成功：保存了部分思路 ('{final_response_buffer[:30]}...') 和用户输入。")
            else:
                session["interrupted_thought"] = "" # Nothing generated yet
                logger.info(f"[{chat_id}] 抢占成功：尚未生成内容，仅保存了用户输入。")
        
        except Exception as e:
            if self.tui_callback: self.tui_callback(f"❌ Error: {e.__class__.__name__}")
            logger.error(f"[{chat_id}] 生成响应时出现严重错误。", exc_info=True)
            await self.adapter.send_message(chat_id, "抱歉，处理您的请求时出现内部错误。")
            
        finally:
            if self.tui_callback: self.tui_callback("✅ Idle")
            self.generation_tasks.pop(chat_id, None)
            logger.debug(f"[{chat_id}] 本次生成任务结束。")

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

