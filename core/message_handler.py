'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-12 13:28:21
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""消息处理 Mixin — handle_new_message + 命令路由。"""

import asyncio
import logging
import re
import time
from typing import Dict, Any, List, Optional

from brain.multimodal import extract_image_payloads, extract_video_payloads
from brain.prompts import render_template, get_soul_prompt, load_identity_context
from core.group_listener import AppendAction, GroupMessageEvent, PassiveAction
from core.group_presence_store import GroupPresence
from core.routing import has_image_input, sanitize_adapter_output_text, sanitize_user_visible_text
from core.conversation_identity import conversation_target_dict, normalize_chat_type
from core.scene_context import build_current_scene_block, should_redact_cross_place_details
from core.default_chat_id_store import save_default_chat_target
from core.delivery_target_store import record_active_scene
from core.scheduler import (
    AIPresence,
    set_ai_presence,
    record_user_activity,
    get_ai_state_summary,
)
import config

logger = logging.getLogger(__name__)


def _update_message_entry_presence(runtime_key: str, chat_type: str) -> Dict[str, Any]:
    """Record activity while keeping group presence independent from global presence."""
    normalized_chat_type = normalize_chat_type(chat_type)
    if normalized_chat_type != "group":
        set_ai_presence(
            AIPresence.ONLINE,
            reason="private_message_received",
            runtime_key=runtime_key,
        )
    record_user_activity(runtime_key)
    return get_ai_state_summary()


def _context_platform_message_ids(context: Dict[str, Any]) -> List[str]:
    """Return all platform message ids carried by an adapter context."""
    raw_ids = context.get("platform_message_ids")
    ids: List[str] = []
    if isinstance(raw_ids, (list, tuple, set)):
        ids.extend(str(mid) for mid in raw_ids if mid is not None)
    elif raw_ids is not None:
        ids.append(str(raw_ids))

    single_id = context.get("platform_message_id")
    if single_id is not None:
        ids.append(str(single_id))

    return list(dict.fromkeys(mid for mid in ids if mid))


def reply_target_message_id(context: Dict[str, Any]) -> str:
    """Return an explicitly selected platform message id to quote."""
    # 私聊场景不需要回复引用，避免消息错乱
    if normalize_chat_type(context.get("chat_type")) == "private":
        return ""
    return str(context.get("reply_target_message_id") or "").strip()


def _attach_platform_ids_to_media(items: List[Dict[str, Any]], platform_msg_ids: List[str]) -> List[Dict[str, Any]]:
    """Attach per-media platform message ids by adapter order when available."""
    if not items or not platform_msg_ids:
        return items
    for item, platform_msg_id in zip(items, platform_msg_ids):
        if isinstance(item, dict) and not item.get("platform_message_id"):
            item["platform_message_id"] = platform_msg_id
    return items


def group_message_content(context: Dict[str, Any], user_name: str, text: str, chat_type: str) -> str:
    """Return persisted user content, avoiding duplicate names for aggregated group text."""
    if str(chat_type or "private").lower() == "private":
        return text
    contributors = context.get("contributors") or []
    contributor_ids = {
        str(item.get("user_id") or "").strip()
        for item in contributors
        if isinstance(item, dict) and str(item.get("user_id") or "").strip()
    }
    if len(contributor_ids) > 1:
        return text
    return f"{user_name}: {text}"


class MessageHandlerMixin:
    """处理来自适配器的新消息 / 命令路由。"""

    _CONFIRM_DECISION_PATTERN = re.compile(
        r"DECISION\s*:\s*(confirm|cancel|unclear|ignore)",
        re.IGNORECASE,
    )
    _CONFIRM_REPLY_PATTERN = re.compile(
        r"REPLY\s*:\s*(.*)",
        re.IGNORECASE | re.DOTALL,
    )
    _AUTO_ENQUEUE_DECISION_PATTERN = re.compile(
        r"DECISION\s*:\s*(enqueue|skip)",
        re.IGNORECASE,
    )

    def _append_busy_queue_summary(self, chat_id: str, reply: str) -> str:
        """后脑忙碌时，在给用户的回复后附带当前队列详情。"""
        text = (reply or "").strip()
        queue = self._get_scope_queue_for_runtime(chat_id)
        queue_summary = queue.get_queue_summary() if queue else ""
        queue_block = queue_summary if queue_summary else "（暂无排队任务）"

        # 避免重复附加
        if "【后脑队列详情】" in text:
            return text

        if text:
            return f"{text}\n\n【后脑队列详情】\n{queue_block}"
        return f"【后脑队列详情】\n{queue_block}"

    async def submit_group_message(self, context: Dict[str, Any]) -> bool:
        """Adapter-facing entry for normalized ONLINE group events."""
        normalized = dict(context)
        identity = self._identity(normalized)
        if identity.chat_type != "group":
            return False
        normalized.update(
            {
                "runtime_key": identity.runtime_key,
                "platform": identity.platform,
                "chat_id": identity.platform_chat_id,
                "memory_scope_id": identity.memory_scope_id,
                "place_scope_id": identity.place_scope_id,
            }
        )
        return await self.group_listener.receive(normalized)

    def group_presence_for(self, runtime_key: str) -> GroupPresence:
        return self.group_listener.mode_for(runtime_key)

    async def _persist_online_group_message(self, context: Dict[str, Any]) -> None:
        """Persist one ONLINE event immediately into the existing shared history."""
        if context.get("_message_saved"):
            return
        identity = self._identity(context)
        text = str(context.get("text") or "")
        user_name = str(context.get("user_name") or identity.actor_display_name or "User")
        metadata: Dict[str, Any] = {"source": "group_listener"}
        platform_ids = _context_platform_message_ids(context)
        if platform_ids:
            metadata["platform_message_ids"] = platform_ids
        reply_metadata = {
            key: context.get(key)
            for key in (
                "reply_to_message_id",
                "reply_to_user_id",
                "reply_to_user_name",
                "reply_to_text",
            )
            if context.get(key) not in (None, "")
        }
        if reply_metadata:
            metadata["reply"] = reply_metadata
        self.message_history.add_message(
            platform=identity.platform,
            chat_id=identity.storage_id,
            role="user",
            content=group_message_content(context, user_name, text, identity.chat_type),
            user_id=identity.actor_user_id,
            metadata=metadata,
            memory_scope_id=identity.memory_scope_id,
            place_scope_id=identity.place_scope_id,
            actor_display_name=identity.actor_display_name,
        )
        context["_message_saved"] = True

    @staticmethod
    def _format_group_events(events: List[GroupMessageEvent]) -> str:
        lines = []
        for event in events:
            reply = ""
            if event.reply_to_message_id:
                target = event.reply_to_user_name or event.reply_to_user_id or "未知成员"
                reply = f"（回复 {target}#{event.reply_to_message_id}）"
            message_id = f" id={event.platform_message_id}" if event.platform_message_id else ""
            lines.append(f"[{event.sequence}] {event.sender_name}{message_id}{reply}: {event.text}")
        return "\n".join(lines)

    async def _collect_fast_text(self, runtime_key: str, system_prompt: str, user_prompt: str) -> str:
        async def collect() -> str:
            chunks: List[str] = []
            async for chunk in self._chat_stream_wrapper(
                self.fast_llm,
                runtime_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=[],
            ):
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    chunks.append(str(chunk.get("content") or ""))
                elif isinstance(chunk, str):
                    chunks.append(chunk)
            return "".join(chunks).strip()

        return await asyncio.wait_for(collect(), timeout=self.group_listener_decision_timeout)

    async def _decide_group_passive_action(
        self,
        runtime_key: str,
        events: List[GroupMessageEvent],
    ) -> PassiveAction:
        system_prompt = render_template("group_listener.jinja", "passive_system")
        user_prompt = render_template(
            "group_listener.jinja",
            "passive_user",
            events_text=self._format_group_events(list(events)),
        )
        result = await self._collect_fast_text(
            runtime_key,
            system_prompt,
            user_prompt,
        )
        match = re.search(r"\b(KEEP_LISTENING|REPLY|SEMI_ONLINE)\b", result.upper())
        return PassiveAction(match.group(1)) if match else PassiveAction.KEEP_LISTENING

    async def _decide_group_append_action(
        self,
        runtime_key: str,
        base: List[GroupMessageEvent],
        batch: List[GroupMessageEvent],
    ) -> AppendAction:
        system_prompt = render_template("group_listener.jinja", "append_system")
        user_prompt = render_template(
            "group_listener.jinja",
            "append_user",
            base_events_text=self._format_group_events(list(base)),
            new_events_text=self._format_group_events(list(batch)),
        )
        result = await self._collect_fast_text(runtime_key, system_prompt, user_prompt)
        match = re.search(r"\b(APPEND|KEEP_PENDING)\b", result.upper())
        return AppendAction(match.group(1)) if match else AppendAction.KEEP_PENDING

    def _group_batch_context(
        self,
        events: List[GroupMessageEvent],
        reason: str,
        continuation_token: Optional[int] = None,
    ) -> Dict[str, Any]:
        latest = events[-1]
        context = dict(latest.context)
        context.update(
            {
                "runtime_key": latest.runtime_key,
                "platform": latest.platform,
                "chat_id": latest.platform_chat_id,
                "chat_type": "group",
                "memory_scope_id": latest.memory_scope_id,
                "place_scope_id": latest.place_scope_id,
                "text": self._format_group_events(events),
                "group_message_batch": [event.context for event in events],
                "_message_saved": True,
                "_group_listener_promoted": True,
                "_group_listener_reason": reason,
            }
        )
        if continuation_token is not None:
            context["_group_listener_continuation_token"] = continuation_token
        return context

    async def _promote_group_message_batch(
        self,
        runtime_key: str,
        events: List[GroupMessageEvent],
        reason: str,
    ) -> None:
        if not events:
            return
        await self.handle_new_message(self._group_batch_context(list(events), reason))

    async def _interrupt_group_generation(
        self,
        runtime_key: str,
        events: List[GroupMessageEvent],
        reason: str,
        token: int,
    ) -> None:
        current = self.front_brain_tasks.get(runtime_key)
        if current and not current.done():
            current.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(current), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        restart_events = list(events)
        if reason == "mention":
            restart_events = await self.group_listener.priority_restart_events(
                runtime_key,
                token,
                replacement_events=events,
            )
        if restart_events:
            await self.handle_new_message(
                self._group_batch_context(
                    restart_events,
                    reason,
                    continuation_token=token,
                )
            )

    async def handle_new_message(self, context: Dict[str, Any]):
        """处理来自适配器的新消息/命令。"""
        # copy context to allow safe mutation (inject multimodal payloads, flags, etc.)
        context = dict(context)
        identity = self._identity(context)
        chat_id = identity.runtime_key
        platform_chat_id = identity.platform_chat_id
        platform = identity.platform
        storage_id = identity.storage_id
        user_id = identity.actor_user_id
        text = context["text"]
        chat_type = identity.chat_type
        user_name = context.get("user_name", "User")
        # 跨平台接力作用域字段（统一在身份解包处取，供下方所有 add_message/读取调用复用）
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id
        actor_display_name = identity.actor_display_name

        # 解析多模态输入：从 [image: ...] 中提取真实图片内容
        clean_text, multimodal_images = extract_image_payloads(text)
        # has_image_input 用来探测"用户文本里疑似带图片标记/链接"，
        # multimodal_images 是真正成功加载的图片字节。两者要分开使用：
        #  - has_image_marker: 判断文本里是否出现图片相关标记（用于上下文提示）
        #  - has_real_image: 真的有图片字节（用于决定是否走后脑 image 模型）
        has_image_marker = has_image_input(text)
        has_real_image = bool(multimodal_images)
        # 仅当真的拿到了图片字节，才把它当作"图片输入"来路由到后脑。
        # 文本里出现 [image: xxx] 但加载失败时，按普通文本处理，让模型基于上下文回应，
        # 而不是直接弹"没读取到图片"硬编码消息（也避免模型臆想图片内容）。
        image_input_detected = has_real_image
        if clean_text:
            text = clean_text

        # 解析视频输入：从 [video: ...] 中提取视频内容
        clean_text_v, multimodal_videos = extract_video_payloads(text)
        has_real_video = bool(multimodal_videos)
        if clean_text_v:
            text = clean_text_v

        # 将清洗后的文本和多模态内容回填到 context
        platform_msg_ids = _context_platform_message_ids(context)
        reply_to_message_id = reply_target_message_id(context)
        _attach_platform_ids_to_media(multimodal_images, platform_msg_ids)
        _attach_platform_ids_to_media(multimodal_videos, platform_msg_ids)
        context["text"] = text
        if multimodal_images:
            context["multimodal_images"] = multimodal_images
        if multimodal_videos:
            context["multimodal_videos"] = multimodal_videos
        # 视频也需要走后脑（无论是否有视频模型，后脑会判断降级逻辑）
        video_input_detected = has_real_video

        # 如果探测到图片标记但实际未加载到图片字节，记录到 context，
        # 让后续模型 prompt 知道"用户疑似发了图但没真正收到"，从而避免凭空想象图片内容。
        if has_image_marker and not has_real_image:
            context["image_load_failed"] = True
            logger.warning(
                "[%s] 检测到图片标记但未加载到图片字节，按文本流程继续，message_length=%d",
                chat_id,
                len(text),
            )

        logger.debug(
            "[%s] 收到新聚合消息: chat_type=%s message_length=%d",
            chat_id,
            chat_type,
            len(text),
        )

        # 私聊输入维持既有全局活跃会话语义；群聊的监听模式由群级 marker/fast 独立决定。
        # 群内 @ 或回复只负责唤醒本轮处理，不能因此提升全局或当前群的 presence。
        ai_state = _update_message_entry_presence(chat_id, chat_type)
        logger.info(
            "[%s] 消息入口状态: chat_type=%s global_presence=%s "
            "generating=%s backend_busy=%s explicit_mention=%s reply_to_bot=%s",
            chat_id,
            chat_type,
            ai_state["presence"],
            ai_state["is_generating"],
            ai_state["is_backend_busy"],
            bool(context.get("explicit_bot_mention")),
            bool(context.get("reply_to_bot")),
        )
        # 取消该 chat 已有的对话延续定时器（用户回来了）
        self._cancel_followup_timer(chat_id)

        # ── 非中断型命令快速路径：不打断前脑生成 ──
        # 这些命令不需要取消正在进行的生成任务，直接执行后返回。
        stripped_early = text.strip()
        _NON_INTERRUPTING_CMDS = (
            r"(?m)^\s*/debug\b",
            r"(?m)^\s*/status\b",
            r"(?m)^\s*/custom_scope\b",
            r"(?m)^\s*/set_stream\b",
            r"(?m)^\s*/nonstream\b",
            r"(?m)^\s*/undo\b",
            r"(?m)^\s*/context\b",
        )
        for _pat in _NON_INTERRUPTING_CMDS:
            if re.search(_pat, stripped_early):
                # 直接跳转到命令分发，不取消前脑任务
                return await self._dispatch_non_interrupting_cmd(
                    chat_id, chat_type, user_id, stripped_early
                )

        # 用户在前脑还在生成时又发了一条消息：取消前脑的当前生成，按聚合器语义合并：
        # ONLINE 群监听的 promoted/restart 输入由 GroupListenerManager 自己协调取消和重启，
        # 不能在这里再次把刚登记的新逻辑周期取消掉。
        prev_front_task = self.front_brain_tasks.get(chat_id)
        listener_managed = bool(context.get("_group_listener_promoted"))
        if prev_front_task and not prev_front_task.done() and not listener_managed:
            partial_len = len((self.front_brain_partial.get(chat_id) or ""))
            logger.info(
                f"[{chat_id}] 检测到前脑仍在生成回复（已生成 {partial_len} 字），新消息到达，"
                f"取消前脑任务并合并新消息。"
            )
            prev_front_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(prev_front_task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self.front_brain_tasks.pop(chat_id, None)
            # 注意：故意 *不* 清理 self.front_brain_partial[chat_id]，让下一次前脑能看到草稿。

        if self.scheduler:
            # 自动设置 default_chat_id（首次消息时）
            if not self.scheduler.default_chat_id:
                self.scheduler.default_chat_id = chat_id
                save_default_chat_target(conversation_target_dict(identity))
                logger.info(f"Scheduler default_chat_id 已设置: {chat_id}")

        if getattr(self, "trigger_manager", None) and not self.trigger_manager.default_chat_id:
            self.trigger_manager.default_chat_id = chat_id
            self.trigger_manager.default_chat_target = conversation_target_dict(identity)
            save_default_chat_target(self.trigger_manager.default_chat_target)
            # 同步给已注册的 sub-trigger（如 EmailTrigger 在初始化时拿到的是空字符串）
            for trig in getattr(self.trigger_manager, "_triggers", []) or []:
                if hasattr(trig, "default_chat_id") and not trig.default_chat_id:
                    trig.default_chat_id = chat_id
                if hasattr(trig, "default_chat_target") and not getattr(trig, "default_chat_target", None):
                    trig.default_chat_target = dict(self.trigger_manager.default_chat_target)
            logger.info(f"Trigger default_chat_id 已设置: {chat_id}")

        # ── 记录活跃平台 + 活跃场景（含群聊）──
        # 最新收到消息的平台/场景成为活跃平台/活跃场景，用于 Nora 输出标记
        # [platform][scene] 缺省时的兜底。私聊会顺带刷新主动投递端；
        # 群聊只记活跃场景、不改动私聊投递键（绝不自动发进群）。
        try:
            record_active_scene(conversation_target_dict(identity))
        except Exception as e:
            logger.debug(f"[{chat_id}] 记录活跃平台/场景失败: {e}")

        # ── 命令分发 ──
        stripped = text.strip()

        if re.search(r"(?m)^\s*/start\b", stripped):
            await self._cmd_start(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/reload_models\b", stripped):
            await self._cmd_reload_models(chat_id)
            return

        if re.search(r"(?m)^\s*/clear\b", stripped):
            await self._cmd_clear(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/stop\b", stripped):
            await self._cmd_stop(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/regenerate_proactive\b", stripped):
            await self._cmd_regenerate_proactive(chat_id, stripped)
            return

        if re.search(r"(?m)^\s*/schedule_today\b", stripped):
            await self._cmd_schedule_today(chat_id)
            return

        if re.search(r"(?m)^\s*/status\b", stripped):
            await self._cmd_status(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/debug\b", stripped):
            await self._cmd_debug(chat_id)
            return

        if re.search(r"(?m)^\s*/custom_scope\b", stripped):
            await self._cmd_custom_scope(chat_id, stripped)
            return

        if re.search(r"(?m)^\s*/undo\b", stripped):
            await self._cmd_undo(chat_id, chat_type, user_id)
            return

        if re.search(r"(?m)^\s*/set_stream\b", stripped) or re.search(r"(?m)^\s*/nonstream\b", stripped):
            await self._cmd_set_stream(chat_id, chat_type, user_id, stripped)
            return

        # --- 前后端分离逻辑 ---
        busy_runtime = self.get_busy_backend_runtime(memory_scope_id)
        backend_runtime = busy_runtime or chat_id
        status = self.worker_status.get(backend_runtime)
        queue = self._get_scope_queue_for_runtime(chat_id)
        backend_busy_or_queued = bool(busy_runtime or (queue and queue.size() > 0))

        if backend_busy_or_queued:
            # 后端忙碌：允许前脑继续生成，不强制回复“在忙”。
            # 仅在有图片时直接排队，其他情况交由前脑处理并按需打断/切换。
            message_content = group_message_content(context, user_name, text, chat_type)

            # 若包含真实图片/视频输入（已成功加载字节）：聚合器语义合入正在进行的后脑任务。
            #   - 后脑还没输出任何文本：cancel 旧后脑 → 把"旧图 + 新图"合并 → 立即重启一次后脑。
            #   - 后脑已经生成部分文本：cancel + 把已生成草稿带到合并后的新一次后脑生成里继续/重写。
            # 不再发硬编码"已排队"提示——行为对齐前脑文本聚合器的精神。
            if image_input_detected or video_input_detected:
                user_metadata = {}
                platform_msg_ids = _context_platform_message_ids(context)
                if platform_msg_ids:
                    user_metadata["platform_message_ids"] = platform_msg_ids

                self.message_history.add_message(
                    platform=platform,
                    chat_id=storage_id,
                    role="user",
                    content=message_content,
                    user_id=user_id,
                    metadata=user_metadata or None,
                    memory_scope_id=memory_scope_id,
                    place_scope_id=place_scope_id,
                    actor_display_name=actor_display_name,
                )
                context["_message_saved"] = True

                # 收集旧后脑的输入快照与已生成草稿
                prior_input = self.back_brain_input_context.get(backend_runtime) or {}
                prior_text = prior_input.get("text", "") or ""
                prior_images = list(prior_input.get("multimodal_images") or [])
                prior_partial = (self.back_brain_partial.get(backend_runtime) or "").strip()

                # 标记当前任务为"被图片合并打断"，让后脑 finally 不要清空缓冲
                old_task = self.generation_tasks.get(backend_runtime)
                if status:
                    status_ctx = getattr(status, "context", None)
                # 给旧后脑 context 打标记的最稳妥方式：直接 cancel 后清理 worker_status，由新任务替代
                logger.info(
                    f"[{chat_id}] 后脑忙碌 + 新图片到达：cancel 旧后脑并合并旧图({len(prior_images)}) + 新图重启。"
                )
                if old_task and not old_task.done():
                    # 让 finally 不清掉 partial / input_context（虽然此时它本来就要被新任务覆盖，
                    # 但保留语义清晰）。直接给旧 context 加标记需要拿到引用——简化处理：
                    # 我们已经把 partial / input_context 提取到本地变量 prior_*，丢失也不影响。
                    old_task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(old_task), timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                        pass
                    self.generation_tasks.pop(backend_runtime, None)

                # 标记打断，避免旧任务的 finally 触发 _process_pending_messages
                session = self.sessions.setdefault(backend_runtime, {"history": [], "interrupted_thought": "", "pending_text": ""})
                session["_interrupted_by_frontend"] = True

                # 旧 worker_status 清理
                if status:
                    try:
                        status.finish()
                    except Exception:
                        pass

                # 合并 multimodal_images：旧图（保持顺序）+ 新图（同样保持顺序，去重 image_id）
                new_images = list(context.get("multimodal_images") or [])
                merged_images: List[Dict[str, Any]] = []
                seen_ids = set()
                for img in (prior_images + new_images):
                    iid = img.get("image_id") if isinstance(img, dict) else None
                    if iid and iid in seen_ids:
                        continue
                    if iid:
                        seen_ids.add(iid)
                    merged_images.append(img)

                # 合并 text：把旧文本与新文本拼起来（新文本是这条消息已经处理过 [image:...] 的纯文本）
                merged_text_parts = [t for t in (prior_text.strip(), text.strip()) if t]
                merged_text = "\n".join(merged_text_parts) if merged_text_parts else text

                # 构造合并后的 context，重启后脑
                merged_context = dict(context)
                merged_context["text"] = merged_text
                merged_context["multimodal_images"] = merged_images
                if prior_partial:
                    merged_context["_back_brain_prior_partial"] = prior_partial
                merged_context["_message_saved"] = True
                # 清理上一轮残留：旧 input_context 和 partial 我们已经吃掉了
                self.back_brain_partial.pop(backend_runtime, None)
                self.back_brain_input_context.pop(backend_runtime, None)

                task = asyncio.create_task(self._generate_response(merged_context))
                self.generation_tasks[chat_id] = task
                logger.info(
                    f"[{chat_id}] 已合并 {len(merged_images)} 张图片（草稿 {len(prior_partial)} 字）重启后脑。"
                )
                return

            # 无图片：记录消息并运行意图检测，仅在需要打断/切换/队列操作时回复；
            # 普通 queue 场景不再强制提示“在忙”，让前脑即时回复。
            logger.info(f"[{chat_id}] 后端忙碌（允许前脑即时回复），记录消息后进行意图检测")
            user_metadata = {}
            platform_msg_ids = _context_platform_message_ids(context)
            if platform_msg_ids:
                user_metadata["platform_message_ids"] = platform_msg_ids

            self.message_history.add_message(
                platform=platform,
                chat_id=storage_id,
                role="user",
                content=message_content,
                user_id=user_id,
                metadata=user_metadata or None,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
                actor_display_name=actor_display_name,
            )
            decision = await self._detect_interrupt_intent_and_reply(chat_id, text, status, backend_runtime=backend_runtime)
            action = decision.get("action", "queue")
            reply = decision.get("reply", "")

            # 标记已保存用户消息，后续前脑阶段无需重复写库
            context["_message_saved"] = True
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session_history = session.setdefault("history", [])

            if action in {"stop", "change"}:
                session_history.append({"role": "user", "content": message_content})
                if reply:
                    msg_ids = await self._send_platform_message(
                        chat_id,
                        reply,
                        reply_to_message_id=reply_to_message_id,
                    )
                    md = {"source": "interrupt"}
                    if msg_ids:
                        md["platform_message_ids"] = [str(mid) for mid in msg_ids]
                    self.message_history.add_message(
                        platform=platform,
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                        memory_scope_id=memory_scope_id,
                        place_scope_id=place_scope_id,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                await self._interrupt_backend(
                    chat_id,
                    reason=action,
                    user_text=text,
                    skip_reply=True,
                    target_runtime_key=backend_runtime,
                    source_context=context,
                )
                return
            elif action == "list_queue":
                session_history.append({"role": "user", "content": message_content})
                if reply:
                    reply = self._append_busy_queue_summary(chat_id, reply)
                    msg_ids = await self._send_platform_message(
                        chat_id,
                        reply,
                        reply_to_message_id=reply_to_message_id,
                    )
                    md = {"source": "queue_status"}
                    if msg_ids:
                        md["platform_message_ids"] = [str(mid) for mid in msg_ids]
                    self.message_history.add_message(
                        platform=platform,
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                        memory_scope_id=memory_scope_id,
                        place_scope_id=place_scope_id,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                return
            elif action == "cancel_queue":
                session_history.append({"role": "user", "content": message_content})
                queue = self._get_scope_queue_for_runtime(chat_id)
                param = decision.get("param")
                if queue and param:
                    removed_text = await queue.remove_by_index(param)
                    if removed_text:
                        logger.info(f"[{chat_id}] 已取消排队任务 #{param}: '{removed_text}'")
                    else:
                        logger.warning(f"[{chat_id}] 取消排队任务 #{param} 失败（序号无效或队列已空）")
                if reply:
                    msg_ids = await self._send_platform_message(
                        chat_id,
                        reply,
                        reply_to_message_id=reply_to_message_id,
                    )
                    md = {"source": "queue_cancel"}
                    if msg_ids:
                        md["platform_message_ids"] = [str(mid) for mid in msg_ids]
                    self.message_history.add_message(
                        platform=platform,
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                        memory_scope_id=memory_scope_id,
                        place_scope_id=place_scope_id,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                return
            elif action == "clear_queue":
                session_history.append({"role": "user", "content": message_content})
                queue = self._get_scope_queue_for_runtime(chat_id)
                if queue:
                    await queue.clear()
                    logger.info(f"[{chat_id}] 已清空所有排队任务")
                if reply:
                    msg_ids = await self._send_platform_message(
                        chat_id,
                        reply,
                        reply_to_message_id=reply_to_message_id,
                    )
                    md = {"source": "queue_clear"}
                    if msg_ids:
                        md["platform_message_ids"] = [str(mid) for mid in msg_ids]
                    self.message_history.add_message(
                        platform=platform,
                        chat_id=storage_id,
                        role="assistant",
                        content=reply,
                        user_id="assistant",
                        metadata=md,
                        memory_scope_id=memory_scope_id,
                        place_scope_id=place_scope_id,
                    )
                    session_history.append({"role": "assistant", "content": reply})
                return
            else:
                # action == "queue"：不发忙碌提示，让前脑继续。
                pass

        # --- 正常流程：后端空闲 ---
        # 仅在确保非忙碌的情况下，才执行旧的抢占逻辑，清理残留的“假死”任务
        if not (status and status.busy):
            if chat_id in self.generation_tasks:
                logger.info(f"[{chat_id}] 抢占：清理可能残留的生成任务。")
                self.generation_tasks[chat_id].cancel()
                await asyncio.sleep(0.1)

        # 图片/视频消息直接走后脑（需要 image/video 模型 + 工具能力）
        # 注意：此处 image_input_detected 已经只在"真的拿到了图片字节"时为 True；
        # 如果用户文本里出现 [image: ...] 但加载失败，会走下方前脑流程，
        # 由模型基于 context["image_load_failed"] 标记自然回应（不再硬编码"没读到图片"）。
        if image_input_detected or video_input_detected:
            media_type = "视频" if video_input_detected else "图片"
            logger.info(f"[{chat_id}] 检测到{media_type}输入，直接启动后脑。")
            # 将解析后的干净文本放入 context，避免后脑重复清洗
            context = dict(context)
            context["text"] = text
            if multimodal_images:
                context["multimodal_images"] = multimodal_images
            task = asyncio.create_task(self._generate_response(context))
            self.generation_tasks[chat_id] = task
            return

        # --- 前脑优先路由 (Front-Brain-First) ---
        # 1) 保存用户消息到数据库（保证前脑能读到历史）
        message_content = group_message_content(context, user_name, text, chat_type)
        if not context.get("_message_saved"):
            user_metadata = {}
            platform_msg_ids = _context_platform_message_ids(context)
            if platform_msg_ids:
                user_metadata["platform_message_ids"] = platform_msg_ids

            self.message_history.add_message(
                platform=platform,
                chat_id=storage_id,
                role="user",
                content=message_content,
                user_id=user_id,
                metadata=user_metadata or None,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
                actor_display_name=actor_display_name,
            )
            context["_message_saved"] = True

        # 2) 前脑生成即时回复 + 路由判断（包成 task 以便后续到达的新消息能打断它）
        listener_token = None
        if context.get("_group_listener_promoted") and chat_type == "group":
            listener_events = [
                GroupMessageEvent.from_context(item, index + 1)
                for index, item in enumerate(context.get("group_message_batch") or [])
                if isinstance(item, dict)
            ]
            continuation_token = context.get("_group_listener_continuation_token")
            listener_token = await self.group_listener.begin_generation(
                chat_id,
                listener_events,
                continuation_token=continuation_token,
            )
            if continuation_token is not None:
                await self.group_listener.mark_restart_started(
                    chat_id,
                    continuation_token,
                    listener_token,
                )
        logger.info(
            "[%s] 前脑生成开始: source=%s listener_token=%s batch_size=%d",
            chat_id,
            context.get("_group_listener_reason") or "direct",
            listener_token if listener_token is not None else "none",
            len(context.get("group_message_batch") or []),
        )
        front_task = asyncio.create_task(self._generate_front_chat_response(context))
        self.front_brain_tasks[chat_id] = front_task
        if listener_token is not None:
            await self.group_listener.bind_generation_task(chat_id, listener_token, front_task)
        try:
            front_result = await front_task
            if listener_token is not None and not await self.group_listener.is_generation_current(
                chat_id, listener_token
            ):
                logger.info(f"[{chat_id}] 丢弃过期群聊生成结果 token={listener_token}")
                return
            if listener_token is not None:
                await self.group_listener.finish_generation(chat_id, listener_token)
        except asyncio.CancelledError:
            logger.info(f"[{chat_id}] 前脑生成已被新消息打断，丢弃本轮结果。")
            return
        except Exception:
            if listener_token is not None:
                await self.group_listener.finish_generation(chat_id, listener_token)
            raise
        finally:
            # 仅在仍指向当前 task 时清理
            if self.front_brain_tasks.get(chat_id) is front_task:
                self.front_brain_tasks.pop(chat_id, None)

        # 3) 发送前脑回复给用户（如果有的话）
        logger.info(
            "[%s] 前脑生成完成: listener_token=%s needs_backend=%s should_reply=%s "
            "force_online=%s force_semi_online=%s",
            chat_id,
            listener_token if listener_token is not None else "none",
            bool(front_result.get("needs_backend")),
            bool(front_result.get("should_reply", True)),
            bool(front_result.get("force_online")),
            bool(front_result.get("force_semi_online")),
        )
        user_reply = front_result.get("user_reply", "")
        should_reply = front_result.get("should_reply", True)
        send_front_reply = bool(user_reply and should_reply)
        busy_runtime = self.get_busy_backend_runtime(memory_scope_id)
        backend_runtime = busy_runtime or chat_id
        status = self.worker_status.get(backend_runtime)
        queue = self._get_scope_queue_for_runtime(chat_id)
        backend_busy_or_queued = bool(busy_runtime or (queue and queue.size() > 0))

        # 去除了：后脑忙碌时，只要要给用户发消息，就附带当前队列详情（避免聊天时泄漏队列信息）

        # 3.5) 撤回旧消息（必须在保存新消息到 DB 之前执行，否则 delete_last_assistant_message
        #       会误删刚保存的新消息）
        retract_target = str(front_result.get("retract_message_target", "") or "").strip()
        if retract_target:
            await self._handle_retract_signal(chat_id, chat_type, user_id, retract_target)

        send_target = None
        if send_front_reply:
            send_target = self.resolve_send_target(
                front_result.get("route"), identity, route_source="front_brain"
            )
            if not send_target.get("runtime_key"):
                logger.warning(f"[{chat_id}] 前脑请求的投递目标被拒绝，跳过用户可见回复。")
                send_front_reply = False

        if send_front_reply:
            # 解析 Nora 输出里的 [platform:X][scene:Y] 重定向标记；缺项取活跃值。
            send_target = send_target or {}
            send_key = send_target.get("runtime_key")
            send_chat_type = send_target.get("chat_type") or chat_type
            redirected = bool(send_target.get("redirected"))
            dest_identity = send_target.get("identity") or identity
            # 重定向到别的场景时，被引用的消息 id 在目标场景不存在，不能带引用。
            reply_id = "" if redirected else reply_to_message_id
            # 使用 [SPLIT] 分段发送
            sent_message_ids = await self._send_split_message(
                send_key,
                user_reply,
                reply_to_message_id=reply_id,
                chat_type=send_chat_type,
            )

            # 保存前脑回复到数据库，记录来源与平台 message_id。
            # 若发生重定向，落库到目标场景的作用域，避免历史记到错误会话。
            log_platform = dest_identity.platform if redirected else platform
            log_storage_id = dest_identity.storage_id if redirected else storage_id
            log_memory_scope_id = dest_identity.memory_scope_id if redirected else memory_scope_id
            log_place_scope_id = dest_identity.place_scope_id if redirected else place_scope_id
            metadata: Dict[str, Any] = {"source": "front"}
            if sent_message_ids:
                metadata["platform_message_ids"] = sent_message_ids
            # 把本轮前脑下达给后脑的任务指示一同存档，
            # 让下一轮前脑能从上下文里看到"自己刚才下过这个任务"，
            # 避免对用户的简单承接语（如"嗯嗯/好的"）误升级为重复后脑任务。
            front_task_instruction = front_result.get("task_instruction", "")
            if front_result.get("needs_backend") or front_task_instruction:
                metadata["routing"] = {
                    "needs_backend": bool(front_result.get("needs_backend")),
                    "task_instruction": front_task_instruction,
                }
            self.message_history.add_message(
                platform=log_platform,
                chat_id=log_storage_id,
                role="assistant",
                content=user_reply,
                user_id="assistant",
                metadata=metadata,
                memory_scope_id=log_memory_scope_id,
                place_scope_id=log_place_scope_id,
            )

            # 同步到 session history
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session_history = session.setdefault("history", [])
            if not session_history or session_history[-1] != {"role": "user", "content": message_content}:
                session_history.append({"role": "user", "content": message_content})
            session_history.append({"role": "assistant", "content": user_reply})
            if len(session_history) > 20:
                session["history"] = session_history[-20:]

        presence_ended = await self._apply_presence_markers(
            chat_id,
            chat_type,
            force_online=bool(front_result.get("force_online")),
            force_semi_online=bool(front_result.get("force_semi_online")),
        )
        if presence_ended:
            return

        followup_delay = self.FOLLOWUP_NEED_FOLLOW_DELAY if front_result.get("need_follow") else None
        if front_result.get("keep_segment_open"):
            self._suspend_followup_until_idle(chat_id)
            followup_delay = None
        if front_result["needs_backend"]:
            # 如果前脑没有发送回复，用户不知道后脑即将启动，发送 typing indicator
            if not send_front_reply:
                try:
                    if hasattr(self.adapter, "start_typing"):
                        await self._start_platform_typing(chat_id)
                except Exception:
                    pass

            # 标记上下文：后脑应跳过用户消息保存（前脑已保存）
            backend_context = context.copy()
            backend_context["_front_brain_handled"] = True
            backend_context["_front_brain_reply"] = user_reply if should_reply else ""
            backend_context["_message_saved"] = True
            backend_context["task_instruction"] = front_result.get("task_instruction", "")
            backend_context["use_image_model"] = front_result.get("use_image_model", context.get("use_image_model", False))
            if followup_delay is not None:
                backend_context["_followup_initial_delay"] = followup_delay

            busy_runtime = self.get_busy_backend_runtime(memory_scope_id)
            status = self.worker_status.get(busy_runtime) if busy_runtime else self.worker_status.get(chat_id)
            queue = self._get_scope_queue_for_runtime(chat_id)
            backend_busy_or_queued = bool(busy_runtime or (queue and queue.size() > 0))
            if backend_busy_or_queued:
                queue_summary = queue.get_queue_summary() if queue else ""
                progress_summary = status.get_summary() if status else ""
                # 把后脑当前正在处理的 query / task_instruction 一并给到入队判定，
                # 让 fast_llm 能基于"现在已经在做什么"判断是否实质重复，而不是凭空决定。
                current_original_query = status.original_query if status else ""
                current_task_instruction = status.task_instruction if status else ""
                if busy_runtime and should_redact_cross_place_details(identity, busy_runtime):
                    progress_summary = (
                        f"后脑正在另一个窗口执行任务（来源地点: {busy_runtime}）。"
                        "当前窗口不能看到那边的任务内容、文件、结果或进度细节。"
                    )
                    current_original_query = ""
                    current_task_instruction = ""
                if queue:
                    try:
                        hidden_count = 0
                        visible_lines = []
                        for item in getattr(queue, "_items", []) or []:
                            item_context = item.get("context") or {}
                            item_identity = self._identity(item_context)
                            if should_redact_cross_place_details(identity, item_identity.runtime_key):
                                hidden_count += 1
                                continue
                            text_preview = str(item.get("text") or item_context.get("text") or "")[:60]
                            instr = str(item_context.get("task_instruction") or "").strip()
                            visible_lines.append(f"- {text_preview}")
                            if instr:
                                visible_lines.append(f"  指示: {instr[:120]}")
                        if hidden_count:
                            queue_summary = "\n".join(visible_lines)
                            queue_summary = (
                                f"{queue_summary}\n另有 {hidden_count} 个其它窗口的排队任务，内容已隐藏。"
                            ).strip()
                    except Exception:
                        queue_summary = "存在其它窗口的排队任务，内容已隐藏。"
                should_enqueue = await self._auto_confirm_backend_enqueue(
                    chat_id=chat_id,
                    pending_request_text=text,
                    front_reply=user_reply if send_front_reply else "",
                    task_instruction=front_result.get("task_instruction", ""),
                    queue_summary=queue_summary,
                    progress_summary=progress_summary,
                    current_original_query=current_original_query,
                    current_task_instruction=current_task_instruction,
                )

                if not should_enqueue:
                    logger.info(f"[{chat_id}] 前脑内部判定本轮暂不入队，跳过排队")
                    return

                logger.info(f"[{chat_id}] 前脑判定需要后脑，且内部确认通过，自动加入队列")
                queue = self._get_scope_queue_for_runtime(chat_id, create=True)
                await queue.enqueue({
                    "context": backend_context,
                    "text": backend_context.get("text", ""),
                })
                logger.info(f"[{chat_id}] 任务已自动入队 (队列长度 {queue.size()})")
                return

            logger.info(f"[{chat_id}] 前脑判定需要后脑，启动轮询循环...")
            task = asyncio.create_task(self._run_polling_loop(backend_context))
            self.generation_tasks[chat_id] = task
        else:
            logger.info(f"[{chat_id}] 前脑判定纯聊天，无需后脑。")
            # 纯聊天完成后，也需要标记空闲并启动 followup 计时
            self._mark_scheduler_idle(chat_id, initial_delay=followup_delay)

    async def _handle_retract_signal(self, chat_id: str, chat_type: str, user_id: str, target: str) -> None:
        """处理前脑发出的撤回标记。支持撤回最近一条或倒数第 N 条 assistant 消息。"""
        identity = self._identity_for_runtime_key(chat_id)
        storage_id = identity.storage_id
        platform = identity.platform
        normalized = target.strip().lower() if target else "last"
        source = None
        nth = 1
        if normalized not in {"", "last", "latest", "上一条", "最近一条"}:
            try:
                value = int(normalized)
                nth = abs(value) if value != 0 else 1
            except (TypeError, ValueError):
                logger.info(f"[{chat_id}] 撤回标记目标暂不支持: {target}，按 last 处理。")
                nth = 1
        try:
            undo_result = self.message_history.delete_last_assistant_message(
                platform,
                storage_id,
                source=source,
                nth=nth,
            )
            if not undo_result:
                return
            md = undo_result.get("metadata") or {}
            platform_ids = md.get("platform_message_ids") or []
            feats = getattr(self.adapter, "platform_features", None)
            if getattr(feats, "supports_delete_message", False):
                for pid in platform_ids:
                    try:
                        await self._delete_platform_message(chat_id, str(pid))
                    except Exception:
                        logger.debug(f"[{chat_id}] 撤回平台消息失败: {pid}", exc_info=True)
        except Exception:
            logger.warning(f"[{chat_id}] 处理撤回标记失败。", exc_info=True)

    async def _auto_confirm_backend_enqueue(
        self,
        chat_id: str,
        pending_request_text: str,
        front_reply: str,
        task_instruction: str,
        queue_summary: str,
        progress_summary: str,
        current_original_query: str = "",
        current_task_instruction: str = "",
    ) -> bool:
        """前脑内部二次确认：判断本轮是否应自动加入后脑队列（不向用户发确认）。"""
        soul = get_soul_prompt()
        identity = None
        try:
            identity = self._identity_for_runtime_key(chat_id)
        except Exception:
            identity = None
        if identity:
            identity_context = load_identity_context(
                include_schedule=False,
                actor_display_name=identity.actor_display_name,
                is_owner=identity.is_owner,
                chat_type=identity.chat_type,
            )
        else:
            identity_context = load_identity_context(include_schedule=False)

        system_prompt = render_template(
            "queue_confirmation.jinja",
            "auto_decide_system",
            soul=soul,
            identity_context=identity_context,
        )
        if identity:
            system_prompt = system_prompt + "\n\n---\n" + build_current_scene_block(identity)
        user_prompt = render_template(
            "queue_confirmation.jinja",
            "auto_decide_user",
            pending_request_text=pending_request_text,
            front_reply=front_reply,
            task_instruction=task_instruction,
            queue_summary=queue_summary,
            progress_summary=progress_summary,
            current_original_query=current_original_query,
            current_task_instruction=current_task_instruction,
        )

        response = ""
        try:
            stream = self._chat_stream_wrapper(
                self.fast_llm,
                chat_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=None,
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += str(chunk.get("content", ""))
                elif isinstance(chunk, str):
                    response += chunk
        except Exception:
            logger.warning(f"[{chat_id}] 前脑内部入队确认失败，默认 enqueue", exc_info=True)
            return True

        response = response.strip()
        m = self._AUTO_ENQUEUE_DECISION_PATTERN.search(response)
        decision = m.group(1).lower() if m else "enqueue"
        should_enqueue = decision == "enqueue"
        logger.info(
            f"[{chat_id}] 前脑内部入队确认: decision={decision}, raw='{response[:120]}'"
        )
        return should_enqueue

    async def _prepare_backend_enqueue_confirmation(
        self,
        chat_id: str,
        storage_id: str,
        user_message_content: str,
        backend_context: Dict[str, Any],
        pending_request_text: str,
        front_reply: str = "",
    ) -> None:
        """后脑忙碌时，先缓存待入队任务并向用户发起二次确认。"""
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})

        queue = self._get_scope_queue_for_runtime(chat_id)
        queue_summary = queue.get_queue_summary() if queue else ""
        busy_runtime = self.get_busy_backend_runtime(memory_scope_id)
        status = self.worker_status.get(busy_runtime) if busy_runtime else self.worker_status.get(chat_id)
        progress_summary = status.get_summary() if status else ""

        session["pending_backend_enqueue"] = {
            "backend_context": backend_context,
            "pending_request_text": pending_request_text,
            "queue_summary": queue_summary,
            "created_at": time.time(),
        }

        confirm_reply = await self._generate_enqueue_confirmation_reply(
            chat_id=chat_id,
            pending_request_text=pending_request_text,
            queue_summary=queue_summary,
            progress_summary=progress_summary,
        )

        session_history = session.setdefault("history", [])
        session_history.append({"role": "user", "content": user_message_content})

        if front_reply:
            reply_to_message_id = reply_target_message_id(backend_context)
            front_sent_ids = await self._send_split_message(
                chat_id,
                front_reply,
                reply_to_message_id=reply_to_message_id,
            )
            front_metadata = {"source": "front", "stage": "busy_chat"}
            if front_sent_ids:
                front_metadata["platform_message_ids"] = front_sent_ids
            self.message_history.add_message(
                platform=platform,
                chat_id=storage_id,
                role="assistant",
                content=front_reply,
                user_id="assistant",
                metadata=front_metadata,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
            )
            session_history.append({"role": "assistant", "content": front_reply})

        if confirm_reply:
            reply_to_message_id = reply_target_message_id(backend_context)
            sent_ids = await self._send_split_message(
                chat_id,
                confirm_reply,
                reply_to_message_id=reply_to_message_id,
            )
            metadata = {"source": "front", "stage": "queue_confirm"}
            if sent_ids:
                metadata["platform_message_ids"] = sent_ids

            self.message_history.add_message(
                platform=platform,
                chat_id=storage_id,
                role="assistant",
                content=confirm_reply,
                user_id="assistant",
                metadata=metadata,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
            )
            session_history.append({"role": "assistant", "content": confirm_reply})

        if len(session_history) > 20:
            session["history"] = session_history[-20:]

    async def _try_handle_pending_backend_enqueue(
        self,
        context: Dict[str, Any],
        text: str,
        chat_type: str,
        user_id: str,
        user_name: str,
    ) -> bool:
        """处理“待确认入队”对话；已处理返回 True。"""
        chat_id = context["chat_id"]
        identity = self._identity(context)
        chat_id = identity.runtime_key
        platform = identity.platform
        storage_id = identity.storage_id
        user_id = identity.actor_user_id
        chat_type = identity.chat_type
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id
        actor_display_name = identity.actor_display_name
        session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
        pending = session.get("pending_backend_enqueue")
        if not pending:
            return False

        decision, reply = await self._detect_enqueue_confirmation_decision(
            chat_id=chat_id,
            user_text=text,
            pending_request_text=str(pending.get("pending_request_text", "")),
            queue_summary=str(pending.get("queue_summary", "")),
        )

        if decision == "ignore":
            session.pop("pending_backend_enqueue", None)
            logger.info(f"[{chat_id}] 待确认入队取消：用户开始了新话题或拒绝（判断为 ignore）")
            return False

        decision, reply = await self._detect_enqueue_confirmation_decision(
            chat_id=chat_id,
            user_text=text,
            pending_request_text=str(pending.get("pending_request_text", "")),
            queue_summary=str(pending.get("queue_summary", "")),
        )

        if decision == "ignore":
            session.pop("pending_backend_enqueue", None)
            logger.info(f"[{chat_id}] 待确认入队取消：用户开始了新话题或拒绝（判断为 ignore）")
            return False

        message_content = group_message_content(context, user_name, text, chat_type)

        user_metadata = {}
        platform_msg_ids = _context_platform_message_ids(context)
        if platform_msg_ids:
            user_metadata["platform_message_ids"] = platform_msg_ids

        self.message_history.add_message(
            platform=platform,
            chat_id=storage_id,
            role="user",
            content=message_content,
            user_id=user_id,
            metadata=user_metadata or None,
            memory_scope_id=memory_scope_id,
            place_scope_id=place_scope_id,
            actor_display_name=actor_display_name,
        )
        context["_message_saved"] = True

        session_history = session.setdefault("history", [])
        session_history.append({"role": "user", "content": message_content})

        if decision == "confirm":
            backend_context = pending.get("backend_context") or {}
            busy_runtime = self.get_busy_backend_runtime(memory_scope_id)
            status = self.worker_status.get(busy_runtime) if busy_runtime else self.worker_status.get(chat_id)
            if busy_runtime or (status and status.busy):
                queue = self._get_scope_queue_for_runtime(chat_id, create=True)
                await queue.enqueue({
                    "context": backend_context,
                    "text": backend_context.get("text", ""),
                })
                logger.info(f"[{chat_id}] 待确认任务已入队 (队列长度 {queue.size()})")
            else:
                task = asyncio.create_task(self._run_polling_loop(backend_context))
                self.generation_tasks[chat_id] = task
                logger.info(f"[{chat_id}] 待确认任务已直接启动（后脑空闲）")

            session.pop("pending_backend_enqueue", None)
        elif decision == "cancel":
            session.pop("pending_backend_enqueue", None)
            logger.info(f"[{chat_id}] 用户取消待确认入队任务")
        else:
            logger.info(f"[{chat_id}] 待确认入队：用户回复不明确，继续等待确认")

        if reply:
            sent_ids = await self._send_split_message(
                chat_id,
                reply,
                reply_to_message_id=reply_target_message_id(context),
            )
            metadata = {"source": "front", "stage": "queue_confirm"}
            if sent_ids:
                metadata["platform_message_ids"] = sent_ids

            self.message_history.add_message(
                platform=platform,
                chat_id=storage_id,
                role="assistant",
                content=reply,
                user_id="assistant",
                metadata=metadata,
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
            )
            session_history.append({"role": "assistant", "content": reply})

        if len(session_history) > 20:
            session["history"] = session_history[-20:]

        return True

    async def _generate_enqueue_confirmation_reply(
        self,
        chat_id: str,
        pending_request_text: str,
        queue_summary: str,
        progress_summary: str,
    ) -> str:
        """让前脑生成“是否入队”的二次确认消息。"""
        soul = get_soul_prompt()
        identity_context = load_identity_context(include_schedule=False)
        system_prompt = render_template(
            "queue_confirmation.jinja",
            "ask_system",
            soul=soul,
            identity_context=identity_context,
        )
        user_prompt = render_template(
            "queue_confirmation.jinja",
            "ask_user",
            pending_request_text=pending_request_text,
            queue_summary=queue_summary,
            progress_summary=progress_summary,
        )

        response = ""
        try:
            stream = self._chat_stream_wrapper(
                self.fast_llm,
                chat_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=None,
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += str(chunk.get("content", ""))
                elif isinstance(chunk, str):
                    response += chunk
        except Exception:
            logger.warning(f"[{chat_id}] 生成入队确认消息失败，使用模板回退", exc_info=True)
            response = render_template(
                "queue_confirmation.jinja",
                "ask_fallback",
                pending_request_text=pending_request_text,
                queue_summary=queue_summary,
            )

        return self._strip_timestamp_markers(response.strip())

    async def _detect_enqueue_confirmation_decision(
        self,
        chat_id: str,
        user_text: str,
        pending_request_text: str,
        queue_summary: str,
    ) -> tuple[str, str]:
        """识别用户对“是否入队”的确认意图。"""
        soul = get_soul_prompt()
        identity_context = load_identity_context(include_schedule=False)
        system_prompt = render_template(
            "queue_confirmation.jinja",
            "detect_system",
            soul=soul,
            identity_context=identity_context,
        )
        user_prompt = render_template(
            "queue_confirmation.jinja",
            "detect_user",
            user_text=user_text,
            pending_request_text=pending_request_text,
            queue_summary=queue_summary,
        )

        response = ""
        try:
            stream = self._chat_stream_wrapper(
                self.fast_llm,
                chat_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=[],
                tools=None,
            )
            async for chunk in stream:
                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    response += str(chunk.get("content", ""))
                elif isinstance(chunk, str):
                    response += chunk
        except Exception:
            logger.warning(f"[{chat_id}] 入队确认意图检测失败，默认 unclear", exc_info=True)
            return "unclear", render_template("queue_confirmation.jinja", "unclear_fallback")

        response = response.strip()
        decision_match = self._CONFIRM_DECISION_PATTERN.search(response)
        decision = decision_match.group(1).lower() if decision_match else "unclear"

        reply_match = self._CONFIRM_REPLY_PATTERN.search(response)
        reply = reply_match.group(1).strip() if reply_match else ""
        reply = self._strip_timestamp_markers(reply)

        if not reply:
            if decision == "confirm":
                reply = render_template("queue_confirmation.jinja", "confirm_fallback")
            elif decision == "cancel":
                reply = render_template("queue_confirmation.jinja", "cancel_fallback")
            else:
                reply = render_template("queue_confirmation.jinja", "unclear_fallback")

        return decision, reply

    async def _send_split_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: str = "",
        chat_type: str = "",
    ) -> list[str]:
        """按 [SPLIT] 或 [SPLIT:delay] 规则发送消息，返回平台消息 ID 列表。

        chat_type 非空时透传给适配器（OneBot 用它区分群/私聊，消除歧义）。
        """
        sent_message_ids: list[str] = []
        text = sanitize_adapter_output_text(text)
        if not text:
            return sent_message_ids
        send_kwargs: Dict[str, Any] = {"reply_to_message_id": reply_to_message_id}
        if chat_type:
            send_kwargs["chat_type"] = chat_type
        # fallback reply 只属于首个实际发出的语义分段；显式 [reply:ID] 仍由各分段自行解释。
        pending_reply_to_message_id = reply_to_message_id
        # _SPLIT_MARKER_PATTERN 现在返回 [text_part, delay_str, text_part, ...]
        parts = self._SPLIT_MARKER_PATTERN.split(text)

        for i in range(0, len(parts), 2):
            part = parts[i].strip()
            if part:
                send_kwargs["reply_to_message_id"] = pending_reply_to_message_id
                msg_ids = await self._send_platform_message(
                    chat_id,
                    part,
                    **send_kwargs,
                )
                if msg_ids:
                    sent_message_ids.extend(str(mid) for mid in msg_ids)
                    pending_reply_to_message_id = ""

            # 如果后面还有 delay，则执行停顿（最后一个 part 后面没有 delay 所以用 i+1 检查）
            if i + 1 < len(parts) and parts[i+1] is not None:
                try:
                    delay_val = float(parts[i+1])
                    if delay_val > 0:
                        await asyncio.sleep(delay_val)
                except ValueError:
                    pass
        return sent_message_ids

    # ------------------------------------------------------------------
    # 斜杠命令处理子方法
    # ------------------------------------------------------------------

    async def _cmd_start(self, chat_id: str, chat_type: str, user_id: str):
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        for runtime_key in self._runtime_keys_for_scope(memory_scope_id):
            if runtime_key in self.generation_tasks:
                self.generation_tasks[runtime_key].cancel()
        await asyncio.sleep(0.1)
        self.sessions[chat_id] = {"history": [], "interrupted_thought": ""}
        self.message_history.clear_chat_history(platform, storage_id, keep_pinned=True, memory_scope_id=memory_scope_id)
        for runtime_key in self._runtime_keys_for_scope(memory_scope_id):
            status = self.worker_status.get(runtime_key)
            if status:
                status.finish()
        queue = self._get_scope_queue_for_runtime(chat_id)
        if queue:
            await queue.clear()
        await self._send_platform_message(chat_id, "N.O.R.A. Core 已启动。")
        logger.info(f"[{chat_id}] Session已重置 by /start command.")

    async def _cmd_reload_models(self, chat_id: str):
        try:
            self._reload_llm_clients()
            await self._send_platform_message(chat_id, "✅ 模型配置已重新加载。")
            logger.info(f"[{chat_id}] 模型配置已通过 /reload_models 刷新。")
        except Exception as e:
            logger.error(f"[{chat_id}] /reload_models 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 模型刷新失败: {e}")

    async def _cmd_clear(self, chat_id: str, chat_type: str, user_id: str):
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        try:
            for runtime_key in self._runtime_keys_for_scope(memory_scope_id):
                if runtime_key in self.generation_tasks:
                    self.generation_tasks[runtime_key].cancel()
            await asyncio.sleep(0.1)
            self.sessions.pop(chat_id, None)
            queue = self._get_scope_queue_for_runtime(chat_id)
            if queue:
                await queue.clear()
            for runtime_key in self._runtime_keys_for_scope(memory_scope_id):
                status = self.worker_status.get(runtime_key)
                if status:
                    status.finish()
            self.message_history.clear_chat_history(platform, storage_id, keep_pinned=False, memory_scope_id=memory_scope_id)
            await self._send_platform_message(chat_id, "✅ 已清空跨平台共享的聊天记录（所有平台/窗口的共享上下文）。")
            logger.info(f"[{chat_id}] 聊天历史已通过 /clear 命令清空（共享作用域 {memory_scope_id}）。")
        except Exception as e:
            logger.error(f"[{chat_id}] 清空历史记录时出错: {e}")
            await self._send_platform_message(chat_id, "❌ 清空历史记录时发生错误。")

    async def _cmd_stop(self, chat_id: str, chat_type: str = "private", user_id: str = ""):
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id
        try:
            busy_runtime = self.get_busy_backend_runtime(memory_scope_id) or chat_id
            if busy_runtime in self.generation_tasks:
                self.generation_tasks[busy_runtime].cancel()
                await asyncio.sleep(0.1)
            busy_session = self.sessions.setdefault(
                busy_runtime, {"history": [], "interrupted_thought": "", "pending_text": ""}
            )
            busy_session["_interrupted_by_frontend"] = True
            self.sessions.pop(chat_id, None)
            queue = self._get_scope_queue_for_runtime(chat_id)
            if queue:
                await queue.clear()
            status = self.worker_status.get(busy_runtime)
            if status:
                status.finish()
            self._cancel_followup_timer(chat_id)
            # 标记被前端打断，避免 finally 再次调度排队任务
            session = self.sessions.setdefault(chat_id, {"history": [], "interrupted_thought": "", "pending_text": ""})
            session["_interrupted_by_frontend"] = True
            reply = "✅ 已停止当前所有任务。"
            await self._send_platform_message(chat_id, reply)
            self.message_history.add_message(
                platform=platform,
                chat_id=storage_id,
                role="assistant",
                content=reply,
                user_id="assistant",
                memory_scope_id=memory_scope_id,
                place_scope_id=place_scope_id,
            )
            logger.info(f"[{chat_id}] 已通过 /stop 命令停止所有任务。")
        except Exception as e:
            logger.error(f"[{chat_id}] 停止任务时出错: {e}")
            await self._send_platform_message(chat_id, "❌ 停止任务时发生错误。")

    async def _cmd_regenerate_proactive(self, chat_id: str, text: str):
        if not self.scheduler:
            await self._send_platform_message(chat_id, "❌ Scheduler 未启用，无法重新生成主动消息计划。")
            return

        mode = "replace"
        parts = text.strip().split()
        if len(parts) >= 2 and parts[1].lower() in ("append", "replace"):
            mode = parts[1].lower()
        clear_existing = (mode != "append")

        try:
            result = await self.scheduler.regenerate_today_plan(clear_existing=clear_existing)
            if result.get("success"):
                msg = (
                    f"✅ 主动消息计划已{'覆盖重建' if clear_existing else '追加生成'}\n"
                    f"日期: {result.get('date', '-')}\n"
                    f"本次新增: {result.get('count', 0)}\n"
                    f"今日总未触发: {result.get('total_today', 0)}"
                )
            else:
                msg = f"❌ 重新生成失败: {result.get('message', 'unknown error')}"
            await self._send_platform_message(chat_id, msg)
        except Exception as e:
            logger.error(f"[{chat_id}] /regenerate_proactive 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 重新生成失败: {e}")

    async def _cmd_schedule_today(self, chat_id: str):
        if not self.scheduler:
            await self._send_platform_message(chat_id, "❌ Scheduler 未启用，无法查看今日计划。")
            return
        try:
            today_plan = self.scheduler.get_today_plan()
            if not today_plan:
                await self._send_platform_message(chat_id, "📭 今日没有未触发的主动消息计划。")
                return
            sorted_plan = sorted(today_plan, key=lambda x: x.get("trigger_time", ""))
            lines = ["🗓️ 今日主动消息计划（未触发）:"]
            for i, item in enumerate(sorted_plan, start=1):
                trigger_time = item.get("trigger_time", "")
                hhmm = trigger_time[11:16] if len(trigger_time) >= 16 else trigger_time
                reason = item.get("reason", "") or "(无缘由)"
                kind = item.get("message_kind", "autonomous")
                kind_label = "明确提醒" if kind == "explicit" else "自主消息"
                lines.append(f"{i}. {hhmm} [{kind_label}] - {reason}")
            lines.append(f"\n共 {len(sorted_plan)} 条")
            await self._send_platform_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"[{chat_id}] /schedule_today 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 获取今日计划失败: {e}")

    async def _dispatch_non_interrupting_cmd(
        self, chat_id: str, chat_type: str, user_id: str, stripped: str
    ) -> None:
        """分发不打断前脑生成的命令。"""
        if re.search(r"(?m)^\s*/debug\b", stripped):
            await self._cmd_debug(chat_id)
        elif re.search(r"(?m)^\s*/status\b", stripped):
            await self._cmd_status(chat_id, chat_type, user_id)
        elif re.search(r"(?m)^\s*/custom_scope\b", stripped):
            await self._cmd_custom_scope(chat_id, stripped)
        elif re.search(r"(?m)^\s*/undo\b", stripped):
            await self._cmd_undo(chat_id, chat_type, user_id)
        elif re.search(r"(?m)^\s*/set_stream\b", stripped) or re.search(r"(?m)^\s*/nonstream\b", stripped):
            await self._cmd_set_stream(chat_id, chat_type, user_id, stripped)
        elif re.search(r"(?m)^\s*/context\b", stripped):
            await self._cmd_context(chat_id, chat_type, user_id)

    async def _cmd_status(self, chat_id: str, chat_type: str, user_id: str):
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        try:
            lines = ["📊 **N.O.R.A. Core 状态报告**\n"]
            
            # 后脑状态
            busy_runtime = self.get_busy_backend_runtime(memory_scope_id)
            status = self.worker_status.get(busy_runtime) if busy_runtime else self.worker_status.get(chat_id)
            if status and status.busy:
                elapsed = int(time.time() - status.started_at)
                lines.append("🧠 后脑 (Back Brain): 🔴 忙碌")
                if busy_runtime and busy_runtime != chat_id:
                    lines.append(f"  来源地点: {busy_runtime}")
                lines.append(f"  任务: \"{status.original_query[:80]}\"")
                lines.append(f"  阶段: {status.phase}")
                if status.detail:
                    lines.append(f"  详情: {status.detail}")
                lines.append(f"  进度: 第 {status.current_turn}/{status.max_turns} 轮")
                lines.append(f"  耗时: {elapsed}s")
                if status.tool_history:
                    recent = status.tool_history[-3:]
                    lines.append("  最近工具:")
                    for step in recent:
                        lines.append(f"    • {step[:80]}")
            else:
                lines.append("🧠 后脑 (Back Brain): 🟢 空闲")
            
            # 前脑信息
            lines.append("")
            front_task = self.front_brain_tasks.get(chat_id) if hasattr(self, "front_brain_tasks") else None
            front_busy = bool(front_task and not front_task.done())
            review_active = bool(getattr(self, "_in_review_loop", {}).get(chat_id, False))
            polling_active = bool(getattr(self, "_polling_active", {}).get(chat_id, False))
            if front_busy:
                lines.append("⚡ 前脑 (Fast Brain): 🟡 正在生成回复")
            elif review_active:
                lines.append("⚡ 前脑 (Fast Brain): 🟡 正在审查后脑结果")
            elif polling_active:
                lines.append("⚡ 前脑 (Fast Brain): 🟡 正在轮询后脑")
            else:
                lines.append("⚡ 前脑 (Fast Brain): 🟢 空闲")

            # 待处理 / 已注册 followup
            pending_followups = [cid for cid, t in (getattr(self, "_followup_timers", {}) or {}).items() if t and not t.done()]
            if pending_followups:
                lines.append(f"  followup 定时器: {len(pending_followups)} 个活跃")

            # 全局在线状态
            lines.append("")
            ai_state = get_ai_state_summary()
            lines.append("🌐 在线状态:")
            lines.append(f"  presence: {ai_state['presence']}")
            lines.append(f"  generating: {ai_state['is_generating']}")
            lines.append(f"  backend_busy: {ai_state['is_backend_busy']}")
            lines.append(f"  skip_proactive: {ai_state['should_skip_proactive']}")
            
            # 模型信息
            lines.append("")
            lines.append("🤖 模型配置:")
            try:
                smart_model = config.get_model_name("smart")
                fast_model = config.get_model_name("fast")
                image_model = config.get_model_name("image")
                lines.append(f"  smart: {smart_model}")
                lines.append(f"  fast: {fast_model}")
                lines.append(f"  image: {image_model}")
            except Exception:
                lines.append("  (无法获取模型信息)")
            
            # Scheduler 状态
            lines.append("")
            if self.scheduler:
                lines.append(f"📅 Scheduler: {'🟢 运行中' if self.scheduler._scheduler.running else '🔴 停止'}")
                lines.append(f"  AI 状态: {ai_state['presence']}")
                today_plan = self.scheduler.get_today_plan()
                active_alarms = self.scheduler.list_alarms()
                lines.append(f"  今日待触发: {len(today_plan)} 条")
                lines.append(f"  活跃闹钟: {len(active_alarms)} 个")
            else:
                lines.append("📅 Scheduler: 🔴 未启用")
            
            # 排队消息
            queue = self._get_scope_queue_for_runtime(chat_id)
            if queue and queue.size() > 0:
                lines.append(f"\n{queue.get_queue_summary()}")
            
            # RAG / 消息历史
            lines.append("")
            lines.append(f"🗄️ RAG: {'🟢 启用' if self.rag.enabled else '🔴 离线'}")
            lines.append(f"💰 成本跟踪: {'🟢 启用' if self.cost_tracking_enabled else '🔴 禁用'}")
            
            # 对话分段统计
            try:
                stats = self.message_history.get_statistics(platform, storage_id, memory_scope_id=memory_scope_id)
                lines.append("")
                lines.append(f"📋 对话段落: {stats.get('total_sessions', 0)} 个已封闭")
                active_msgs = stats.get('active_messages', 0)
                if active_msgs > 0:
                    lines.append(f"  当前活跃对话: {active_msgs} 条消息")
            except Exception:
                pass
            
            await self._send_platform_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"[{chat_id}] /status 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 获取状态失败: {e}")

    async def _cmd_context(self, chat_id: str, chat_type: str, user_id: str):
        """显示当前上下文窗口使用情况。"""
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id
        try:
            context_msgs = self.message_history.get_context_messages(
                platform, storage_id, memory_scope_id=memory_scope_id, current_place_scope_id=place_scope_id
            )
            stats = self.message_history.get_statistics(platform, storage_id, memory_scope_id=memory_scope_id)

            total_chars = sum(len(msg.get("content", "")) for msg in context_msgs)
            estimated_tokens = int(total_chars / 1.5)

            lines = [
                "📊 上下文窗口状态",
                "",
                f"当前上下文消息数: {len(context_msgs)}",
                f"估算 Token 数: ~{estimated_tokens:,}",
                f"总字符数: {total_chars:,}",
                "",
                f"活跃消息: {stats.get('raw_messages', 0)}",
                f"已归档: {stats.get('archived_messages', 0)}",
                f"置顶: {stats.get('pinned_messages', 0)}",
                f"对话段落: {stats.get('total_sessions', 0)}",
                f"L1 总结: {stats.get('level1_summaries', 0)}",
            ]
            await self._send_platform_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"[{chat_id}] /context 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 获取上下文信息失败: {e}")

    async def _cmd_debug(self, chat_id: str):
        current = self.debug_mode.get(chat_id, False)
        self.debug_mode[chat_id] = not current
        state_str = "🟢 已开启" if self.debug_mode[chat_id] else "🔴 已关闭"
        await self._send_platform_message(chat_id, f"🐛 Debug 模式: {state_str}\n开启后每次工具调用、技能执行、思考阶段都会实时推送详情。")
        logger.info(f"[{chat_id}] Debug 模式切换为: {self.debug_mode[chat_id]}")

    async def _cmd_custom_scope(self, chat_id: str, text: str):
        """设置 CUSTOM.md 注入范围。"""
        parts = text.strip().split()
        args = [p.strip().lower() for p in parts[1:]]
        try:
            cfg = config.get_config() or {}
            current_scopes = config.get_custom_injection_scopes()
            if not args:
                if not current_scopes:
                    scope_desc = "all"
                elif any(s in ("none", "off") for s in current_scopes):
                    scope_desc = "none"
                else:
                    scope_desc = ", ".join(current_scopes)
                await self._send_platform_message(
                    chat_id,
                    "🧭 CUSTOM 注入范围\n"
                    f"当前: {scope_desc}\n"
                    "用法: /custom_scope（按钮多选）或 /custom_scope fast smart | image | coder | all | none",
                )
                return

            if "all" in args:
                scopes_to_set = []
            elif any(a in ("none", "off") for a in args):
                scopes_to_set = ["none"]
            else:
                allowed = {"fast", "smart", "image", "coder"}
                invalid = [a for a in args if a not in allowed]
                if invalid:
                    await self._send_platform_message(
                        chat_id,
                        "❌ 无效范围: " + ", ".join(invalid) + "。可选: fast, smart, image, coder, all, none",
                    )
                    return
                scopes_to_set = list(dict.fromkeys(args))

            cfg.setdefault("custom_injection", {})["scopes"] = scopes_to_set
            config.save_config(cfg)

            if not scopes_to_set:
                scope_desc = "all"
            elif any(s in ("none", "off") for s in scopes_to_set):
                scope_desc = "none"
            else:
                scope_desc = ", ".join(scopes_to_set)

            await self._send_platform_message(chat_id, f"✅ CUSTOM 注入范围已更新为: {scope_desc}")
        except Exception as e:
            logger.error(f"[{chat_id}] /custom_scope 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 设置失败: {e}")

    async def _cmd_undo(self, chat_id: str, chat_type: str, user_id: str):
        """撤销上一条已发送的 assistant 消息。"""
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        try:
            removed = self.message_history.delete_last_assistant_message(
                platform=platform,
                chat_id=storage_id,
                source=None,
            )
            if removed is None:
                await self._send_platform_message(chat_id, "❌ 没有可撤销的消息。")
                return

            # 内存会话历史同步删除最近一条 assistant（不连带用户）
            session = self.sessions.get(chat_id)
            if session and session.get("history"):
                hist = session["history"]
                for idx in range(len(hist) - 1, -1, -1):
                    if hist[idx].get("role") == "assistant":
                        hist.pop(idx)
                        break

            # 尝试删除平台消息
            md = removed.get("metadata") or {}
            platform_ids = md.get("platform_message_ids") or []
            delete_ok = False
            delete_attempted = False
            if platform_ids and getattr(self.adapter, "platform_features", None):
                feats = self.adapter.platform_features
                if getattr(feats, "supports_delete_message", False):
                    delete_attempted = True
                    for pid in platform_ids:
                        try:
                            ok = await self._delete_platform_message(chat_id, pid)
                            delete_ok = delete_ok or ok
                        except Exception:
                            logger.warning(f"[{chat_id}] 平台删除消息 {pid} 失败", exc_info=True)

            if delete_attempted:
                if delete_ok:
                    await self._send_platform_message(chat_id, "✅ 已撤销上一条 AI 消息（聊天界面已删除）。")
                else:
                    await self._send_platform_message(chat_id, "✅ 已撤销存档；⚠️ 平台消息删除失败或权限不足。")
            else:
                await self._send_platform_message(chat_id, "✅ 已撤销上一条 AI 消息（仅存档层面）。")
        except Exception as e:
            logger.error(f"[{chat_id}] /undo 执行失败: {e}", exc_info=True)
            await self._send_platform_message(chat_id, f"❌ 撤销失败: {e}")

    async def _cmd_set_stream(self, chat_id: str, chat_type: str, user_id: str, text: str):
        """切换 per-chat 非流式输出模式。/set_stream on|off（on=一次性输出，off=流式），空参=查询当前状态。"""
        identity = self._identity_for_runtime_key(chat_id)
        platform = identity.platform
        storage_id = identity.storage_id
        memory_scope_id = identity.memory_scope_id
        place_scope_id = identity.place_scope_id
        parts = text.strip().split()
        arg = parts[1].lower() if len(parts) >= 2 else ""
        current = self.non_stream_flags.get(chat_id, self.default_non_stream)

        if arg in ("on", "off"):
            new_val = arg == "on"
            self.non_stream_flags[chat_id] = new_val
            msg = "✅ 已启用一次性输出（非流式）" if new_val else "✅ 已恢复流式输出"
        else:
            msg = "ℹ️ 当前输出模式: " + ("一次性输出（非流式）" if current else "流式输出")

        await self._send_platform_message(chat_id, msg)
        self.message_history.add_message(
            platform=platform,
            chat_id=storage_id,
            role="assistant",
            content=msg,
            user_id="assistant",
            memory_scope_id=memory_scope_id,
            place_scope_id=place_scope_id,
        )
