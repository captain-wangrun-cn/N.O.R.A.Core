import asyncio
import logging
from typing import Dict, Any

from platforms.base import BaseAdapter
from brain.llm import llm_client
from brain.prompts import get_system_prompt
from memory.rag import RAGEngine

logger = logging.getLogger(__name__)

class NoraController:
    """处理机器人的核心业务逻辑。"""

    def __init__(self, adapter: BaseAdapter):
        self.adapter = adapter
        self.llm = llm_client
        self.rag = RAGEngine() # Initialize RAG Engine
        self.sessions: Dict[str, Any] = {}
        self.generation_tasks: Dict[str, asyncio.Task] = {}
        logger.info(f"NoraController 已初始化。RAG 状态: {'Online' if self.rag.enabled else 'Offline'}")

    async def handle_new_message(self, chat_id: str, text: str):
        """处理来自聚合器的新消息/命令。"""
        logger.info(f"[{chat_id}] 收到新聚合消息: '{text[:100]}...'")
        
        # 特殊处理 /start 命令
        if text.strip() == "/start":
            self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
            await self.adapter.send_message(chat_id, "N.O.R.A. Core 已启动。")
            logger.info(f"[{chat_id}] Session已重置 by /start command.")
            return

        # --- 抢占逻辑 ---
        if chat_id in self.generation_tasks:
            logger.warning(f"[{chat_id}] 抢占：取消正在进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        task = asyncio.create_task(self._generate_response(chat_id, text))
        self.generation_tasks[chat_id] = task

    async def _generate_response(self, chat_id: str, text: str):
        """内部生成逻辑。"""
        # Ensure session keys exist
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        logger.info(f"[{chat_id}] 开始生成响应。History length: {len(session['history'])}")
        
        try:
            await self.adapter.start_typing(chat_id)
            
            # --- 1. RAG 记忆检索 (Memory Retrieval) ---
            # 只在没有被抢占的情况下检索，或者对最新的 text 进行检索
            rag_context = ""
            if self.rag.enabled:
                logger.info(f"[{chat_id}] 正在从大脑检索相关记忆...")
                rag_context = self.rag.get_context_string(text, top_k=3)
                if rag_context:
                    logger.info(f"[{chat_id}] RAG 命中！检索到 {len(rag_context.splitlines())} 条相关记忆。")
                else:
                    logger.info(f"[{chat_id}] RAG 未检索到强相关记忆。")

            # --- 2. 构建 Prompt (Context Reconstruction) ---
            instructions = []
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

            # --- 3. 流式生成 ---
            response_buffer = ""
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=full_user_prompt,
                history=session["history"]
            )
            
            async for chunk in stream:
                response_buffer += chunk
            
            logger.info(f"[{chat_id}] 流式生成完成。")
            
            # --- 4. 发送响应 ---
            await self.adapter.send_message(chat_id, response_buffer)
            
            # --- 5. RAG 记忆存储 (Memory Storage) ---
            # 异步执行，不阻塞主流程
            if self.rag.enabled:
                # 存储用户的输入
                asyncio.create_task(self._async_save_memory(text, {"role": "user", "chat_id": chat_id}))
                # 存储 AI 的回复
                asyncio.create_task(self._async_save_memory(response_buffer, {"role": "assistant", "chat_id": chat_id}))

            # Store the FULL context (including the merged pending text) so future memory retrieval works
            session["history"].append({"role": "user", "content": full_user_prompt})
            session["history"].append({"role": "assistant", "content": response_buffer})
            
            if len(session["history"]) > 20:
                session["history"] = session["history"][-20:]
            logger.info(f"[{chat_id}] History updated. New length: {len(session['history'])}")

        except asyncio.CancelledError:
            # --- 抢占发生：抢救现场 ---
            # 1. Save the User's input that triggered this specific task (it hasn't been added to history yet!)
            session["pending_text"] = text
            
            # 2. Save the AI's partial thought
            if 'response_buffer' in locals() and response_buffer:
                session["interrupted_thought"] = response_buffer
                logger.info(f"[{chat_id}] 抢占成功：保存了部分思路 ('{response_buffer[:30]}...') 和用户输入。")
            else:
                session["interrupted_thought"] = "" # Nothing generated yet
                logger.info(f"[{chat_id}] 抢占成功：尚未生成内容，仅保存了用户输入。")
        
        except Exception as e:
            logger.error(f"[{chat_id}] 生成响应时出现严重错误。", exc_info=True)
            await self.adapter.send_message(chat_id, "抱歉，处理您的请求时出现内部错误。")
            
        finally:
    async def _async_save_memory(self, text: str, metadata: Dict[str, Any]):
        """异步保存记忆，避免阻塞主线程。"""
        try:
            # 由于 RAG.add_memory 是同步的 (requests 是同步的)，我们需要在 executor 中运行它
            # 或者如果 RAG 内部实现了异步更好。目前先简单用 to_thread
            await asyncio.to_thread(self.rag.add_memory, text, metadata)
            logger.info(f"[{metadata.get('chat_id')}] 记忆已异步保存 (Role: {metadata.get('role')})")
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

