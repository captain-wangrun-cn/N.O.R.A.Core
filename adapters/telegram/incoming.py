from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

from .message import has_bot_mention, strip_bot_mention

logger = logging.getLogger(__name__)

MENTION_ONLY_TEXT = "[群聊单独艾特: 用户只艾特了你，没有附加文字。请自然地回应。]"


class TelegramIncomingMixin:
    def _is_group_chat(self, update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.type in ["group", "supergroup"])

    def _message_text_for_mention(self, update: Update) -> str:
        if not update.message:
            return ""
        return update.message.text or update.message.caption or ""

    def _has_bot_mention(self, update: Update) -> bool:
        text_to_check = self._message_text_for_mention(update)
        return has_bot_mention(text_to_check, self.bot_username)

    def _is_known_media_group_continuation(self, update: Update) -> bool:
        if not update.message:
            return False
        media_group_id = getattr(update.message, "media_group_id", None)
        return bool(media_group_id and media_group_id in self._media_group_buffers)

    def _strip_trigger_mention_if_group(self, update: Update, text: str) -> str:
        if self._is_group_chat(update):
            return strip_bot_mention(text, self.bot_username)
        return text

    async def _should_process_message(self, update: Update) -> bool:
        """判断是否应该处理收到的消息。"""
        if not update.effective_chat:
            return False
        
        # 1. 私聊总是处理
        if update.effective_chat.type == 'private':
            return True
        
        # 2. 群聊/超级群聊
        if update.effective_chat.type in ['group', 'supergroup']:
            if not update.message:
                return False
            if self._is_known_media_group_continuation(update):
                return True
            return self._has_bot_mention(update)
        
        # 默认不处理
        return False

    async def _handle_incoming_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._should_process_message(update):
            return

        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id # Set current chat_id
        mention_only_trigger = self._is_group_chat(update) and self._has_bot_mention(update)
        text = update.message.text if update.message else ""
        text = self._strip_trigger_mention_if_group(update, text)
        if not text and mention_only_trigger:
            text = MENTION_ONLY_TEXT

        
        # 如果是空消息（例如，只有一张图片），也需要继续处理
        if not text and not (update.message and (update.message.photo or update.message.document or update.message.sticker)):
            return
        
        # 处理回复消息
        if update.message:
            reply_info = await self._extract_reply_info(update.message)
            if reply_info:
                text = f"[回复: {reply_info}]\n{text}"

        # 处理转发消息：提取来源信息
        if update.message:
            forward_info = None
            if hasattr(update.message, "forward_origin") and update.message.forward_origin:
                origin = update.message.forward_origin
                if hasattr(origin, "sender_user") and origin.sender_user:
                    forward_info = origin.sender_user.first_name
                    if origin.sender_user.last_name:
                        forward_info += f" {origin.sender_user.last_name}"
                elif hasattr(origin, "chat") and origin.chat:
                    forward_info = origin.chat.title or origin.chat.first_name or "未知频道"
                elif hasattr(origin, "sender_user_name") and origin.sender_user_name:
                    forward_info = origin.sender_user_name
            elif hasattr(update.message, "forward_from") and update.message.forward_from:
                forward_info = update.message.forward_from.first_name
            elif hasattr(update.message, "forward_from_chat") and update.message.forward_from_chat:
                forward_info = update.message.forward_from_chat.title or "未知频道"
            if forward_info:
                text = f"[转发自: {forward_info}]\n{text}"
        
        if self._aggregator:
            full_context = await self._context_from_update_with_group(update, text)
            await self._aggregator.add_message(chat_id, text or "", full_context)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理图片消息（支持单图和多图相册 media_group）"""
        if not await self._should_process_message(update):
            return

        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        if not update.message or not update.message.photo:
            return
        photo = update.message.photo[-1]  # 获取最高质量版本
        caption = self._strip_trigger_mention_if_group(update, update.message.caption or "")
        media_group_id = update.message.media_group_id
        
        # 下载图片
        file = await photo.get_file()
        abs_file_path = os.path.join(self.telegram_data_dir, f"photo_{photo.file_id}.jpg")
        await file.download_to_drive(abs_file_path)
        rel_file_path = os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')
        
        reply_info = await self._extract_reply_info(update.message)
        
        if media_group_id:
            # --- 媒体组（相册）：缓冲后统一处理 ---
            if media_group_id not in self._media_group_buffers:
                self._media_group_buffers[media_group_id] = {
                    "photos": [],
                    "caption": "",
                    "chat_id": chat_id,
                    "update": update,
                    "context": await self._context_from_update_with_group(update, ""),
                    "chat_type": update.effective_chat.type,
                    "reply_info": reply_info,
                    "platform": "telegram",
                    "message_ids": [],
                }
            
            self._media_group_buffers[media_group_id]["photos"].append(f"[image: {rel_file_path}]")
            if update.message:
                self._media_group_buffers[media_group_id]["message_ids"].append(str(update.message.message_id))
            # caption 只取第一张非空的（Telegram 相册只允许第一张图有 caption）
            if caption and not self._media_group_buffers[media_group_id]["caption"]:
                self._media_group_buffers[media_group_id]["caption"] = caption
            
            # 取消旧定时器，重新设置（等待所有图片到齐）
            old_timer = self._media_group_timers.pop(media_group_id, None)
            if old_timer and not old_timer.done():
                old_timer.cancel()
            self._media_group_timers[media_group_id] = asyncio.create_task(
                self._flush_media_group(media_group_id)
            )
            logger.info(f"[{chat_id}] 收到相册图片: {rel_file_path} (group={media_group_id})")
        else:
            # --- 单张图片：直接处理 ---
            text = f"[image: {rel_file_path}]"
            if caption:
                text += f"\n{caption}"
            if reply_info:
                text = f"[回复: {reply_info}]\n{text}"
            
            if self._aggregator:
                full_context = await self._context_from_update_with_group(update, text)
                await self._aggregator.add_message(chat_id, text, full_context)
            
            logger.info(f"[{chat_id}] 收到图片: {rel_file_path}")

    async def _flush_media_group(self, media_group_id: str):
        """等待短暂时间后，将同一媒体组的所有图片/视频合并为一条消息发送给聚合器。"""
        await asyncio.sleep(1.0)  # 等待 1 秒让 Telegram 发完所有媒体

        buf = self._media_group_buffers.pop(media_group_id, None)
        self._media_group_timers.pop(media_group_id, None)
        if not buf or not buf["photos"]:
            return

        # 构造包含所有媒体的消息文本（每项已是完整的 [image: ...] 或 [video: ...] 标签）
        media_tags = "\n".join(buf["photos"])
        text = media_tags
        if buf["caption"]:
            text += f"\n{buf['caption']}"
        if buf.get("reply_info"):
            text = f"[回复: {buf['reply_info']}]\n{text}"
        
        chat_id = buf["chat_id"]
        platform_ids = [str(mid) for mid in buf.get("message_ids", []) if mid is not None]
        platform_msg_id = platform_ids[0] if platform_ids else None
        if self._aggregator:
            full_context = dict(buf.get("context") or {})
            full_context.update(
                {
                    "text": text,
                    "platform_message_id": platform_msg_id,
                    "platform_message_ids": platform_ids,
                }
            )
            source_update = buf.get("update")
            source_chat = getattr(source_update, "effective_chat", None)
            if source_chat is not None:
                full_context = await self._enrich_group_context(full_context, source_chat, update=source_update)
            await self._aggregator.add_message(chat_id, text, full_context)
        
        logger.info(f"[{chat_id}] 媒体组合并完成: {len(buf['photos'])} 个媒体 (group={media_group_id})")

    async def _handle_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理视频消息（包括普通视频和圆形视频消息 video_note）"""
        if not await self._should_process_message(update):
            return

        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id

        if not update.message:
            return

        video = update.message.video or update.message.video_note
        if not video:
            return

        caption = self._strip_trigger_mention_if_group(update, update.message.caption or "")
        file_id = video.file_id
        file_ext = ".mp4"
        if hasattr(video, "mime_type") and video.mime_type:
            import mimetypes as _mt
            ext = _mt.guess_extension(video.mime_type)
            if ext:
                file_ext = ext

        file = await video.get_file()
        abs_file_path = os.path.join(self.telegram_data_dir, f"video_{file_id}{file_ext}")
        await file.download_to_drive(abs_file_path)
        rel_file_path = os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')

        reply_info = await self._extract_reply_info(update.message)
        media_group_id = update.message.media_group_id

        original_filename = getattr(video, "file_name", None) or ""
        video_tag = f"[video: {rel_file_path}]"
        if original_filename:
            video_tag += f"\n(文件名: {original_filename})"

        if media_group_id:
            # 媒体组：加入同一缓冲（和图片共用），等待合并
            if media_group_id not in self._media_group_buffers:
                self._media_group_buffers[media_group_id] = {
                    "photos": [],
                    "caption": "",
                    "chat_id": chat_id,
                    "update": update,
                    "context": await self._context_from_update_with_group(update, ""),
                    "chat_type": update.effective_chat.type,
                    "reply_info": reply_info,
                    "platform": "telegram",
                    "message_ids": [],
                }
            self._media_group_buffers[media_group_id]["photos"].append(video_tag)
            if update.message:
                self._media_group_buffers[media_group_id]["message_ids"].append(str(update.message.message_id))
            if caption and not self._media_group_buffers[media_group_id]["caption"]:
                self._media_group_buffers[media_group_id]["caption"] = caption

            old_timer = self._media_group_timers.pop(media_group_id, None)
            if old_timer and not old_timer.done():
                old_timer.cancel()
            self._media_group_timers[media_group_id] = asyncio.create_task(
                self._flush_media_group(media_group_id)
            )
            logger.info(f"[{chat_id}] 收到媒体组视频: {rel_file_path} (group={media_group_id})")
        else:
            # 单独视频：直接处理
            text = video_tag
            if caption:
                text += f"\n{caption}"
            if reply_info:
                text = f"[回复: {reply_info}]\n{text}"

            if self._aggregator:
                full_context = await self._context_from_update_with_group(update, text)
                await self._aggregator.add_message(chat_id, text, full_context)

            logger.info(f"[{chat_id}] 收到视频: {rel_file_path}")

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理文档消息"""
        if not await self._should_process_message(update):
            return

        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        if not update.message or not update.message.document:
            return
        document = update.message.document
        caption = self._strip_trigger_mention_if_group(update, update.message.caption or "")
        
        # 下载文档
        file = await document.get_file()
        file_name = document.file_name or f"document_{document.file_id}"
        abs_file_path = os.path.join(self.telegram_data_dir, file_name)
        await file.download_to_drive(abs_file_path)
        rel_file_path = os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')
        
        # 构造消息
        reply_info = await self._extract_reply_info(update.message)
        text = f"[file: {rel_file_path}]"
        if caption:
            text += f"\n{caption}"
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            full_context = await self._context_from_update_with_group(update, text)
            await self._aggregator.add_message(chat_id, text, full_context)
        
        logger.info(f"[{chat_id}] 收到文档: {rel_file_path}")

    async def _handle_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理贴纸消息"""
        if not await self._should_process_message(update):
            return

        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        if not update.message or not update.message.sticker:
            return
        sticker = update.message.sticker
        emoji = sticker.emoji or "🎨"
        set_name = sticker.set_name or "未知贴纸包"
        
        # 下载贴纸
        file = await sticker.get_file()
        ext = "webm" if sticker.is_video else "webp"
        abs_file_path = os.path.join(self.telegram_data_dir, f"sticker_{sticker.file_id}.{ext}")
        await file.download_to_drive(abs_file_path)
        rel_file_path = os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')
        
        # 构造消息
        reply_info = await self._extract_reply_info(update.message)
        text = f"[sticker: {emoji} from {set_name}]\n[file: {rel_file_path}]"
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            full_context = await self._context_from_update_with_group(update, text)
            await self._aggregator.add_message(chat_id, text, full_context)
        
        logger.info(f"[{chat_id}] 收到贴纸: {rel_file_path}")
