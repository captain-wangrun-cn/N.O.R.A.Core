'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:33:48
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""后脑 Mixin — _generate_response 工具循环 + 文本处理工具方法。"""

import asyncio
import logging
import os
import re
import time
from typing import Dict, Any, List, cast

from brain.prompts import get_system_prompt, render_template, load_custom_prompt, should_inject_custom
from brain.multimodal import extract_image_payloads
from core.worker_status import WorkerStatus
import config

logger = logging.getLogger(__name__)


class BackBrainMixin:
    """后脑工具循环执行 + 文本清洗工具方法。"""

    _TIMESTAMP_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*")

    # ------------------------------------------------------------------
    # 文本清洗 class methods
    # ------------------------------------------------------------------

    @classmethod
    def _strip_thinking_content(cls, text: str) -> str:
        """移除模型可能泄漏的思维链标签内容（如 <think>...</think>）和图片标签块。"""
        if not text:
            return ""
        cleaned = cls._THINK_BLOCK_PATTERN.sub("", text)
        cleaned = cls._THINK_INLINE_PATTERN.sub("", cleaned)
        # 移除 IMAGE_TAGS 块（不应展示给用户，仅后台存储用）
        cleaned = cls._IMAGE_TAGS_PATTERN.sub("", cleaned)
        # 移除 IMAGE_OCR 块（不应展示给用户，仅后台存储用）
        cleaned = cls._IMAGE_OCR_PATTERN.sub("", cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        return cleaned

    @classmethod
    def _strip_timestamp_markers(cls, text: str) -> str:
        """移除消息中的自动时间戳标记，避免泄漏到用户侧。"""
        if not text:
            return ""
        return cls._TIMESTAMP_PATTERN.sub("", text).strip()

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

    # ------------------------------------------------------------------
    # 后脑主循环
    # ------------------------------------------------------------------

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
        base_model_alias = "image" if multimodal_images else "coder"
        active_llm = self.image_llm if base_model_alias == "image" else self.coder_llm
        
        try:
            if self.tui_callback: self.tui_callback(f"🏃 Handling new message...")
            
            # --- 0. 保存用户消息到数据库 ---
            # 如果前脑已经保存过，跳过重复写入
            front_brain_handled = context.get("_front_brain_handled", False)
            message_saved = context.get("_message_saved", False)
            status.update("保存消息", "正在保存用户消息...")
            message_content = f"{user_name}: {text}" if chat_type != "private" else text
            if not front_brain_handled and not message_saved:
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

            # 前脑下发的任务指示（用户不可见），提供给后脑执行
            task_instruction = context.get("task_instruction", "")
            if task_instruction:
                instructions.append(
                    "【前脑任务指示 (Task Instruction)】\n"
                    "以下指示来自前脑，用户不可见，请严格按此执行：\n"
                    f"{task_instruction}\n"
                )
            
            # Inject Workspace Context
            from brain.tools import WORKSPACE_ROOT, SKILLS_DIR, CODE_SKILLS_DIR
            workspace_info = render_template('context_injection.jinja', 'workspace',
                                             workspace_root=WORKSPACE_ROOT,
                                             skills_dir=CODE_SKILLS_DIR,
                                             downloads_dir=os.path.join(WORKSPACE_ROOT, 'downloads'))
            instructions.append(workspace_info)
            
            # Inject Skills
            skills = self.skill_loader.scan_skills()
            if skills:
                skill_desc = "\n".join([f"- {s['name']}: {s['description']} (Path: {s.get('path', 'N/A')})" for s in skills])
                instructions.append(
                    render_template('context_injection.jinja', 'skills', skill_desc=skill_desc)
                )

            # Inject Tools
            try:
                from brain.tools import TOOL_INTROS
                if TOOL_INTROS:
                    tool_desc = "\n".join([f"- {name}: {intro}" for name, intro in TOOL_INTROS.items()])
                    instructions.append(
                        render_template('context_injection.jinja', 'tools', tool_desc=tool_desc)
                    )
            except Exception:
                logger.warning("工具简介注入失败，已忽略。", exc_info=True)

            if "\n" in text.strip(): 
                instructions.append(render_template('context_injection.jinja', 'multiline'))
            
            # Inject RAG context into instructions or system prompt
            if rag_context:
                instructions.append(
                    render_template('context_injection.jinja', 'rag', rag_context=rag_context)
                )

            # Inject CUSTOM.md based on scope configuration
            try:
                custom_scope = "image" if multimodal_images else "smart"
                if should_inject_custom(custom_scope):
                    custom_prompt = load_custom_prompt()
                    if custom_prompt:
                        instructions.append(
                            render_template('context_injection.jinja', 'custom', custom_content=custom_prompt)
                        )
            except Exception:
                logger.warning("后脑 CUSTOM 注入失败，已忽略。", exc_info=True)

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
            
            # --- 前脑上下文注入（让后脑知道当前对话段落和前脑的回复） ---
            if front_brain_handled:
                # 获取当前活跃对话段落（session_id IS NULL 的消息）
                segment_messages = self.message_history.get_current_segment_messages(
                    "telegram", storage_id
                )
                if segment_messages:
                    # 格式化段落内容（排除当前轮次的用户消息，因为已作为 user_prompt 传入）
                    segment_lines = []
                    for msg in segment_messages:
                        if msg["role"] in ("user", "assistant"):
                            # 跳过当前轮已作为 user_prompt 的消息
                            if msg["content"] == message_content and msg == segment_messages[-1]:
                                continue
                            segment_lines.append(f"{msg['role']}: {msg['content']}")
                    
                    if segment_lines:
                        segment_text = "\n".join(segment_lines)
                        instructions.append(
                            f"【当前对话段落 (Current Conversation Segment)】\n"
                            f"以下是本轮对话段落中的完整交互记录（包含前脑的即时回复），"
                            f"请基于此上下文继续工作：\n\n{segment_text}\n\n"
                            f"⚠️ 前脑已经给用户发送了即时回复，不要重复前脑已说过的内容。"
                            f"直接执行任务，完成后汇报结果即可。"
                        )
                    else:
                        # 段落为空但前脑有回复，至少告知后脑前脑说了什么
                        front_reply = context.get("_front_brain_reply", "")
                        if front_reply:
                            instructions.append(
                                f"【前脑已回复 (Front-Brain Reply)】\n"
                                f"在你接手之前，前脑已经给用户发送了以下即时回复：\n"
                                f"「{front_reply}」\n"
                                f"请基于此继续工作，不要重复前脑已经说过的内容。直接执行任务，完成后汇报结果即可。"
                            )
                else:
                    # 没有段落消息时，回退到简单的前脑回复注入
                    front_reply = context.get("_front_brain_reply", "")
                    if front_reply:
                        instructions.append(
                            f"【前脑已回复 (Front-Brain Reply)】\n"
                            f"在你接手之前，前脑已经给用户发送了以下即时回复：\n"
                            f"「{front_reply}」\n"
                            f"请基于此继续工作，不要重复前脑已经说过的内容。直接执行任务，完成后汇报结果即可。"
                        )

            # --- 轮询模式指令注入 ---
            # 在轮询模式下，后脑不直接与用户沟通，而是产出详细专业的工作报告
            # 由前脑审查后决定如何回复用户
            if context.get("_in_polling_loop", False):
                instructions.append(
                    "【轮询模式 (Polling Mode)】\n"
                    "你当前处于**轮询模式**，你的输出将由前脑审查后再转达给用户。\n"
                    "⚠️ 重要规则：\n"
                    "1. **禁止调用 report_progress** — 进度由前脑管理。\n"
                    '2. **不要写用户向的寒暄/过渡语** — 不要说"好的我来帮你"、"搜到了哦~"之类的话。\n'
                    "3. **你的最终回复应是详细、专业的工作报告**，包含：\n"
                    "   - 执行了什么操作（哪些工具/技能）\n"
                    "   - 得到了什么结果（关键数据/文件路径/搜索结果）\n"
                    "   - 是否完全完成了用户请求\n"
                    "   - 如有异常或部分失败，说明原因\n"
                    "4. 保持客观、简洁、信息密度高。前脑会将你的报告转化为自然对话回复用户。"
                )

            system_prompt = get_system_prompt(
                instructions,
                platform=self.adapter.platform_name,
                custom_scope="coder" if not multimodal_images else "image",
            )

            # --- 3. 执行循环 (Tool Execution Loop) ---
            current_turn = 0
            final_response_buffer = ""
            latest_tool_output = ""  # Safeguard: fallback to show tool output if LLM stays silent
            latest_meaningful_output = ""  # Only meaningful outputs (execute_skill, etc.)
            force_no_tools = False  # 循环检测触发后，下一轮禁用工具
            last_turn_model_alias = base_model_alias
            last_turn_llm = active_llm
            
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

            # 注入当前对话段落的完整消息序列，确保后脑拥有完整上下文（避免缺段）
            try:
                segment_messages = self.message_history.get_current_segment_messages(
                    "telegram", storage_id
                )
                if segment_messages:
                    segment_lines = []
                    for msg in segment_messages:
                        if msg["role"] in ("user", "assistant"):
                            # 跳过当前轮已作为 user_prompt 的最后一条用户消息，避免重复
                            if msg["content"] == message_content and msg == segment_messages[-1]:
                                continue
                            segment_lines.append(f"{msg['role']}: {msg['content']}")
                    if segment_lines:
                        segment_text = "\n".join(segment_lines)
                        instructions.append(
                            "【当前对话段落 (Current Conversation Segment)】\n"
                            "以下是本轮对话段落中的完整交互记录（含前脑已回复的内容，如有），"
                            "请基于此上下文继续工作：\n\n"
                            f"{segment_text}\n\n"
                            "⚠️ 不要重复前脑已说过的内容，直接执行任务并汇报。"
                        )
            except Exception:
                logger.warning(f"[{chat_id}] 注入当前对话段落失败，继续执行", exc_info=True)

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
                # --- 抢占检查点：每轮开始前检查是否被取消 ---
                await asyncio.sleep(0)  # 让出控制权，允许 CancelledError 传播
                current_turn += 1
                status.current_turn = current_turn
                status.update("思考中", f"第 {current_turn}/{MAX_TURNS} 轮推理...")
                if self.tui_callback: self.tui_callback(f"🤔 Thinking... (Turn {current_turn}/{MAX_TURNS})")
                logger.debug(f"[{chat_id}] Turn {current_turn}/{MAX_TURNS}")
                await self._send_debug(chat_id, f"💭 开始第 {current_turn}/{MAX_TURNS} 轮推理...")
                
                turn_multimodal_images = multimodal_images if current_turn == 1 else pending_tool_multimodal_images
                turn_model_alias = "image" if turn_multimodal_images else base_model_alias
                turn_llm = self.image_llm if turn_model_alias == "image" else self.coder_llm
                last_turn_model_alias = turn_model_alias
                last_turn_llm = turn_llm
                # 构造本轮 user_prompt：工具返回图片时追加提示（这些是回查的旧图，不要生成 IMAGE_TAGS）
                turn_user_prompt = full_user_prompt if current_turn == 1 else " (Continue processing tool outputs...)"
                if current_turn > 1 and turn_multimodal_images and any(ti.get("from_tool") for ti in turn_multimodal_images):
                    turn_user_prompt += "\n\n（注意：以下图片是通过 view_image 工具回查的已有图片，**不要**生成 [IMAGE_TAGS] 标签，直接分析图片内容即可。）"
                stream = self._chat_stream_wrapper(
                    turn_llm,
                    chat_id,
                    system_prompt=system_prompt,
                    user_prompt=turn_user_prompt,
                    history=temp_history,
                    tools=[] if force_no_tools else self.tool_manager.get_tool_schemas(),
                    multimodal_images=turn_multimodal_images if turn_multimodal_images else None,
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
                    provider_name = config.get_model_provider(last_turn_model_alias)
                    provider = config.get_provider_type(provider_name)
                    model = config.get_model_name(last_turn_model_alias)
                    self.cost_tracker.log_usage(
                        provider=provider,
                        model=model,
                        input_tokens=usage_data["input_tokens"],
                        output_tokens=usage_data["output_tokens"],
                        model_alias=last_turn_model_alias,
                        context="chat"
                    )
                
                # Append text to final buffer (visible to user)
                if response_text_buffer:
                    final_response_buffer += response_text_buffer
                
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
                    _file_tools = {"write_file", "edit_file", "read_file", "create_new_skill", "list_dir", "search"}
                    if tool_name in _file_tools and isinstance(tool_args, dict):
                        _target = tool_args.get("path", tool_args.get("skill_name", ""))
                        if tool_name == "edit_file":
                            loop_key = f"edit_file:{_target}"
                        else:
                            loop_key = f"{tool_name}:{_target}"
                    elif tool_name == "execute_skill" and isinstance(tool_args, dict):
                        loop_key = f"execute_skill:{tool_args.get('skill_name', '')}"
                    else:
                        loop_key = tool_name
                    
                    # Also track cumulative edits to the same file
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
                            file_edit_counts[edit_target] = 0
                            force_no_tools = True
                            continue
                    
                    last_loop_key = session.get("last_loop_key")
                    
                    if last_loop_key and last_loop_key == loop_key:
                        session["tool_call_loop_counter"] = session.get("tool_call_loop_counter", 0) + 1
                    else:
                        session["tool_call_loop_counter"] = 0
                    
                    session["last_loop_key"] = loop_key

                    if session["tool_call_loop_counter"] >= 2:
                        _high_tolerance_tools = {"execute_skill"}
                        _threshold = 5 if tool_name in _high_tolerance_tools else 2

                        if session["tool_call_loop_counter"] < _threshold:
                            pass
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
                            logger.warning(f"[{chat_id}] 检测到工具调用循环: {loop_key} 已连续调用 {session['tool_call_loop_counter']+1} 次。正在引导总结。")
                            temp_history.append({
                                "role": "user",
                                "content": render_template('loop_interventions.jinja', 'generic_tool_loop')
                            })
                            session["tool_call_loop_counter"] = 0
                            force_no_tools = True
                            continue
                    
                    
                    # --- 抢占检查点：工具执行前检查取消 ---
                    await asyncio.sleep(0)
                    
                    # Execute Tool
                    status.preview_next(tool_name, tool_args)
                    status.update("工具调用", f"正在执行 {tool_name}...")
                    status.log_tool(tool_name, tool_args)
                    if tool_name == "view_image" and isinstance(tool_args, dict):
                        if not str(tool_args.get("user_id", "")).strip():
                            tool_args["user_id"] = storage_id
                    # --- report_progress 拦截 ---
                    if tool_name == "report_progress" and isinstance(tool_args, dict):
                        progress_msg = str(tool_args.get("message", "")).strip()
                        in_polling = context.get("_in_polling_loop", False)
                        if progress_msg and not in_polling:
                            try:
                                await self.adapter.send_message(chat_id, f"⏳ {progress_msg}", parse_media=False)
                            except Exception as e:
                                logger.debug(f"[{chat_id}] 发送进度消息失败: {e}")
                            tool_result = "Progress message sent to user."
                        elif progress_msg and in_polling:
                            logger.info(f"[{chat_id}] [轮询模式] 后脑进度: {progress_msg}")
                            tool_result = "Progress noted. (Polling mode: message will be reviewed by front-brain, not sent directly.)"
                        else:
                            tool_result = "No message provided."
                    else:
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
                                for ti in tool_images:
                                    ti["from_tool"] = True
                                pending_tool_multimodal_images = tool_images
                                logger.info(f"[{chat_id}] {tool_name} 返回 {len(tool_images)} 张图片，下一轮切换 image 模型进行分析。")
                        except Exception:
                            logger.warning(f"[{chat_id}] 解析 {tool_name} 返回图片失败，已跳过多模态注入。", exc_info=True)
                    status.update("处理结果", f"{tool_name} 执行完毕，正在分析结果...")
                    # --- 抢占检查点：工具执行后检查取消 ---
                    await asyncio.sleep(0)
                    logger.info(f"[{chat_id}] 🔧 Tool Result for {tool_name}: {tool_result[:100]}...")
                    result_preview = tool_result[:500] if len(tool_result) > 500 else tool_result
                    await self._send_debug(chat_id, f"📋 {tool_name} 结果 ({len(tool_result)} 字符):\n{result_preview}")

                    # --- TRUNCATION SAFETY VALVE ---
                    TRUNCATE_LIMIT = 8000 if tool_name == "read_file" else 4000
                    if len(tool_result) > TRUNCATE_LIMIT:
                        total_len = len(tool_result)
                        truncated_result = tool_result[:TRUNCATE_LIMIT]
                        if tool_name == "read_file":
                            shown_lines = truncated_result.count('\n') + 1
                            total_lines = tool_result.count('\n') + 1
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
                    latest_tool_output = truncated_result
                    _user_facing_tools = {"execute_skill", "create_new_skill"}
                    _is_error = any(kw in truncated_result[:100] for kw in ["Error:", "REFUSED:", "failed", "not found"])
                    if tool_name in _user_facing_tools and not _is_error:
                        latest_meaningful_output = truncated_result
                    
                    # Append history for tools
                    if current_turn == 1 and full_user_prompt not in [h['content'] for h in temp_history]:
                        temp_history.append({"role": "user", "content": full_user_prompt})
                    
                    temp_history.append({"role": "user", "content": f"【Tool Output for {tool_name}】\n{truncated_result}"})
                    
                    # CRITICAL: Sync current progress back to the main session
                    session["history"] = list(temp_history)
                    
                    continue
                else:
                    # No tool call -> Final response
                    break
            
            # If loop exhausted MAX_TURNS with pending tool work
            if current_turn >= MAX_TURNS and not final_response_buffer:
                logger.warning(f"[{chat_id}] Tool loop exhausted {MAX_TURNS} turns. Requesting LLM summary.")
                temp_history.append({
                    "role": "user",
                    "content": render_template('loop_interventions.jinja', 'turns_exhausted')
                })
                stream = self._chat_stream_wrapper(
                    last_turn_llm,
                    chat_id,
                    system_prompt=system_prompt,
                    user_prompt=render_template('loop_interventions.jinja', 'summarize_turns_exhausted'),
                    history=temp_history,
                    tools=[],
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
            in_polling = context.get("_in_polling_loop", False)
            
            # 先提取图片标签（在任何清理之前），供后续存储
            image_tags_extracted: Dict[str, str] = {}
            image_ocr_extracted: Dict[str, str] = {}
            parsed_tag_blocks: List[tuple[str, str]] = []
            parsed_ocr_blocks: List[tuple[str, str]] = []
            if multimodal_images and final_response_buffer:
                for m in self._IMAGE_TAGS_PATTERN.finditer(final_response_buffer):
                    img_id = m.group(1).strip()
                    tags_text = m.group(2).strip()
                    if img_id and tags_text:
                        image_tags_extracted[img_id] = tags_text
                        parsed_tag_blocks.append((img_id, tags_text))
                for m in self._IMAGE_OCR_PATTERN.finditer(final_response_buffer):
                    img_id = m.group(1).strip()
                    ocr_text = m.group(2).strip()
                    if img_id and ocr_text and ocr_text != "（无文字）":
                        image_ocr_extracted[img_id] = ocr_text
                        parsed_ocr_blocks.append((img_id, ocr_text))

                # LLM 可能输出占位符/错误 image_id（如 img_xxxxxxxx）。
                # 若出现这种情况，按图片顺序重映射到本轮真实 image_id，避免标签丢失回退为文件名。
                expected_ids = [img["image_id"] for img in multimodal_images]
                expected_set = set(expected_ids)

                if parsed_tag_blocks:
                    for idx, expected_id in enumerate(expected_ids):
                        if expected_id in image_tags_extracted:
                            continue
                        if idx >= len(parsed_tag_blocks):
                            break
                        parsed_id, parsed_tags = parsed_tag_blocks[idx]
                        if parsed_id not in expected_set and parsed_tags:
                            image_tags_extracted[expected_id] = parsed_tags
                            logger.warning(
                                f"[{chat_id}] IMAGE_TAGS image_id 不匹配，已按顺序重映射: "
                                f"{parsed_id} -> {expected_id}"
                            )

                if parsed_ocr_blocks:
                    for idx, expected_id in enumerate(expected_ids):
                        if expected_id in image_ocr_extracted:
                            continue
                        if idx >= len(parsed_ocr_blocks):
                            break
                        parsed_id, parsed_ocr = parsed_ocr_blocks[idx]
                        if parsed_id not in expected_set and parsed_ocr:
                            image_ocr_extracted[expected_id] = parsed_ocr
                            logger.warning(
                                f"[{chat_id}] IMAGE_OCR image_id 不匹配，已按顺序重映射: "
                                f"{parsed_id} -> {expected_id}"
                            )
                logger.debug(f"[{chat_id}] 已提取 {len(image_tags_extracted)} 个图片标签块，{len(image_ocr_extracted)} 个 OCR 文字块。")

            if final_response_buffer:
                clean_response = re.sub(
                    r'(?:execute_skill|execute_tool_plan|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill|search)\s*\([^)]*\)',
                    '', final_response_buffer
                ).strip()
                clean_response = re.sub(
                    r'返回结果[：:]\s*```[\s\S]*?```', '', clean_response
                ).strip()
                clean_response = self._strip_thinking_content(clean_response)
                clean_response = self._strip_timestamp_markers(clean_response)
                if clean_response:
                    if not in_polling:
                        await self.adapter.send_message(chat_id, self._strip_timestamp_markers(clean_response))
                    else:
                        logger.info(f"[{chat_id}] [轮询模式] 后脑回复已缓存，等待前脑审查 ({len(clean_response)} 字符)")
                elif latest_meaningful_output:
                    pass
            elif latest_meaningful_output:
                temp_history.append({
                    "role": "user",
                    "content": render_template('loop_interventions.jinja', 'llm_silent_meaningful')
                })
                stream = self._chat_stream_wrapper(
                    last_turn_llm,
                    chat_id,
                    system_prompt=system_prompt,
                    user_prompt=render_template('loop_interventions.jinja', 'summarize_meaningful'),
                    history=temp_history,
                    tools=[],
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
                
                final_response_buffer = self._strip_thinking_content(final_response_buffer)
                final_response_buffer = self._strip_timestamp_markers(final_response_buffer)
                if final_response_buffer:
                    if not in_polling:
                        await self.adapter.send_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
                else:
                    final_response_buffer = "任务已完成。"
                    if not in_polling:
                        await self.adapter.send_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
            elif latest_tool_output:
                temp_history.append({
                    "role": "user",
                    "content": render_template('loop_interventions.jinja', 'llm_silent_fallback',
                                               tool_output=latest_tool_output[:1000])
                })
                stream = self._chat_stream_wrapper(
                    last_turn_llm,
                    chat_id,
                    system_prompt=system_prompt,
                    user_prompt=render_template('loop_interventions.jinja', 'summarize_fallback'),
                    history=temp_history,
                    tools=[],
                )
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        final_response_buffer += chunk["content"]
                    elif isinstance(chunk, str):
                        final_response_buffer += chunk
                
                final_response_buffer = self._strip_thinking_content(final_response_buffer)
                final_response_buffer = self._strip_timestamp_markers(final_response_buffer)
                if final_response_buffer:
                    if not in_polling:
                        await self.adapter.send_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
                else:
                    final_response_buffer = "任务已完成。"
                    if not in_polling:
                        await self.adapter.send_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
            
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
                        tags = f"用户发送的图片: {os.path.basename(img['path'])}"
                    ocr_text = image_ocr_extracted.get(img_id, "")
                    asyncio.create_task(
                        self._async_save_image_metadata(
                            image_id=img_id,
                            file_path=img["path"],
                            tags=tags,
                            user_id=storage_id,
                            chat_id=chat_id,
                            ocr_text=ocr_text,
                        )
                    )
                    ocr_info = f", ocr_text='{ocr_text[:40]}...'" if ocr_text else ""
                    logger.info(f"[{chat_id}] 图片 {img_id} 标签已提取并入库: '{tags[:60]}...'{ocr_info}")            
            # --- 5. RAG 记忆存储 (Memory Storage) ---
            if self.rag.enabled:
                asyncio.create_task(self._async_save_memory(message_content, storage_id, {"role": "user", "chat_id": chat_id}))
                
                full_assistant_turn = " ".join([h['content'] for h in temp_history if h['role'] == 'assistant'])
                if final_response_buffer and final_response_buffer not in full_assistant_turn:
                    full_assistant_turn += " " + final_response_buffer
                
                if full_assistant_turn.strip():
                     asyncio.create_task(self._async_save_memory(full_assistant_turn.strip(), storage_id, {"role": "assistant", "chat_id": chat_id}))

            # Update session history
            session["history"] = [
                h for h in temp_history
                if h.get("role") in ("user", "assistant", "system")
                and not str(h.get("content", "")).startswith("【Tool Output for ")
            ]
            if final_response_buffer:
                 if not any(final_response_buffer in str(h.get('content', '')) for h in session["history"]):
                    session["history"].append({"role": "assistant", "content": final_response_buffer})

            if len(session["history"]) > 20:
                session["history"] = session["history"][-20:]
            logger.debug(f"[{chat_id}] History updated. New length: {len(session['history'])}")

        except asyncio.CancelledError:
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
                pass
            
        finally:
            status.finish()
            self._mark_scheduler_idle(chat_id)
            if self.tui_callback: self.tui_callback("✅ Idle")
            self.generation_tasks.pop(chat_id, None)
            logger.debug(f"[{chat_id}] 本次生成任务结束。")
            
            in_polling = context.get("_in_polling_loop", False)
            if not in_polling and not self._was_interrupted(chat_id):
                await self._process_pending_messages(chat_id)
