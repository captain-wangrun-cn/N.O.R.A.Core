'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:26:35
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""后端工作状态追踪 + 后脑任务队列。"""

import asyncio
import logging
import os
import time
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class WorkerStatus:
    """后端工作状态追踪器 —— 记录当前正在做什么，供前端查询。"""

    # 工具名 → 用户可读的描述
    _TOOL_DESCRIPTIONS = {
        "execute_skill": "执行技能",
        "read_file": "读取文件",
        "write_file": "写入文件",
        "edit_file": "编辑文件",
        "search": "搜索代码",
        "list_dir": "浏览目录",
        "exec_command": "执行命令",
        "create_new_skill": "创建新技能",
        "execute_tool_plan": "执行多步骤计划",
        "view_image": "检索图片",
        "crop_image_for_llm": "裁剪图片",
        "set_alarm": "设置闹钟",
        "get_available_skills": "查看可用技能",
    }

    def __init__(self):
        self.busy: bool = False
        self.phase: str = ""          # 当前阶段名 (e.g. "RAG检索", "工具调用")
        self.detail: str = ""         # 更详细描述 (e.g. "正在执行 execute_skill(web_search)")
        self.tool_history: List[str] = []  # 本次任务执行过的工具步骤摘要
        self.tool_history_readable: List[str] = []  # 用户可读的步骤描述
        self.key_results: List[str] = []   # 关键结果信息（路径/ID/主输出摘要）
        self.current_turn: int = 0
        self.max_turns: int = 0
        self.started_at: float = 0.0
        self.original_query: str = ""  # 用户最初问的问题
        self.task_instruction: str = ""  # 前脑下达给后脑的任务指示（供前脑下轮上下文比对，避免重复触发）
        self.next_action: str = ""     # 预告：下一步准备做什么

    def start(self, query: str, max_turns: int = 30, task_instruction: str = ""):
        self.busy = True
        self.phase = "初始化"
        self.detail = "正在准备处理请求..."
        self.tool_history = []
        self.tool_history_readable = []
        self.key_results = []
        self.current_turn = 0
        self.max_turns = max_turns
        self.started_at = time.time()
        self.original_query = query
        self.task_instruction = task_instruction or ""
        self.next_action = ""

    def update(self, phase: str, detail: str = ""):
        self.phase = phase
        self.detail = detail

    def preview_next(self, tool_name: str, tool_args: Any):
        """预告下一步要做什么（在工具执行前调用）。"""
        readable = self._TOOL_DESCRIPTIONS.get(tool_name, tool_name)
        if isinstance(tool_args, dict):
            # 提取关键参数增加可读性
            if tool_name == "execute_skill":
                skill = tool_args.get("skill_name", "")
                self.next_action = f"准备{readable}: {skill}"
            elif tool_name in ("read_file", "write_file", "edit_file"):
                path = tool_args.get("path", "")
                self.next_action = f"准备{readable}: {os.path.basename(path) if path else '...'}"
            elif tool_name == "exec_command":
                cmd = str(tool_args.get("command", ""))[:40]
                self.next_action = f"准备{readable}: {cmd}"
            elif tool_name == "search":
                query = tool_args.get("query", "")
                self.next_action = f"准备{readable}: {query[:40]}"
            else:
                self.next_action = f"准备{readable}"
        else:
            self.next_action = f"准备{readable}"
        self.phase = "工具调用"
        self.detail = self.next_action

    def log_tool(self, tool_name: str, tool_args: Any):
        """记录一次工具调用的简略描述。"""
        if isinstance(tool_args, dict):
            # 只取关键参数做摘要
            short_args = {k: (str(v)[:60] + "..." if len(str(v)) > 60 else str(v))
                          for k, v in list(tool_args.items())[:3]}
            self.tool_history.append(f"{tool_name}({short_args})")
        else:
            self.tool_history.append(f"{tool_name}(...)")
        
        # 用户可读版本
        readable = self._TOOL_DESCRIPTIONS.get(tool_name, tool_name)
        if isinstance(tool_args, dict):
            if tool_name == "execute_skill":
                self.tool_history_readable.append(f"{readable}: {tool_args.get('skill_name', '...')}")
            elif tool_name in ("read_file", "write_file", "edit_file"):
                path = tool_args.get("path", "")
                self.tool_history_readable.append(f"{readable}: {os.path.basename(path) if path else '...'}")
            else:
                self.tool_history_readable.append(readable)
        else:
            self.tool_history_readable.append(readable)

    def finish(self):
        self.busy = False
        self.phase = "完成"
        self.detail = ""
        self.next_action = ""

    def get_summary(self) -> str:
        """生成一段给用户看的进度概括。"""
        if not self.busy:
            return ""
        elapsed = int(time.time() - self.started_at)
        lines = [
            f"📌 正在处理: \"{self.original_query[:80]}\"",
            f"⏱ 已用时 {elapsed} 秒 | 阶段: {self.phase}",
        ]
        if self.task_instruction:
            lines.append(f"📝 任务指示: {self.task_instruction[:160]}")
        if self.detail:
            lines.append(f"🔧 {self.detail}")
        if self.next_action:
            lines.append(f"⏳ 下一步: {self.next_action}")
        if self.tool_history_readable:
            recent = self.tool_history_readable[-3:]  # 最近3步
            lines.append("📝 最近步骤:")
            for step in recent:
                lines.append(f"  • {step}")
        lines.append(f"🔄 进度: 第 {self.current_turn}/{self.max_turns} 轮")
        return "\n".join(lines)


class BackendTaskQueue:
    """后脑任务队列 — 支持任务排队、依次执行、状态查询。"""

    def __init__(self):
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._items: List[Dict[str, Any]] = []  # 有序列表，用于状态查询
        self._lock = asyncio.Lock()

    async def enqueue(self, context: Dict[str, Any]):
        """入队一个后脑任务。"""
        item = {
            "context": context,
            "text": context.get("text", ""),
            "enqueued_at": time.time(),
        }
        async with self._lock:
            self._items.append(item)
        await self._queue.put(item)

    async def dequeue(self) -> Optional[Dict[str, Any]]:
        """出队一个任务（如果队列为空则返回 None）。"""
        try:
            item = self._queue.get_nowait()
            async with self._lock:
                if item in self._items:
                    self._items.remove(item)
            return item
        except asyncio.QueueEmpty:
            return None

    async def dequeue_all(self) -> List[Dict[str, Any]]:
        """取出所有排队任务并清空队列。"""
        items = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                items.append(item)
            except asyncio.QueueEmpty:
                break
        async with self._lock:
            self._items.clear()
        return items

    def size(self) -> int:
        return self._queue.qsize()

    def get_queue_summary(self) -> str:
        """获取队列概况，供用户/前脑查看。"""
        if not self._items:
            return ""
        lines = [f"📬 排队任务 ({len(self._items)} 个):"]
        for i, item in enumerate(self._items, 1):
            text_preview = item["text"][:60]
            wait_secs = int(time.time() - item["enqueued_at"])
            ctx = item.get("context") or {}
            instr = str(ctx.get("task_instruction", "")).strip()
            line = f"  {i}. \"{text_preview}\" (等待 {wait_secs}s)"
            if instr:
                # 任务指示是前脑给后脑的执行说明，让后脑/前脑判定时能比对
                line += f"\n     指示: {instr[:120]}"
            lines.append(line)
        return "\n".join(lines)

    async def remove_by_index(self, index: int) -> Optional[str]:
        """
        按序号（1-based）删除特定排队任务。
        返回被删除任务的文本摘要，失败返回 None。
        """
        async with self._lock:
            if index < 1 or index > len(self._items):
                return None
            removed = self._items.pop(index - 1)
        # 重建 asyncio.Queue 以保持同步
        await self._rebuild_queue()
        return removed["text"][:60]

    async def _rebuild_queue(self):
        """根据 _items 列表重建内部 asyncio.Queue。"""
        # 清空旧队列
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # 按顺序重新入队
        async with self._lock:
            for item in self._items:
                await self._queue.put(item)

    async def clear(self):
        """清空队列。"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        async with self._lock:
            self._items.clear()
