from __future__ import annotations

import io
import json
import logging
import os
from typing import Any, Dict, Optional

from telegram import Message

from .constants import COMPRESS_TARGET_SIZE, HISTORICAL_REPLY_IMAGE_DIRECT_MARKER
from adapters.media_path import build_media_subdir, infer_scene

logger = logging.getLogger(__name__)


class TelegramReplyMixin:
    def _pick_image_id_from_metadata(self, metadata: Dict[str, Any], reply_msg_id: str) -> Optional[str]:
        """Pick the image/video id that belongs to the replied Telegram message when possible."""
        if not isinstance(metadata, dict):
            return None
        by_platform_id = metadata.get("image_ids_by_platform_message_id") or {}
        if isinstance(by_platform_id, dict):
            exact = by_platform_id.get(str(reply_msg_id))
            if exact:
                return str(exact)
        image_id = metadata.get("image_id")
        if image_id:
            return str(image_id)
        image_ids = metadata.get("image_ids") or []
        if isinstance(image_ids, list) and image_ids:
            first = image_ids[0]
            return str(first) if first else None
        return None

    async def _extract_reply_info(self, message: Message) -> Optional[str]:
        """提取回复消息的信息"""
        if not message.reply_to_message:
            return None

        reply_msg = message.reply_to_message
        reply_chat_id = str(reply_msg.chat.id)
        reply_msg_id = str(reply_msg.message_id)

        # 表情包优先重下文件并重建 [sticker: path] 标记，不要走下面的历史查找。
        # 原因：表情包**故意不进 ImageStore**（见 QUICK_REFERENCE §3），入库正文又被
        # media_placeholder_text 收敛成裸 "[表情包]"（core/message_handler.py），
        # 所以历史里根本没有路径可还原。只有 [sticker: path] 这个标记能让
        # extract_image_payloads → _describe_stickers 的 fast-image 链路跑起来；
        # 少了它，引用表情包就只剩一个没有内容的占位符。
        #
        # 用 getattr 而非 reply_msg.sticker：这条链路会收到各种精简过的消息对象
        # （测试里的 SimpleNamespace、其它 adapter 的伪造消息），少一个属性不该
        # 让整条入站解析崩掉。
        if getattr(reply_msg, "sticker", None):
            sticker_input = await self._redownload_replied_sticker_as_input(reply_msg)
            if sticker_input:
                return sticker_input

        # 从历史记录中获取回复内容
        history = self.message_history.get_context_messages("telegram", reply_chat_id)
        if not history:
            history = []

        logger.debug(
            f"[telegram] _extract_reply_info: reply_msg_id={reply_msg_id}, "
            f"chat_id={reply_chat_id}, history_size={len(history)}, "
            f"has_text={bool(reply_msg.text)}, has_caption={bool(reply_msg.caption)}, "
            f"has_photo={bool(reply_msg.photo)}"
        )

        # 查找对应的回复消息
        for msg in history:
            md = msg.get("metadata") or {}
            platform_ids = msg.get("platform_message_ids") or []
            db_message_id = str(msg.get("message_id")) if msg.get("message_id") is not None else None

            if reply_msg_id == db_message_id or reply_msg_id in platform_ids:
                logger.debug(f"[telegram] reply 命中历史消息: msg_id={reply_msg_id}, platform_ids={platform_ids}")
                content = msg.get("content", "")
                image_id = self._pick_image_id_from_metadata(md, reply_msg_id)
                image_input = None
                if image_id:
                    image_input = await self._image_id_to_image_input(image_id, reply_msg)
                if not image_input and reply_msg.photo:
                    image_input = await self._image_input_from_platform_message_id(
                        reply_msg, reply_chat_id, reply_msg_id, allow_redownload=True
                    )
                if image_input:
                    return f"{image_input}\n{content}" if content else image_input
                return content

        # 次级查找：回溯消息镜像库，扩大检索窗口，避免 raw_window 限制导致找不到
        try:
            log = getattr(self.message_history, "message_log", None)
            if log:
                recent_logs = log.get_recent_messages("telegram", reply_chat_id, limit=500)
                for row in recent_logs:
                    md_raw = row.get("metadata")
                    try:
                        md = json.loads(md_raw) if md_raw else {}
                    except Exception:
                        md = {}
                    platform_ids = md.get("platform_message_ids") or md.get("platform_message_id") or []
                    if isinstance(platform_ids, str):
                        platform_ids = [platform_ids]
                    platform_ids = [str(pid) for pid in platform_ids if pid is not None]

                    if reply_msg_id in platform_ids:
                        content = row.get("raw_content") or row.get("content_with_timestamp") or ""
                        image_id = self._pick_image_id_from_metadata(md, reply_msg_id)
                        image_input = None
                        if image_id:
                            image_input = await self._image_id_to_image_input(image_id, reply_msg)
                        if not image_input and reply_msg.photo:
                            image_input = await self._image_input_from_platform_message_id(
                                reply_msg, reply_chat_id, reply_msg_id, allow_redownload=True
                            )
                        if image_input:
                            return f"{image_input}\n{content}" if content else image_input
                        return content
        except Exception as e:
            logger.debug(f"[telegram] reply lookup in message_log failed: {e}")

        # 三级查找：直接去 ImageStore 通过 platform_message_id 反查图片文件。
        # message_history 镜像库可能已经裁剪掉这条消息，但只要图片入库时带上了
        # platform_message_id，ImageStore (MongoDB) 就还能拿回 file_path。
        if reply_msg.photo:
            try:
                image_input = await self._image_input_from_platform_message_id(
                    reply_msg, reply_chat_id, reply_msg_id, allow_redownload=False
                )
                if image_input:
                    caption_text = (reply_msg.caption or "").strip()
                    if caption_text:
                        return f"{image_input}\n{caption_text}"
                    return image_input
            except Exception as e:
                logger.debug(f"[telegram] reply 通过 ImageStore 反查图片文件失败: {e}")

        # 四级兜底：图片库里也没有这张图（可能已清理 / 入库失败 / 早期消息没入库）。
        # 此时直接重新下载这张被引用的图片，作为新图传给上游，让流程像新发图一样
        # 完整跑一遍（解析图片字节 → 入库 ImageStore → 后脑可视化）。
        if reply_msg.photo:
            try:
                image_input = await self._redownload_replied_photo_as_image_input(reply_msg)
                if image_input:
                    parts = [image_input]
                    caption_text = (reply_msg.caption or "").strip()
                    if caption_text:
                        parts.append(caption_text)
                    parts.append("(用户引用了一张历史图片，已为你重新下载)")
                    logger.info(
                        f"[telegram] reply 历史图片在 message_history / ImageStore 都未命中，"
                        f"已重新下载并走 image 模型/新图片入库流程"
                    )
                    return "\n".join(parts)
            except Exception as e:
                logger.warning(f"[telegram] reply 历史图片兜底重新下载失败: {e}")

        # 如果历史中没有找到对应消息，退回直接使用 Telegram 回复内容
        fallback_parts = []
        if reply_msg.text:
            fallback_parts.append(reply_msg.text)
        if reply_msg.caption:
            fallback_parts.append(reply_msg.caption)
        if reply_msg.photo:
            # 不要使用 [image: ...] 这种结构化标记，否则会触发上游的图片输入检测，
            # 但又因为 reply_photo 不是真实路径而无法加载，最终给用户错误提示。
            # 使用纯描述文本，让模型从语义上理解"用户引用了一张图片但当前没附上图片字节"。
            fallback_parts.append("(用户引用了一张历史图片，但本轮未重新附带图片内容)")

        result = "\n".join(part for part in fallback_parts if part) or None
        if result:
            logger.debug(f"[telegram] reply 使用 Telegram API 直接内容兜底 (历史未命中): msg_id={reply_msg_id}")
        else:
            logger.debug(f"[telegram] reply 最终返回 None: msg_id={reply_msg_id}, 无可用文本/图片内容")
        return result

    # ------------------------------------------------------------------
    # 回复历史图片的兜底支持
    # ------------------------------------------------------------------

    def _get_image_store(self):
        """懒加载并缓存 ImageStore 实例（共享 MongoDB 连接，便于反查历史图片文件）。"""
        cached = getattr(self, "_image_store_cache", None)
        if cached is not None:
            return cached
        try:
            from memory.image_store import ImageStore
            store = ImageStore()
        except Exception as e:
            logger.warning(f"[telegram] 初始化 ImageStore 失败: {e}")
            store = None
        # 即使失败也缓存 None，避免反复重试造成日志刷屏
        self._image_store_cache = store
        return store

    def _path_to_image_input(self, raw_path: str) -> Optional[str]:
        """把本地图片路径转成内部 [image: ...] 输入标记；路径不存在则返回 None。"""
        if not raw_path:
            return None
        resolved = self._resolve_local_media_path(raw_path)
        if not resolved:
            return None
        try:
            path_for_prompt = os.path.relpath(resolved, self.workspace_root).replace('\\', '/')
        except Exception:
            path_for_prompt = resolved
        return f"[image: {path_for_prompt}]"

    def _image_doc_to_image_input(self, doc: Dict[str, Any]) -> Optional[str]:
        """把 ImageStore 文档里的 file_path 转成内部 [image: ...] 输入标记。"""
        file_path = (doc or {}).get("file_path") or ""
        return self._path_to_image_input(file_path)

    def _historical_reply_image_note(self) -> str:
        """历史 reply 图片直送 image 模型时给 LLM 的内部说明。"""
        return (
            f"{HISTORICAL_REPLY_IMAGE_DIRECT_MARKER}\n"
            "（系统备注：用户回复的是一张历史图片；这张图已经作为本轮真实视觉输入提供给 image 模型。"
            "不要输出或提及 image_id、[image: ...]、[IMAGE_TAGS]、[IMAGE_OCR] 等任何内部标签，"
            "也不要复述本系统备注；直接根据图片内容自然回答用户。）"
        )

    def _with_historical_reply_image_note(self, image_input: str) -> str:
        """把历史图片输入和内部说明组合起来。"""
        return f"{image_input}\n{self._historical_reply_image_note()}"

    async def _image_input_from_platform_message_id(
        self,
        reply_msg: Message,
        reply_chat_id: str,
        reply_msg_id: str,
        *,
        allow_redownload: bool,
    ) -> Optional[str]:
        """Resolve a replied Telegram photo via ImageStore, optionally redownloading as fallback."""
        if not reply_msg.photo:
            return None
        store = self._get_image_store()
        if store is not None:
            doc = store.get_by_platform_message_id("telegram", reply_msg_id, chat_id=reply_chat_id)
            if doc:
                image_input = self._image_doc_to_image_input(doc)
                image_from_existing_store_file = bool(image_input)
                if not image_input and doc.get("image_id"):
                    image_input = await self._image_id_to_image_input(str(doc["image_id"]), reply_msg)
                if not image_input and allow_redownload:
                    image_input = await self._redownload_replied_photo_as_image_input(reply_msg)
                if image_input:
                    if image_from_existing_store_file:
                        image_input = self._with_historical_reply_image_note(image_input)
                    image_id = doc.get("image_id", "")
                    logger.info(
                        f"[telegram] reply 历史图片已通过 ImageStore 反查到图片文件"
                        + (f" image_id={image_id}" if image_id else "")
                    )
                    return image_input

        if allow_redownload:
            return await self._redownload_replied_photo_as_image_input(reply_msg)
        return None

    async def _image_id_to_image_input(self, image_id: str, reply_msg: Message) -> Optional[str]:
        """
        将历史 image_id 转成真实图片输入。

        优先从 ImageStore 取 file_path；若图片库记录缺失或本地文件丢失，
        但 Telegram reply 消息本身仍带 photo，则重新下载该 photo。
        """
        if not image_id:
            return None
        try:
            store = self._get_image_store()
            if store is not None:
                doc = store.get_by_id(image_id)
                image_input = self._image_doc_to_image_input(doc or {})
                if image_input:
                    logger.info(f"[telegram] reply 图片 image_id={image_id} 已转换为 image 模型输入")
                    return self._with_historical_reply_image_note(image_input)
        except Exception as e:
            logger.debug(f"[telegram] image_id={image_id} 转图片输入失败: {e}")

        if reply_msg.photo:
            return await self._redownload_replied_photo_as_image_input(reply_msg)
        return None

    async def _redownload_replied_photo_as_image_input(self, reply_msg: Message) -> Optional[str]:
        """重新下载 reply 引用的历史图片，并返回内部 [image: ...] 输入标记。"""
        rel_path = await self._redownload_replied_photo(reply_msg)
        if not rel_path:
            return None
        return f"[image: {rel_path}]"

    async def _redownload_replied_sticker_as_input(self, reply_msg: Message) -> Optional[str]:
        """重新下载 reply 引用的表情包，返回与新发表情包完全一致的内部标记。

        格式必须和 `_handle_sticker`（adapters/telegram/incoming.py）逐字对齐：
        emoji/贴纸包那行给模型看语义，路径那行才是 `extract_image_payloads`
        识别的载荷。两行都在时，引用表情包和直接发表情包走的是同一条 fast-image 链路。
        """
        rel_path = await self._redownload_replied_sticker(reply_msg)
        if not rel_path:
            return None
        sticker = reply_msg.sticker
        emoji = (getattr(sticker, "emoji", None) or "🎨") if sticker else "🎨"
        set_name = (getattr(sticker, "set_name", None) or "未知贴纸包") if sticker else "未知贴纸包"
        return f"[sticker: {emoji} from {set_name}]\n[sticker: {rel_path}]"

    async def _redownload_replied_sticker(self, reply_msg: Message) -> Optional[str]:
        """
        重新下载用户 reply 引用的那个表情包到本地，返回相对路径（相对 workspace_root）。

        - 复用 `_handle_sticker` 的命名规则（`sticker_<file_id>.<ext>`），同 file_id 不重复占空间。
        - 出错返回 None，由调用方退回原有的历史查找/文本兜底。
        """
        sticker = getattr(reply_msg, "sticker", None)
        if not sticker:
            return None
        try:
            chat = getattr(reply_msg, "chat", None)
            chat_type = chat.type if chat else ""
            chat_id = str((chat.id if chat else None) or self.current_chat_id or "")
            scene = infer_scene(chat_type=chat_type, chat_id=chat_id)
            target_dir = build_media_subdir(
                data_dir=self.data_dir,
                platform="telegram",
                scene=scene,
                chat_id=chat_id,
                media_type="sticker",
            )
            ext = "webm" if getattr(sticker, "is_video", False) else "webp"
            abs_file_path = os.path.join(target_dir, f"sticker_{sticker.file_id}.{ext}")
            # 已存在就直接复用，不重复下载
            if not os.path.isfile(abs_file_path):
                file = await sticker.get_file()
                await file.download_to_drive(abs_file_path)
            return os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')
        except Exception as e:
            logger.warning(f"[telegram] 重新下载引用的表情包失败: {e}")
            return None

    async def _redownload_replied_photo(self, reply_msg: Message) -> Optional[str]:
        """
        重新下载用户 reply 引用的那张历史图片到本地，返回相对路径（相对 workspace_root）。

        - 复用 `_handle_photo` 的命名规则（`photo_<file_id>.jpg`），同 file_id 不会重复占空间。
        - 出错返回 None，由调用方走更下层兜底。
        """
        if not reply_msg.photo:
            return None
        try:
            photo = reply_msg.photo[-1]
            chat = getattr(reply_msg, "chat", None)
            chat_type = chat.type if chat else ""
            chat_id = str((chat.id if chat else None) or self.current_chat_id or "")
            scene = infer_scene(chat_type=chat_type, chat_id=chat_id)
            target_dir = build_media_subdir(
                data_dir=self.data_dir,
                platform="telegram",
                scene=scene,
                chat_id=chat_id,
                media_type="image",
            )
            abs_file_path = os.path.join(target_dir, f"photo_{photo.file_id}.jpg")
            # 已存在就直接复用，不重复下载
            if not os.path.isfile(abs_file_path):
                file = await photo.get_file()
                await file.download_to_drive(abs_file_path)
            rel_file_path = os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')
            return rel_file_path
        except Exception as e:
            logger.warning(f"[telegram] 重新下载历史图片失败: {e}")
            return None
