import asyncio
from typing import Dict, Any

from platforms.base import BaseAdapter
from brain.llm import llm_client
from brain.prompts import NORA_SOUL_PROMPT

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
        # --- 抢占逻辑 ---
        if chat_id in self.generation_tasks:
            print(f"抢占：取消正在为 {chat_id} 进行的生成任务。")
            self.generation_tasks[chat_id].cancel()
            # 等待任务真正取消
            await asyncio.sleep(0.1)

        # 为这个新的请求创建一个生成任务
        task = asyncio.create_task(self._generate_response(chat_id, text))
        self.generation_tasks[chat_id] = task

    async def _generate_response(self, chat_id: str, text: str):
        """内部生成逻辑。"""
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": ""})
        
        try:
            await self.adapter.start_typing(chat_id)
            
            # --- 构建 Prompt ---
            if session["interrupted_thought"]:
                # 如果有被打断的思路，加入到上下文中
                full_user_prompt = f"（我之前的思路：{session['interrupted_thought']}）\n\n---\n\n{text}"
                session["interrupted_thought"] = "" # 清空
            else:
                full_user_prompt = text

            # --- 流式生成 ---
            response_buffer = ""
            stream = self.llm.chat_stream(
                system_prompt=NORA_SOUL_PROMPT,
                user_prompt=full_user_prompt,
                history=session["history"]
            )
            
            async for chunk in stream:
                response_buffer += chunk
            
            # --- 正常完成 -> 发送 & 更新记忆 ---
            await self.adapter.send_message(chat_id, response_buffer)
            session["history"].append({"role": "user", "content": text})
            session["history"].append({"role": "assistant", "content": response_buffer})
            
            # 简单历史截断
            if len(session["history"]) > 20:
                session["history"] = session["history"][-20:]

        except asyncio.CancelledError:
            # 任务被抢占，保存已生成的思路
            if 'response_buffer' in locals() and response_buffer:
                session["interrupted_thought"] = response_buffer
                print(f"抢占成功：为 {chat_id} 保存了部分思路: '{response_buffer[:50]}...'")
            # 不再发送 "typing" 状态
        
        except Exception as e:
            print(f"生成响应时出错: {e}")
            await self.adapter.send_message(chat_id, "抱歉，处理您的请求时出现内部错误。")
            
        finally:
            self.generation_tasks.pop(chat_id, None)

