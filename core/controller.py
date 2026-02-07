import asyncio
import logging
from typing import Dict, Any, Callable, Optional, cast

from platforms.base import BaseAdapter
from brain.llm import llm_client
from brain.prompts import get_system_prompt
from memory.rag import RAGEngine
from brain.tools import ToolManager
from skills.loader import SkillLoader
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
        logger.info(f"NoraController 已初始化。RAG: {'Online' if self.rag.enabled else 'Offline'}")

    async def handle_new_message(self, chat_id: str, text: str):
        """处理来自聚合器的新消息/命令。"""
        # 降级为 DEBUG，避免刷屏
        logger.debug(f"[{chat_id}] 收到新聚合消息: '{text[:50]}...'")
        
        # 特殊处理 /start 命令
        if text.strip().startswith("/start"):
            self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
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
            await self.adapter.start_typing(chat_id)
            
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
            
            # Inject Skills
            skills = self.skill_loader.scan_skills()
            if skills:
                skill_desc = "\n".join([f"- {s['name']}: {s['description']} (Path: {s.get('path', 'N/A')})" for s in skills])
                instructions.append(f"【可用技能 (Available Skills)】\n{skill_desc}\n\n你可以使用 `exec_command` 来运行这些技能脚本 (e.g., `python3 skills/xxx/skill.py`)。")

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
            MAX_TURNS = 15
            current_turn = 0
            final_response_buffer = ""
            latest_tool_output = ""  # Safeguard: fallback to show tool output if LLM stays silent
            
            # 临时历史，用于多轮工具调用，最后合并入主历史
            temp_history = list(session["history"]) 

            while current_turn < MAX_TURNS:
                current_turn += 1
                if self.tui_callback: self.tui_callback(f"🤔 Thinking... (Turn {current_turn}/{MAX_TURNS})")
                logger.debug(f"[{chat_id}] Turn {current_turn}/{MAX_TURNS}")
                
                stream = self.llm.chat_stream(
                    system_prompt=system_prompt,
                    user_prompt=full_user_prompt if current_turn == 1 else " (Continue processing tool outputs...)", # Subsequent prompts handled via history
                    history=temp_history,
                    tools=self.tool_manager.get_tool_schemas()
                )
                
                response_text_buffer = ""
                tool_call = None
                
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
                                    if text_to_send:
                                        await self.adapter.send_message(chat_id, text_to_send)
                                        # Important: keep final buffer synchronized
                                        temp_history.append({"role": "assistant", "content": text_to_send})
                                # Keep the last incomplete part in buffer
                                response_text_buffer = parts[-1]
                        
                        elif chunk["type"] == "tool_call":
                            tool_call = chunk
                            # IMMEDIATELY clear text buffer if tool call is detected, to prevent leaking thoughts.
                            if response_text_buffer:
                                # final_response_buffer += response_text_buffer # This was the source of the leak
                                response_text_buffer = ""
                    elif isinstance(chunk, str):
                        # Fallback for legacy providers
                        response_text_buffer += chunk
                
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
                    # Track by tool NAME (not exact args) to catch semantically repeated calls
                    # e.g. execute_skill with different keyword variants
                    last_tool_name = session.get("last_tool_name")
                    
                    if last_tool_name and last_tool_name == tool_name:
                        session["tool_call_loop_counter"] = session.get("tool_call_loop_counter", 0) + 1
                    else:
                        session["tool_call_loop_counter"] = 0
                    
                    session["last_tool_name"] = tool_name

                    if session["tool_call_loop_counter"] >= 3:
                        logger.warning(f"[{chat_id}] 检测到工具调用循环: {tool_name} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在强制中断。")
                        if tool_name == "read_file":
                            # For read-only tools, inject guidance to use a different approach instead of breaking
                            temp_history.append({
                                "role": "user",
                                "content": (
                                    "【系统提示】你已经多次读取同一个文件了。如果你需要修改它，请直接使用 write_file 写入完整的新内容，"
                                    "不要再尝试 read_file。如果你已经完成了所有修改，请直接给用户回复最终结果。"
                                )
                            })
                            session["tool_call_loop_counter"] = 0  # Reset to allow continued operation
                            continue  # Don't break, let LLM try a different approach
                        else:
                            # For other tools, break the loop
                            temp_history.append({"role": "user", "content": "【系统提示】检测到重复的工具调用。请停止当前操作，并重新评估下一步。如果是在检查目录，请直接开始创建操作。"})
                            break # Exit the while loop
                    
                    
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
                await self.adapter.send_message(chat_id, final_response_buffer)
            elif latest_tool_output:
                # Fallback: LLM stayed silent after tool calls; at least surface the latest tool output
                await self.adapter.send_message(chat_id, f"【工具结果】\n{latest_tool_output}")
            
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

            # Update session history with the multi-turn conversation from temp_history
            session["history"] = temp_history
            if final_response_buffer:
                 # Append the final text response if it wasn't part of a tool call history
                 if not any(final_response_buffer in str(h.get('content', '')) for h in session["history"]):
                    session["history"].append({"role": "assistant", "content": final_response_buffer})
            elif latest_tool_output:
                session["history"].append({"role": "assistant", "content": f"【工具结果】\n{latest_tool_output}"})

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

