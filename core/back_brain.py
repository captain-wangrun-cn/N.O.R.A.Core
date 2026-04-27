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
import traceback
from typing import Dict, Any, List, Optional, cast

from brain.prompts import (
    get_system_prompt,
    render_template,
    load_custom_prompt,
    should_inject_custom,
    get_lazy_lexicon_user_prompt_block,
)
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
        cleaned = cls._THINK_BLOCK_PATTERN.sub("", text)  # type: ignore[attr-defined]
        cleaned = cls._THINK_INLINE_PATTERN.sub("", cleaned)  # type: ignore[attr-defined]
        # 移除 IMAGE_TAGS 块（不应展示给用户，仅后台存储用）
        cleaned = cls._IMAGE_TAGS_PATTERN.sub("", cleaned)  # type: ignore[attr-defined]
        # 移除 IMAGE_OCR 块（不应展示给用户，仅后台存储用）
        cleaned = cls._IMAGE_OCR_PATTERN.sub("", cleaned)  # type: ignore[attr-defined]
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

        for marker in cls._SPLIT_MARKER_PATTERN.finditer(buffer):  # type: ignore[attr-defined]
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
        provided_images = context.get("multimodal_images") or []
        clean_text, extracted_images = extract_image_payloads(text)
        multimodal_images = provided_images or extracted_images
        if clean_text:
            text = clean_text

        # 图片先占位入库（无标签，不写向量），待模型回复后再补标签
        if multimodal_images and self.image_store.enabled:
            for img in multimodal_images:
                if img.get("from_tool"):
                    continue
                asyncio.create_task(
                    self._async_save_image_stub(
                        image_id=img["image_id"],
                        file_path=img["path"],
                        user_id=user_id,
                        chat_id=chat_id,
                        platform=context.get("platform", "telegram"),
                        platform_message_id=context.get("platform_message_id"),
                    )
                )

        # 确定用于存储和检索的唯一ID
        storage_id = chat_id if chat_type != "private" else user_id

        # Ensure session keys exist
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        logger.debug(f"[{chat_id}] 开始生成响应。History length: {len(session['history'])}")
        
        # --- 初始化后端状态追踪 ---
        MAX_TURNS = 30
        status = self.worker_status.setdefault(chat_id, WorkerStatus())
        status.start(
            query=text,
            max_turns=MAX_TURNS,
            task_instruction=context.get("task_instruction", ""),
        )
        
        # 通知 scheduler: 后端开始忙碌
        self._update_scheduler_state(chat_id, busy=True)

        response_platform_ids: List[str] = []

        # 根据输入类型选择模型：图片消息优先走 image 模型，其余走 coder 模型
        force_image_model_until_crop_done = bool(context.get("use_image_model", False))
        base_model_alias = "image" if (multimodal_images or force_image_model_until_crop_done) else "coder"
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
                user_metadata = {}
                platform_msg_id = context.get("platform_message_id")
                if platform_msg_id:
                    user_metadata["platform_message_ids"] = [str(platform_msg_id)]

                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="user",
                    content=message_content,
                    user_id=user_id,
                    metadata=user_metadata or None,
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
                    if not img.get("from_tool"):
                        image_id_lines.append(f"- 图片 ID: {img['image_id']}  文件: {os.path.basename(img['path'])}")
                
                if image_id_lines:
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

            # 懒加载词库命中：仅命中词条注入 user prompt
            lazy_lexicon_block = get_lazy_lexicon_user_prompt_block(text)
            if lazy_lexicon_block:
                full_user_prompt = f"{full_user_prompt}\n\n{lazy_lexicon_block}"
            
            # --- 前脑上下文注入（告知后脑前脑的回复） ---
            if front_brain_handled:
                front_reply = context.get("_front_brain_reply", "")
                if front_reply:
                    instructions.append(
                        f"【前脑已回复 (Front-Brain Reply)】\n"
                        f"在你接手之前，前脑（你较快的一个处理分支）已经给用户发送了以下即时回复：\n"
                        f"「{front_reply}」\n"
                        f"请基于此继续工作，绝对不要重复前脑已经说过的内容。直接执行长耗时任务，完成后汇报结果即可。"
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

            # 避免重复注入：清理历史尾部与当前轮次重合的消息
            # 可能的尾部结构： [user(当前消息)], 或 [user(当前消息), assistant(前脑/忙碌回复)]
            if db_context:
                # 先弹出无用的“忙碌/前脑/已排队”的助手回复
                while db_context and db_context[-1].get("role") == "assistant" and (
                    front_brain_handled or context.get("_message_saved")
                ):
                    db_context.pop()
                
                # 再看看末部的是不是刚好就是本轮要处理的用户消息
                if db_context and db_context[-1].get("role") == "user" and str(db_context[-1].get("content", "")) == message_content:
                    db_context.pop()

            temp_history = [
                {"role": msg["role"], "content": msg["content"]}
                for msg in db_context
                if msg["role"] in ("user", "assistant", "system")
            ]

            # Typing 状态跟踪（在所有 turns 中保持）
            typing_started = False
            # 若本轮包含“新图片”（非工具回查图片），先缓存文本，待 IMAGE_TAGS 校验/重试完成后再统一发送
            defer_user_send_until_image_tags_ready = any(not img.get("from_tool") for img in multimodal_images)
            # 工具回查图片（view_image return_image=true）注入到下一轮多模态输入
            pending_tool_multimodal_images: List[Dict[str, Any]] = []
            # 若上游要求强制使用图片模型，保持标记直到 crop_image_for_llm 首轮输出
            force_image_model_until_crop_done = bool(force_image_model_until_crop_done)
            in_polling_mode = context.get("_in_polling_loop", False)
            no_reply = context.get("no_reply", False)
            # 记录最后一次 image 回合的原始输出，便于检测空输出/标签结构异常
            last_image_raw_output: Optional[str] = None
            
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
                # 如果上游工具要求使用 image 模型，则强制切换（直到 crop 轮返回首个响应后清除）
                if force_image_model_until_crop_done:
                    turn_model_alias = "image"
                else:
                    turn_model_alias = "image" if turn_multimodal_images else base_model_alias
                turn_llm = self.image_llm if turn_model_alias == "image" else self.coder_llm
                last_turn_model_alias = turn_model_alias
                last_turn_llm = turn_llm
                image_turn_raw_parts: List[str] = [] if turn_model_alias == "image" else None
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
                deferred_split_parts_emitted = False
                
                async for raw_chunk in stream:
                    chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                    # Handle new dict-based chunks
                    if isinstance(chunk, dict):
                        if chunk["type"] == "text":
                            content = chunk["content"]
                            
                            response_text_buffer += content
                            if image_turn_raw_parts is not None:
                                image_turn_raw_parts.append(content)
                            
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
                                        # 轮询模式 / no_reply / 图片标签待校验 时均不直接发送，先缓冲
                                        if not in_polling_mode and not no_reply and not defer_user_send_until_image_tags_ready:
                                            msg_id = await self.adapter.send_message(chat_id, text_to_send)
                                            if msg_id:
                                                response_platform_ids.append(str(msg_id))
                                        elif defer_user_send_until_image_tags_ready and not in_polling_mode and not no_reply:
                                            # 图片标签校验期间延迟发送：保留 SPLIT 边界，避免最终被拼成整段
                                            if final_response_buffer:
                                                final_response_buffer += "[SPLIT]"
                                            final_response_buffer += text_to_send
                                            deferred_split_parts_emitted = True
                                        else:
                                            # 将分段汇总到最终结果，后续统一发送（或由前脑审查）
                                            if final_response_buffer:
                                                final_response_buffer += "\n\n"
                                            final_response_buffer += text_to_send
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
                        if image_turn_raw_parts is not None:
                            image_turn_raw_parts.append(chunk)
                
                # Debug: 在 image 模型回合输出完整原始内容，便于排查标签缺失
                if turn_model_alias == "image" and self.debug_mode.get(chat_id, False):
                    raw_image_text = "".join(image_turn_raw_parts) if image_turn_raw_parts else ""
                    last_image_raw_output = raw_image_text
                    await self._send_debug(chat_id, f"[image turn {current_turn}] raw output:\n{raw_image_text or '(empty)'}")
                elif turn_model_alias == "image":
                    raw_image_text = "".join(image_turn_raw_parts) if image_turn_raw_parts else ""
                    last_image_raw_output = raw_image_text

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
                    if (
                        defer_user_send_until_image_tags_ready
                        and not in_polling_mode
                        and not no_reply
                        and deferred_split_parts_emitted
                        and final_response_buffer
                    ):
                        final_response_buffer += "[SPLIT]"
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
                        if progress_msg and not in_polling and not no_reply:
                            try:
                                await self.adapter.send_message(chat_id, f"⏳ {progress_msg}", parse_media=False)
                                self.message_history.add_message(
                                    platform="telegram",
                                    chat_id=storage_id,
                                    role="assistant",
                                    content=f"⏳ {progress_msg}",
                                    user_id="assistant",
                                )
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

                    # 如果工具要求强制使用 image 模型，则标记，直到 crop_image_for_llm 第一轮输出后清除
                    if tool_name == "view_image" and isinstance(tool_args, dict) and bool(tool_args.get("use_image_model", False)):
                        force_image_model_until_crop_done = True
                        # 如果返回了 MediaTag，已在上方提取；若未提取到但有 file_path，可直接注入
                        if not pending_tool_multimodal_images and isinstance(tool_result, str):
                            try:
                                _, inferred_images = extract_image_payloads(tool_result)
                                if inferred_images:
                                    for ti in inferred_images:
                                        ti["from_tool"] = True
                                    pending_tool_multimodal_images = inferred_images
                            except Exception:
                                logger.debug(f"[{chat_id}] use_image_model 标记下未能注入图片，多模态输入保持为空。", exc_info=True)

                    # 在 crop_image_for_llm 回合的首个文本输出后，解除强制 image 模型
                    if force_image_model_until_crop_done and tool_name == "crop_image_for_llm":
                        force_image_model_until_crop_done = False
                    status.update("处理结果", f"{tool_name} 执行完毕，正在分析结果...")
                    # --- 抢占检查点：工具执行后检查取消 ---
                    await asyncio.sleep(0)
                    logger.info(f"[{chat_id}] 🔧 Tool Result for {tool_name}: {tool_result[:100]}...")
                    result_preview = tool_result[:500] if len(tool_result) > 500 else tool_result
                    await self._send_debug(chat_id, f"📋 {tool_name} 结果 ({len(tool_result)} 字符):\n{result_preview}")

                    # 记录关键结果：保留路径/ID/摘要，供前脑总结
                    try:
                        if isinstance(tool_result, str):
                            snippet = tool_result.strip()
                            if snippet:
                                if len(snippet) > 400:
                                    snippet = snippet[:397] + "..."
                                status.key_results.append(f"{tool_name}: {snippet}")
                    except Exception:
                        logger.debug("记录关键结果失败，已忽略", exc_info=True)

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
            image_tags_failed = False
            
            # 先提取图片标签（在任何清理之前），供后续存储
            image_tags_extracted: Dict[str, str] = {}
            image_ocr_extracted: Dict[str, str] = {}
            parsed_tag_blocks: List[tuple[str, str]] = []
            parsed_ocr_blocks: List[tuple[str, str]] = []
            
            # 若有新图片才要求tags，由搜索查找到的历史图片不需要生成标签
            expected_ids = [img["image_id"] for img in multimodal_images if not img.get("from_tool")]
            
            if expected_ids or multimodal_images:
                source_text_for_tags = final_response_buffer or last_image_raw_output or ""
                for m in self._IMAGE_TAGS_PATTERN.finditer(source_text_for_tags):
                    img_id = m.group(1).strip()
                    tags_text = m.group(2).strip()
                    if img_id and tags_text:
                        image_tags_extracted[img_id] = tags_text
                        parsed_tag_blocks.append((img_id, tags_text))
                for m in self._IMAGE_OCR_PATTERN.finditer(source_text_for_tags):
                    img_id = m.group(1).strip()
                    ocr_text = m.group(2).strip()
                    if img_id and ocr_text and ocr_text != "（无文字）":
                        image_ocr_extracted[img_id] = ocr_text
                        parsed_ocr_blocks.append((img_id, ocr_text))

                # LLM 可能输出占位符/错误 image_id（如 img_xxxxxxxx）。
                # 若出现这种情况，按图片顺序重映射到本轮真实 image_id，避免标签丢失回退为文件名。
                # 仅重映射新图片，跳过历史工具图片 (from_tool)
                expected_ids = [img["image_id"] for img in multimodal_images if not img.get("from_tool")]
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

                def _find_tag_issues():
                    missing = [img_id for img_id in expected_ids if not image_tags_extracted.get(img_id)]
                    bad = [img_id for img_id, tags in image_tags_extracted.items() if "," not in tags]
                    return missing, bad

                def _detect_tag_structure_issue(text: str) -> bool:
                    if not text:
                        return False
                    lower = text.lower()
                    has_marker = ("[image_tags" in lower) or ("[/image_tags" in lower)
                    tokens = list(re.finditer(r"\[/?image_tags[^\]]*\]", text, re.IGNORECASE))
                    if not has_marker:
                        return False
                    if not tokens:
                        return True
                    balance = 0
                    for token in tokens:
                        if token.group(0).startswith("[/"):
                            balance -= 1
                        else:
                            balance += 1
                        if balance < 0:
                            return True
                    return balance != 0

                missing_tag_ids, bad_comma_ids = _find_tag_issues()
                image_output_empty = bool(multimodal_images and (last_image_raw_output is not None) and not str(last_image_raw_output).strip())
                tag_structure_invalid = _detect_tag_structure_issue(source_text_for_tags)
                no_tags_generated = bool(expected_ids) and not image_tags_extracted

                retry_attempt = 0
                MAX_TAG_RETRY = 2
                need_tag_retry = bool(missing_tag_ids or bad_comma_ids or tag_structure_invalid or no_tags_generated or image_output_empty)

                while need_tag_retry and retry_attempt < MAX_TAG_RETRY:
                    retry_attempt += 1
                    reasons = []
                    if missing_tag_ids:
                        reasons.append(f"缺失 {len(missing_tag_ids)} 个")
                    if bad_comma_ids:
                        reasons.append(f"格式异常 {len(bad_comma_ids)} 个")
                    if tag_structure_invalid:
                        reasons.append("标签结构未闭合/多余")
                    if image_output_empty:
                        reasons.append("输出为空")
                    if no_tags_generated and not missing_tag_ids:
                        reasons.append("未生成任何标签")
                    reason_text = "，".join(reasons) or "未知原因"
                    await self._send_debug(
                        chat_id,
                        f"⚠️ IMAGE_TAGS 异常（{reason_text}），正在为您重新生成完整回复（第 {retry_attempt}/{MAX_TAG_RETRY} 次）..."
                    )
                    
                    # 重新生成整个回复：将错误输出作为 assistant 历史，然后发送 user 纠正
                    retry_history = list(temp_history)
                    retry_history.append({"role": "assistant", "content": source_text_for_tags})
                    retry_prompt = (
                        f"你上一次的输出存在格式问题/不完整：{reason_text}。\n"
                        "请无视那次错误的输出，重新生成完整的回复。你的回复应该同时包括你想对我说的话（如果有），并在回复末尾附上所有图片的 [IMAGE_TAGS:ID] 块。\n"
                        "要求：为下面列表中的每一张图片提取至少8个细节相关的关键词，必须使用英文逗号分隔。\n"
                        "图片 ID 列表：\n" + "\n".join(f"- {mid}" for mid in expected_ids)
                    )
                    
                    retry_stream = self._chat_stream_wrapper(
                        self.image_llm if multimodal_images else self.coder_llm,
                        chat_id,
                        system_prompt=system_prompt,
                        user_prompt=retry_prompt,
                        history=retry_history,
                        tools=[],
                        multimodal_images=multimodal_images,
                        temperature=0.7
                    )
                    
                    retry_buffer = ""
                    async for raw_chunk in retry_stream:
                        chunk = cast(Dict[str, Any], raw_chunk) if isinstance(raw_chunk, dict) else raw_chunk
                        if isinstance(chunk, dict) and chunk.get("type") == "text":
                            retry_buffer += chunk.get("content", "")
                        elif isinstance(chunk, str):
                            retry_buffer += chunk

                    final_response_buffer = retry_buffer
                    if multimodal_images:
                        last_image_raw_output = retry_buffer
                    
                    image_tags_extracted.clear()
                    image_ocr_extracted.clear()
                    parsed_tag_blocks.clear()
                    parsed_ocr_blocks.clear()
                    
                    source_text_for_tags = final_response_buffer
                    for m in self._IMAGE_TAGS_PATTERN.finditer(source_text_for_tags):
                        img_id = m.group(1).strip()
                        tags_text = m.group(2).strip()
                        if img_id and tags_text:
                            image_tags_extracted[img_id] = tags_text
                            parsed_tag_blocks.append((img_id, tags_text))
                    for m in self._IMAGE_OCR_PATTERN.finditer(source_text_for_tags):
                        img_id = m.group(1).strip()
                        ocr_text = m.group(2).strip()
                        if img_id and ocr_text and ocr_text != "（无文字）":
                            image_ocr_extracted[img_id] = ocr_text
                            parsed_ocr_blocks.append((img_id, ocr_text))

                    # LLM 可能输出占位符/错误 image_id
                    if parsed_tag_blocks:
                        for idx, expected_id in enumerate(expected_ids):
                            if expected_id in image_tags_extracted:
                                continue
                            if idx >= len(parsed_tag_blocks):
                                break
                            parsed_id, parsed_tags = parsed_tag_blocks[idx]
                            if parsed_id not in expected_set and parsed_tags:
                                image_tags_extracted[expected_id] = parsed_tags
                                logger.warning(f"[{chat_id}] [重试后] IMAGE_TAGS image_id 不匹配，重映射: {parsed_id} -> {expected_id}")
                    
                    missing_tag_ids, bad_comma_ids = _find_tag_issues()
                    tag_structure_invalid = _detect_tag_structure_issue(source_text_for_tags)
                    no_tags_generated = bool(expected_ids) and not image_tags_extracted
                    image_output_empty = bool(multimodal_images and not source_text_for_tags.strip())
                    need_tag_retry = bool(missing_tag_ids or bad_comma_ids or tag_structure_invalid or no_tags_generated or image_output_empty)

                if need_tag_retry:
                    logger.warning(
                        f"[{chat_id}] 重新请求后仍有缺失/结构问题的 IMAGE_TAGS，缺失 {missing_tag_ids}，格式异常 {bad_comma_ids}，结构异常={tag_structure_invalid}, 未生成标签={no_tags_generated}"
                    )
                    await self._send_debug(chat_id, "❌ 仍未获得有效的 IMAGE_TAGS，建议稍后重试。")
                    # 避免向用户发送包含残缺标签或残缺聊天的响应
                    final_response_buffer = ""
                    last_image_raw_output = ""
                    image_tags_failed = True
                elif retry_attempt:
                    logger.info(f"[{chat_id}] 重新请求后已补齐全部 IMAGE_TAGS。")
                elif tag_structure_invalid:
                    final_response_buffer = re.sub(r"\[/?IMAGE_TAGS[^\]]*", "", final_response_buffer or "", flags=re.IGNORECASE)

            if image_tags_failed:
                if not in_polling and not no_reply:
                    await self.adapter.send_message(chat_id, "图片输出异常，已自动重试失败，请稍后重试。")
                else:
                    logger.info(f"[{chat_id}] [轮询模式] 图片输出异常，已自动重试失败，前脑稍后重试。")
            elif final_response_buffer:
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
                    if not in_polling and not no_reply:
                        sent_ids = await self._send_split_message(chat_id, self._strip_timestamp_markers(clean_response))
                        if sent_ids:
                            response_platform_ids.extend(sent_ids)
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
                    if not in_polling and not no_reply:
                        sent_ids = await self._send_split_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
                        if sent_ids:
                            response_platform_ids.extend(sent_ids)
                else:
                    final_response_buffer = "任务已完成。"
                    if not in_polling and not no_reply:
                        sent_ids = await self._send_split_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
                        if sent_ids:
                            response_platform_ids.extend(sent_ids)
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
                    if not in_polling and not no_reply:
                        sent_ids = await self._send_split_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
                        if sent_ids:
                            response_platform_ids.extend(sent_ids)
                else:
                    final_response_buffer = "任务已完成。"
                    if not in_polling and not no_reply:
                        sent_ids = await self._send_split_message(chat_id, self._strip_timestamp_markers(final_response_buffer))
                        if sent_ids:
                            response_platform_ids.extend(sent_ids)
            
            # --- 4.5 保存助手回复到数据库 ---
            if final_response_buffer:
                metadata = {"image_ids": [img.get("image_id") for img in multimodal_images] if multimodal_images else None}
                if response_platform_ids:
                    metadata["platform_message_ids"] = response_platform_ids

                self.message_history.add_message(
                    platform="telegram",
                    chat_id=storage_id,
                    role="assistant",
                    content=final_response_buffer,
                    user_id="assistant",
                    metadata=metadata,
                )
            
            # --- 4.6 图片记忆存储 (Image Memory Storage) ---
            if multimodal_images and self.image_store.enabled:
                for img in multimodal_images:
                    img_id = img["image_id"]
                    # view_image 等工具回查的旧图不需要重新入库
                    if img.get("from_tool"):
                        logger.debug(f"[{chat_id}] 跳过工具回查图片入库: {img_id} ({img['path']})")
                        continue

                    tags = image_tags_extracted.get(img_id, "")
                    ocr_text = image_ocr_extracted.get(img_id, "")

                    if not tags and not ocr_text:
                        # 保持 pending 占位，待后续补充
                        logger.warning(f"[{chat_id}] 图片 {img_id} 未生成标签/OCR，保持占位待补全 (path={img['path']})")
                        continue

                    asyncio.create_task(
                        self._async_update_image_tags(
                            image_id=img_id,
                            tags=tags,
                            ocr_text=ocr_text,
                            user_id=storage_id,
                            chat_id=chat_id,
                            file_path=img["path"],
                            platform=context.get("platform", "telegram"),
                            platform_message_id=context.get("platform_message_id"),
                        )
                    )
                    ocr_info = f", ocr_text='{ocr_text[:40]}...'" if ocr_text else ""
                    logger.info(f"[{chat_id}] 图片 {img_id} 标签已补充: '{tags[:60]}...'{ocr_info}")            
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
            if self.debug_mode.get(chat_id, False):
                try:
                    err_detail = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                    await self._send_debug(chat_id, f"❌ 后脑异常: {e}\n{err_detail}")
                except Exception:
                    logger.debug("发送错误详情失败", exc_info=True)
            try:
                await self.adapter.send_message(chat_id, "抱歉，处理您的请求时出现内部错误。")
            except Exception:
                pass
            
        finally:
            status.finish()
            followup_delay = context.get("_followup_initial_delay")
            self._mark_scheduler_idle(chat_id, initial_delay=followup_delay)
            if self.tui_callback: self.tui_callback("✅ Idle")
            self.generation_tasks.pop(chat_id, None)
            logger.debug(f"[{chat_id}] 本次生成任务结束。")
            
            in_polling = context.get("_in_polling_loop", False)
            if not in_polling and not self._was_interrupted(chat_id):
                await self._process_pending_messages(chat_id)
