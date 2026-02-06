import asyncio
from typing import Dict, Any

from platforms.base import BaseAdapter
from brain.llm import llm_client
from brain.prompts import get_system_prompt

class NoraController:
    """处理机器人的核心业务逻辑。"""

    def __init__(self, adapter: BaseAdapter):
        self.adapter = adapter
        self.llm = llm_client
        self.sessions: Dict[str, Any] = {}
        self.generation_tasks: Dict[str, asyncio.Task] = {}
        print("NoraController 已初始化。")

    async def handle_new_message(self, chat_id: str, text: str):
        """处理来自聚合器的新消息/命令。"""
        # 特殊处理 /start 命令
        if text.strip() == "/start":
            self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
            await self.adapter.send_message(chat_id, "N.O.R.A. Core 已启动。")
            return

        # --- 抢占逻辑 ---
        if chat_id in self.generation_tasks:
            print(f"抢占：取消正在为 {chat_id} 进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            await asyncio.sleep(0.1)

        task = asyncio.create_task(self._generate_response(chat_id, text))
        self.generation_tasks[chat_id] = task

    async def _generate_response(self, chat_id: str, text: str):
        """内部生成逻辑。"""
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": ""})
        
        try:
            await self.adapter.start_typing(chat_id)
            
            # --- 构建 Prompt ---
            instructions = []
            # 简单的启发式规则，判断是否为聚合消息
            if "\n" in text.strip(): 
                instructions.append("请仔细阅读并依次回答以下所有问题，不要遗漏。")

            full_user_prompt = text
            if session["interrupted_thought"]:
                full_user_prompt = f"（我之前的思路：{session['interrupted_thought']}）\n\n---\n\n{text}"
                session["interrupted_thought"] = "" 
            
            system_prompt = get_system_prompt(instructions)

            # --- 流式生成 ---
            response_buffer = ""
            stream = self.llm.chat_stream(
                system_prompt=system_prompt,
                user_prompt=full_user_prompt,
                history=session["history"]
            )
            
            async for chunk in stream:
                response_buffer += chunk
            
            # --- 正常完成 -> 发送 & 更新记忆 ---
            await self.adapter.send_message(chat_id, response_buffer)
            # 使用聚合后的完整 text 作为 user history
            session["history"].append({"role": "user", "content": text})
            session["history"].append({"role": "assistant", "content": response_buffer})
            
            # 简单历史截断
            if len(session["history"]) > 20:
                session["history"] = session["history"][-20:]

        except asyncio.CancelledError:
            if 'response_buffer' in locals() and response_buffer:
                session["interrupted_thought"] = response_buffer
                print(f"抢占成功：为 {chat_id} 保存了部分思路: '{response_buffer[:50]}...'")
        
        except Exception as e:
            print(f"生成响应时出错: {e}")
            await self.adapter.send_message(chat_id, "抱歉，处理您的请求时出现内部错误。")
            
        finally:
            self.generation_tasks.pop(chat_id, None)
