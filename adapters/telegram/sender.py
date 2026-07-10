from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
from typing import List, Optional

from telegram.constants import ChatAction
from telegram.error import RetryAfter, TimedOut

from adapters.message_controls import parse_message_controls, strip_message_control_markers

from .constants import (
    COMPRESS_TARGET_SIZE,
    TG_DOCUMENT_MAX_SIZE,
    TG_MSG_MAX_LENGTH,
    TG_PHOTO_MAX_SIZE,
)
from .formatting import _markdown_to_html, _split_long_text
from .message import telegram_display_name

logger = logging.getLogger(__name__)


class TelegramSenderMixin:
    async def _resolve_mention_label(self, chat_id: str, user_id: str) -> str:
        cache = getattr(self, "_user_label_cache", None)
        if cache is not None:
            found, cached_label = cache.get(user_id)
            if found:
                return cached_label or f"@{user_id}"

        try:
            member = await self.application.bot.get_chat_member(
                chat_id=chat_id,
                user_id=int(user_id),
            )
            user = getattr(member, "user", None)
            label = telegram_display_name(user)
            if label and label != user_id:
                if cache is not None:
                    cache.put(user_id, label)
                return label
        except Exception as exc:
            logger.debug(
                "[telegram] resolving mention label failed chat_id=%s user_id=%s: %s",
                chat_id,
                user_id,
                exc,
            )

        if cache is not None:
            cache.put_missing(user_id)
        return f"@{user_id}"

    async def _render_message_controls(
        self,
        text: str,
        *,
        chat_id: str,
        allow_mentions: bool,
    ) -> tuple[str, str]:
        parsed = parse_message_controls(text)
        labels: dict[str, str] = {}
        if allow_mentions:
            for user_id in parsed.mention_user_ids:
                if user_id.isdigit() and user_id not in labels:
                    labels[user_id] = await self._resolve_mention_label(chat_id, user_id)

        rendered: list[str] = []
        for token in parsed.tokens:
            if token.type == "text":
                rendered.append(token.value)
            elif token.type == "mention" and allow_mentions:
                if token.value.isdigit():
                    display_label = labels[token.value]
                    if not display_label.startswith("@"):
                        display_label = f"@{display_label}"
                    label = html.escape(display_label)
                    rendered.append(f'<a href="tg://user?id={token.value}">{label}</a>')
                else:
                    logger.debug("[telegram] ignoring non-numeric mention user id=%s", token.value)
        return "".join(rendered), parsed.reply_message_id

    def _telegram_reply_kwargs(self, reply_to_message_id: str) -> dict[str, object]:
        value = str(reply_to_message_id or "").strip()
        if not value:
            return {}
        try:
            message_id = int(value)
        except (TypeError, ValueError):
            logger.debug("[telegram] ignoring non-numeric reply_to_message_id=%s", value)
            return {}
        return {
            "reply_to_message_id": message_id,
            "allow_sending_without_reply": True,
        }

    def _resolve_local_media_path(self, raw_path: str) -> Optional[str]:
        """
        解析富媒体中的本地文件路径。
        支持以下输入：
        - 绝对路径
        - workspace/xxx 相对工作区路径
        - downloads/xxx 相对下载目录路径
        - 普通相对路径（优先按 workspace 根目录解析）
        """
        path = (raw_path or "").strip().strip('"').strip("'")
        if not path:
            return None

        # 1) 绝对路径：按原样解析
        if os.path.isabs(path):
            return path if os.path.isfile(path) else None

        normalized = path.replace('\\', '/')
        candidates: List[str] = []

        # 2) workspace/ 前缀映射到真实 workspace 根目录
        if normalized.startswith("workspace/"):
            rel = normalized[len("workspace/"):]
            candidates.append(os.path.join(self.workspace_root, rel))
        elif normalized == "workspace":
            candidates.append(self.workspace_root)

        # 3) downloads/ / download/ 前缀映射到 downloads 目录
        if normalized.startswith("downloads/"):
            rel = normalized[len("downloads/"):]
            candidates.append(os.path.join(self.downloads_dir, rel))
        elif normalized.startswith("download/"):
            rel = normalized[len("download/"):]
            candidates.append(os.path.join(self.downloads_dir, rel))

        # 4) ./ 或普通相对路径：默认按 workspace 根目录解析（优先）
        if normalized.startswith("./"):
            candidates.append(os.path.join(self.workspace_root, normalized[2:]))
        candidates.append(os.path.join(self.workspace_root, path))

        # 5) 兼容 fallback：当前进程工作目录（低优先级）
        candidates.append(os.path.join(os.getcwd(), path))

        # 去重后检查
        seen = set()
        for c in candidates:
            if not c:
                continue
            norm = os.path.normpath(c)
            if norm in seen:
                continue
            seen.add(norm)
            if os.path.isfile(norm):
                return norm

        return None

    async def _send_with_retry(self, send_coro_factory, retries: int = 2, base_delay: float = 1.0):
        """
        对 Telegram 发送操作做超时重试。
        - 仅对 TimedOut / RetryAfter 重试；其他异常直接抛出。
        - 重试次数含首次尝试，默认最多 3 次（0 + 2 次重试）。
        """
        attempt = 0
        while True:
            try:
                return await send_coro_factory()
            except RetryAfter as e:
                attempt += 1
                if attempt > retries:
                    raise
                wait = getattr(e, "retry_after", base_delay * attempt)
                logger.warning(f"发送被限流，{wait}s 后重试 (attempt {attempt}/{retries})")
                await asyncio.sleep(wait)
            except TimedOut:
                attempt += 1
                if attempt > retries:
                    raise
                wait = base_delay * attempt
                logger.warning(f"发送超时，{wait}s 后重试 (attempt {attempt}/{retries})")
                await asyncio.sleep(wait)

    def _compress_image(self, file_path: str, target_size: int = COMPRESS_TARGET_SIZE) -> Optional[io.BytesIO]:
        """
        压缩图片到目标大小以内，返回 BytesIO。
        优先用 Pillow，没装就返回 None。
        """
        try:
            from PIL import Image
        except ImportError:
            logger.warning("Pillow 未安装，无法压缩图片。可以运行 pip install Pillow 来启用压缩。")
            return None

        try:
            img = Image.open(file_path)
            # 如果有 RGBA/P 模式，转为 RGB（JPEG 不支持透明）
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')

            # 先尝试缩小分辨率（Telegram 照片最大边 4096px 就够了）
            max_dimension = 4096
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            # 二分法找到合适的质量
            quality_low, quality_high = 10, 95
            best_buf = None

            while quality_low <= quality_high:
                quality_mid = (quality_low + quality_high) // 2
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=quality_mid, optimize=True)
                size = buf.tell()

                if size <= target_size:
                    best_buf = buf
                    quality_low = quality_mid + 1  # 尝试更高质量
                else:
                    quality_high = quality_mid - 1  # 需要更低质量

            if best_buf:
                best_buf.seek(0)
                logger.info(f"图片压缩成功: {file_path} -> {best_buf.getbuffer().nbytes / 1024 / 1024:.1f}MB (quality={quality_low-1})")
                return best_buf

            # 如果即使最低质量还是太大，进一步缩小分辨率
            for scale in [0.75, 0.5, 0.25]:
                new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
                resized = img.resize(new_size, Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                resized.save(buf, format='JPEG', quality=60, optimize=True)
                if buf.tell() <= target_size:
                    buf.seek(0)
                    logger.info(f"图片压缩成功(缩小): {file_path} -> {buf.getbuffer().nbytes / 1024 / 1024:.1f}MB (scale={scale})")
                    return buf

            logger.warning(f"图片压缩失败，即使最小分辨率仍然超过限制: {file_path}")
            return None

        except Exception as e:
            logger.error(f"压缩图片时出错 {file_path}: {e}")
            return None

    async def _send_photo_smart(
        self,
        chat_id: str,
        file_path: str,
        caption: str,
        reply_kwargs: dict[str, object] | None = None,
    ):
        """
        智能发送图片：
        1. 小于 10MB → 直接 send_photo
        2. 大于 10MB → 尝试压缩后 send_photo
        3. 压缩失败或仍然太大 → 降级为 send_document
        """
        file_size = os.path.getsize(file_path)
        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        reply_kwargs = reply_kwargs or {}

        # Case 1: 小图直接发
        if file_size <= TG_PHOTO_MAX_SIZE:
            with open(file_path, 'rb') as f:
                return await self.application.bot.send_photo(
                    chat_id=chat_id, photo=f, caption=caption, **reply_kwargs
                )

        # Case 2: 大图尝试压缩
        logger.info(f"[{chat_id}] 图片过大 ({file_size/1024/1024:.1f}MB)，尝试压缩: {file_path}")
        compressed = self._compress_image(file_path)
        if compressed:
            try:
                return await self.application.bot.send_photo(
                    chat_id=chat_id, photo=compressed, caption=f"{caption} (已压缩)", **reply_kwargs
                )
            except Exception as e:
                logger.warning(f"[{chat_id}] 压缩后发送图片仍失败: {e}")

        # Case 3: 降级为文档发送
        logger.info(f"[{chat_id}] 图片无法作为照片发送，降级为文档: {file_path}")
        if file_size <= TG_DOCUMENT_MAX_SIZE:
            await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            with open(file_path, 'rb') as f:
                return await self.application.bot.send_document(
                    chat_id=chat_id, document=f, caption=f"{caption} (原图)", **reply_kwargs
                )
        else:
            # 超过 50MB 文档限制，发压缩版文档
            if compressed:
                compressed.seek(0)
                await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                return await self.application.bot.send_document(
                    chat_id=chat_id, document=compressed,
                    filename=os.path.splitext(caption)[0] + '_compressed.jpg',
                    caption=f"{caption} (压缩后文档)",
                    **reply_kwargs,
                )
            raise Exception(f"文件过大 ({file_size/1024/1024:.1f}MB) 且无法压缩")

    def _classify_file(self, path: str) -> str:
        """根据扩展名判断文件类型"""
        # 移除 URL 参数以便正确判断扩展名 (如 image.jpg?v=1)
        clean_path = path.split('?')[0].split('#')[0]
        ext = os.path.splitext(clean_path)[1].lower()
        for media_type, extensions in self.MEDIA_TYPES.items():
            if ext in extensions:
                return media_type
        return 'document'  # 未知扩展名默认作为文档发送

    def _sanitize_links_if_needed(self, text: str) -> str:
        """
        处理文本中的链接以避免内容安全过滤。
        
        只对纯文本中的裸 URL 做处理（在域名的点号前加零宽空格），
        不影响 Markdown 链接 [text](url) 的解析（因为它们会被 _markdown_to_html 转为 <a> 标签）。
        """
        import re
        
        # 先保护 Markdown 链接 [text](url)，不做处理
        protected = []
        def _protect_md_link(m):
            idx = len(protected)
            protected.append(m.group(0))
            return f'\x01MDLINK{idx}\x01'
        
        text = re.sub(r'\[([^\]]+?)\]\((https?://[^\)]+?)\)', _protect_md_link, text)
        
        # 对剩余的裸 URL，在域名的点号前插入零宽空格（不破坏 URL 结构）
        url_pattern = r'(https?://[^\s<>]+)'
        
        def insert_zwsp_at_dots(match):
            url = match.group(1)
            if len(url) < 15:
                return url
            # 只在域名部分的点号前插入零宽空格，不改变 URL 本身
            # 例如: https://example.com -> https://example\u200B.com
            protocol_end = url.find('://') + 3
            domain_end = url.find('/', protocol_end)
            if domain_end == -1:
                domain_end = len(url)
            domain = url[protocol_end:domain_end]
            if '.' not in domain or len(domain) < 8:
                return url
            # 在第一个点号前加零宽空格
            dot_pos = domain.find('.')
            sanitized_domain = domain[:dot_pos] + '\u200B' + domain[dot_pos:]
            return url[:protocol_end] + sanitized_domain + url[domain_end:]
        
        text = re.sub(url_pattern, insert_zwsp_at_dots, text)
        
        # 还原 Markdown 链接
        for idx, link in enumerate(protected):
            text = text.replace(f'\x01MDLINK{idx}\x01', link)
        
        return text

    async def send_message(self, chat_id: str, text: str, **kwargs) -> list[str]:
        """
        发送消息，自动检测并发送嵌入的富媒体文件。

        返回所有已发送 Telegram 消息的 message_id 列表（可能包含多个 ID，
        例如文本 + 图片分别发送时会返回两条消息的 ID）。

        支持的嵌入语法:
          - Markdown:  ![alt](path/to/file.png)
          - HTML:      <img src="path/to/file.png">
          - 自定义标记: [image: path] / [file: path] / [audio: path] / [video: path] / [doc: path]
          - 原始路径:  /path/to/file.ext (自动识别)

        支持的媒体类型:
          - 图片: png, jpg, jpeg, webp, bmp
          - GIF动图: gif
          - 视频: mp4, mkv, avi, mov, webm
          - 音频: mp3, ogg, wav, flac, m4a, opus
          - 文档/代码: pdf, py, js, json, zip, 等等
        """
        parse_media = bool(kwargs.pop("parse_media", True))
        chat_type = str(kwargs.pop("chat_type", "") or "").strip().lower()
        allow_mentions = chat_type in {"group", "supergroup"}
        reply_to_message_id = str(kwargs.pop("reply_to_message_id", "") or "").strip()
        rendered_text, explicit_reply_id = await self._render_message_controls(
            str(text or ""),
            chat_id=str(chat_id),
            allow_mentions=allow_mentions,
        )
        text = rendered_text
        reply_kwargs = self._telegram_reply_kwargs(explicit_reply_id or reply_to_message_id)

        # 1. 提取所有文件路径（可通过 parse_media=False 禁用）
        file_entries = []  # [(path, media_type, is_url)]
        missing_files = []
        if parse_media:
            for match in self.FILE_PATTERN.finditer(text):
                # FILE_PATTERN 有5个捕获组: (1)Markdown, (2)HTML img, (3)自定义标记, (4)URL, (5)本地路径
                path = next((g for g in match.groups() if g), None)
                if path:
                    path = path.strip()
                    is_url = path.startswith(('http://', 'https://'))
                    if is_url:
                        media_type = self._classify_file(path)
                        file_entries.append((path, media_type, True))
                        logger.info(f"[{chat_id}] 检测到媒体URL: {path} ({media_type})")
                    else:
                        resolved_path = self._resolve_local_media_path(path)
                        if resolved_path:
                            media_type = self._classify_file(resolved_path)
                            file_entries.append((resolved_path, media_type, False))
                            logger.info(f"[{chat_id}] 检测到媒体文件: {path} -> {resolved_path} ({media_type})")
                        else:
                            missing_files.append(path)
                            logger.warning(f"[{chat_id}] 文件路径不存在，跳过: {path}")

            # 2. 清理文本中的文件引用（无论文件是否存在都清理，不要把 [image:...] 原样显示）
            clean_text = self.FILE_PATTERN.sub('', text)
            # 清理媒体标签移除后产生的多余空行和空白行
            clean_text = re.sub(r'[ \t]*\n', '\n', clean_text)  # 去除行尾空白
            clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)  # 最多保留一个空行
            clean_text = clean_text.strip()

            # 如果有文件不存在，在文末追加提示
            if missing_files:
                not_found_hint = "\n\n⚠️ 以下文件未找到:\n" + "\n".join(f"  • {p}" for p in missing_files)
                clean_text += not_found_hint
        else:
            clean_text = (text or "").strip()
        
        # 2.5. 处理链接以避免 Gemini 安全过滤问题
        # 如果文本中包含链接，在链接中插入零宽空格以绕过过滤
        clean_text = self._sanitize_links_if_needed(clean_text)

        # 3. 发送文本部分
        all_message_ids: list[str] = []
        pending_reply_kwargs = dict(reply_kwargs)
        if clean_text:
            # Markdown → HTML 转换（LLM 有时会输出 Markdown 格式）
            html_text = _markdown_to_html(clean_text, preserve_safe_html=True)
            
            # 分割超长消息（Telegram 限制 4096 字符）
            html_chunks = _split_long_text(html_text, TG_MSG_MAX_LENGTH)
            
            for chunk_html in html_chunks:
                reply_kwargs = pending_reply_kwargs
                try:
                    message = await self._send_with_retry(
                        lambda: self.application.bot.send_message(
                            chat_id=chat_id,
                            text=chunk_html,
                            parse_mode="HTML",
                            **reply_kwargs,
                        )
                    )
                except Exception as html_err:
                    logger.warning(f"[{chat_id}] HTML 发送失败 ({html_err})，降级为纯文本。")
                    plain_chunk = re.sub(r'<[^>]+>', '', chunk_html)
                    plain_parts = _split_long_text(plain_chunk or chunk_html, TG_MSG_MAX_LENGTH)
                    for part in plain_parts:
                        message = await self._send_with_retry(
                            lambda: self.application.bot.send_message(
                                chat_id=chat_id,
                                text=part,
                                **reply_kwargs,
                            )
                        )
                        reply_kwargs = {}
                        pending_reply_kwargs = {}
                        all_message_ids.append(str(message.message_id))
                    continue
                pending_reply_kwargs = {}
                all_message_ids.append(str(message.message_id))

        # 4. 逐个发送媒体文件
        for file_path, media_type, is_url in file_entries:
            reply_kwargs = pending_reply_kwargs
            caption = os.path.basename(file_path)
            try:
                if is_url:
                    # URL 直接通过 Telegram 发送，不走本地文件读取
                    if media_type == 'photo':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                        message = await self._send_with_retry(lambda: self.application.bot.send_photo(chat_id=chat_id, photo=file_path, caption=caption, **reply_kwargs))
                    elif media_type == 'gif':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                        message = await self._send_with_retry(lambda: self.application.bot.send_animation(chat_id=chat_id, animation=file_path, caption=caption, **reply_kwargs))
                    elif media_type == 'video':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
                        message = await self._send_with_retry(lambda: self.application.bot.send_video(chat_id=chat_id, video=file_path, caption=caption, **reply_kwargs))
                    elif media_type in ('audio', 'voice'):
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                        message = await self._send_with_retry(lambda: self.application.bot.send_audio(chat_id=chat_id, audio=file_path, caption=caption, **reply_kwargs))
                    else:
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                        message = await self._send_with_retry(lambda: self.application.bot.send_document(chat_id=chat_id, document=file_path, caption=caption, **reply_kwargs))
                else:
                    if media_type == 'photo':
                        message = await self._send_photo_smart(chat_id, file_path, caption, reply_kwargs=reply_kwargs)
                    elif media_type == 'gif':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_animation(
                                chat_id=chat_id, animation=f, caption=caption, **reply_kwargs
                            ))
                    elif media_type == 'video':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_video(
                                chat_id=chat_id, video=f, caption=caption, **reply_kwargs
                            ))
                    elif media_type == 'audio':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_audio(
                                chat_id=chat_id, audio=f, caption=caption, **reply_kwargs
                            ))
                    elif media_type == 'voice':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_voice(
                                chat_id=chat_id, voice=f, caption=caption, **reply_kwargs
                            ))
                    else:  # document (including code files)
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_document(
                                chat_id=chat_id, document=f, caption=caption, **reply_kwargs
                            ))
                all_message_ids.append(str(message.message_id))
                pending_reply_kwargs = {}
                logger.info(f"[{chat_id}] 已发送{media_type}: {file_path}")
            except Exception as e:
                logger.error(f"[{chat_id}] 发送{media_type}失败 {file_path}: {e}")
                message = await self._send_with_retry(
                    lambda: self.application.bot.send_message(
                        chat_id=chat_id,
                        text=("⚠️ 无法发送文件: {file_path}\n类型: {media_type}\n错误: {err}\n"
                             "已达到重试上限，建议检查网络或稍后再试。"
                        ).format(file_path=file_path, media_type=media_type, err=e),
                        **reply_kwargs,
                    )
                )
                all_message_ids.append(str(message.message_id))
                pending_reply_kwargs = {}

        # 5. 兜底：如果什么都没发出去，发已清理的正文
        if not all_message_ids:
            fallback_text = strip_message_control_markers(text).strip() or "(empty message)"
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=fallback_text,
                **pending_reply_kwargs,
            )
            all_message_ids.append(str(message.message_id))

        return all_message_ids

    async def start_typing(self, chat_id: str):
        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    async def stop_typing(self, chat_id: str):
        # Telegram doesn't have a "stop typing" action, it's implicit.
        pass

    # ------------------------------------------------------------------
    # 消息编辑 / 删除（覆盖基类默认实现）
    # ------------------------------------------------------------------

    async def edit_message(self, chat_id: str, message_id: str, new_text: str, **kwargs) -> bool:
        """编辑一条已发送的 Telegram 消息。"""
        try:
            html_text = _markdown_to_html(strip_message_control_markers(new_text))
            await self._send_with_retry(
                lambda: self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=html_text,
                    parse_mode="HTML",
                )
            )
            return True
        except Exception as e:
            logger.warning(f"[{chat_id}] 编辑消息 {message_id} 失败: {e}")
            return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """删除一条 Telegram 消息。"""
        try:
            await self._send_with_retry(
                lambda: self.application.bot.delete_message(
                    chat_id=chat_id,
                    message_id=int(message_id),
                )
            )
            return True
        except Exception as e:
            logger.warning(f"[{chat_id}] 删除消息 {message_id} 失败: {e}")
            return False
