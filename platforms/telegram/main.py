from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.constants import ChatAction, ParseMode
from typing import Callable, Any, Optional, List, Dict
import os
import re
import logging
import io
import asyncio

from platforms.base import BaseAdapter
from platforms.aggregator import MessageAggregator
import config

logger = logging.getLogger(__name__)

# Telegram limits
TG_PHOTO_MAX_SIZE = 10 * 1024 * 1024       # 10MB for photos
TG_DOCUMENT_MAX_SIZE = 50 * 1024 * 1024     # 50MB for documents
COMPRESS_TARGET_SIZE = 9 * 1024 * 1024       # Compress target: 9MB (safety margin)

class TelegramAdapter(BaseAdapter):
    """Telegram 平台适配器。"""

    def __init__(self):
        super().__init__() # Call parent __init__
        token = config.get_telegram_token()
        if not token:
            raise ValueError("Telegram Bot Token not found in config.")
        
        self.application = ApplicationBuilder().token(token).build()
        self._message_handler: Callable[[str, str], Any] | None = None
        self._aggregator: MessageAggregator | None = None
        self._reply_contexts: Dict[str, Dict] = {}  # 存储回复上下文 {chat_id: {msg_id: context}}

    def run(self, message_handler: Callable[[str, str], Any]):
        self._message_handler = message_handler
        self._aggregator = MessageAggregator(
            timeout=config.get_config().get("interaction", {}).get("buffer_timeout", 3.0),
            on_complete=self._message_handler
        )

        start_handler = CommandHandler('start', self._start_command)
        msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), self._handle_incoming_message)
        photo_handler = MessageHandler(filters.PHOTO, self._handle_photo)
        document_handler = MessageHandler(filters.Document.ALL, self._handle_document)
        sticker_handler = MessageHandler(filters.Sticker.ALL, self._handle_sticker)
        callback_handler = CallbackQueryHandler(self._handle_callback)
        
        self.application.add_handler(start_handler)
        self.application.add_handler(msg_handler)
        self.application.add_handler(photo_handler)
        self.application.add_handler(document_handler)
        self.application.add_handler(sticker_handler)
        self.application.add_handler(callback_handler)
        
        print("Telegram Adapter is running...")
        self.application.run_polling(stop_signals=None)

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if self._message_handler:
            # Propagate start command as a special message
            await self._message_handler(chat_id, "/start")

    async def _handle_incoming_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id # Set current chat_id
        text = update.message.text
        
        # 处理回复消息
        reply_info = await self._extract_reply_info(update.message)
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理图片消息"""
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        photo = update.message.photo[-1]  # 获取最高质量版本
        caption = update.message.caption or ""
        
        # 下载图片
        file = await photo.get_file()
        file_path = f"downloads/telegram/photo_{photo.file_id}.jpg"
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        await file.download_to_drive(file_path)
        
        # 构造消息
        reply_info = await self._extract_reply_info(update.message)
        text = f"[图片: {file_path}]"
        if caption:
            text += f"\n{caption}"
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)
        
        logger.info(f"[{chat_id}] 收到图片: {file_path}")

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理文档消息"""
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        document = update.message.document
        caption = update.message.caption or ""
        
        # 下载文档
        file = await document.get_file()
        file_name = document.file_name or f"document_{document.file_id}"
        file_path = f"downloads/telegram/{file_name}"
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        await file.download_to_drive(file_path)
        
        # 构造消息
        reply_info = await self._extract_reply_info(update.message)
        text = f"[文件: {file_path}]"
        if caption:
            text += f"\n{caption}"
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)
        
        logger.info(f"[{chat_id}] 收到文档: {file_path}")

    async def _handle_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理贴纸消息"""
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        sticker = update.message.sticker
        emoji = sticker.emoji or "🎨"
        set_name = sticker.set_name or "未知贴纸包"
        
        # 下载贴纸
        file = await sticker.get_file()
        ext = "webm" if sticker.is_video else "webp"
        file_path = f"downloads/telegram/sticker_{sticker.file_id}.{ext}"
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        await file.download_to_drive(file_path)
        
        # 构造消息
        reply_info = await self._extract_reply_info(update.message)
        text = f"[贴纸: {emoji} 来自 {set_name}]\n[文件: {file_path}]"
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)
        
        logger.info(f"[{chat_id}] 收到贴纸: {emoji} from {set_name}")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 inline keyboard 回调"""
        query = update.callback_query
        chat_id = str(query.message.chat_id)
        callback_data = query.data
        
        # 确认回调
        await query.answer()
        
        # 构造消息
        text = f"[按钮点击: {callback_data}]"
        
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)
        
        logger.info(f"[{chat_id}] 收到回调: {callback_data}")

    async def _extract_reply_info(self, message: Message) -> Optional[str]:
        """提取回复消息的信息"""
        if not message.reply_to_message:
            return None
        
        replied = message.reply_to_message
        sender = replied.from_user.first_name if replied.from_user else "未知"
        
        # 提取原消息内容摘要
        if replied.text:
            content = replied.text[:100] + "..." if len(replied.text) > 100 else replied.text
        elif replied.caption:
            content = replied.caption[:100] + "..." if len(replied.caption) > 100 else replied.caption
        elif replied.photo:
            content = "[图片]"
        elif replied.document:
            content = f"[文档: {replied.document.file_name}]"
        elif replied.sticker:
            content = f"[贴纸: {replied.sticker.emoji}]"
        elif replied.voice:
            content = "[语音]"
        elif replied.video:
            content = "[视频]"
        elif replied.audio:
            content = "[音频]"
        else:
            content = "[消息]"
        
        return f"{sender}: {content}"

    # --- 富媒体自动检测 ---
    # 文件扩展名 → 媒体类型映射
    MEDIA_TYPES = {
        'photo':    {'.png', '.jpg', '.jpeg', '.webp', '.bmp'},
        'gif':      {'.gif'},
        'video':    {'.mp4', '.mkv', '.avi', '.mov', '.webm'},
        'audio':    {'.mp3', '.ogg', '.wav', '.flac', '.m4a', '.opus'},
        'voice':    {'.oga'},  # Telegram voice message format
        'document': {
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.zip', '.rar', '.7z', '.tar', '.gz',
            '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.cs',
            '.go', '.rs', '.rb', '.php', '.sh', '.bat', '.ps1',
            '.json', '.yml', '.yaml', '.toml', '.xml', '.csv',
            '.md', '.txt', '.log', '.ini', '.cfg', '.conf',
            '.html', '.css', '.sql', '.r', '.kt', '.swift',
        },
    }

    # 统一匹配：支持多种嵌入语法 + 原始文件路径
    FILE_PATTERN = re.compile(
        r'(?:!\[.*?\]\((.*?)\))|'                  # Markdown: ![alt](path)
        r'(?:<img\s+src="(.*?)"[^>]*>)|'            # HTML: <img src="path">
        r'(?:\[(?:image|file|audio|video|doc):\s*(.*?)\])|'  # 自定义: [image: path] / [file: path] / [audio: path] 等
        r'(?:^|\s)((?:/|\.{0,2}/|[a-zA-Z]:\\)[^\s<>"\']+\.(?:' +
        '|'.join(
            ext.lstrip('.') for exts in MEDIA_TYPES.values() for ext in exts
        ) + r'))\b',                                 # 原始文件路径
        re.IGNORECASE | re.MULTILINE
    )

    def _classify_file(self, path: str) -> str:
        """根据扩展名判断文件类型"""
        ext = os.path.splitext(path)[1].lower()
        for media_type, extensions in self.MEDIA_TYPES.items():
            if ext in extensions:
                return media_type
        return 'document'  # 未知扩展名默认作为文档发送

    def _compress_image(self, file_path: str, target_size: int = COMPRESS_TARGET_SIZE) -> io.BytesIO:
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
                img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

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
                resized = img.resize(new_size, Image.LANCZOS)
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

    async def _send_photo_smart(self, chat_id: str, file_path: str, caption: str):
        """
        智能发送图片：
        1. 小于 10MB → 直接 send_photo
        2. 大于 10MB → 尝试压缩后 send_photo
        3. 压缩失败或仍然太大 → 降级为 send_document
        """
        file_size = os.path.getsize(file_path)
        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

        # Case 1: 小图直接发
        if file_size <= TG_PHOTO_MAX_SIZE:
            with open(file_path, 'rb') as f:
                return await self.application.bot.send_photo(
                    chat_id=chat_id, photo=f, caption=caption
                )

        # Case 2: 大图尝试压缩
        logger.info(f"[{chat_id}] 图片过大 ({file_size/1024/1024:.1f}MB)，尝试压缩: {file_path}")
        compressed = self._compress_image(file_path)
        if compressed:
            try:
                return await self.application.bot.send_photo(
                    chat_id=chat_id, photo=compressed, caption=f"{caption} (已压缩)"
                )
            except Exception as e:
                logger.warning(f"[{chat_id}] 压缩后发送图片仍失败: {e}")

        # Case 3: 降级为文档发送
        logger.info(f"[{chat_id}] 图片无法作为照片发送，降级为文档: {file_path}")
        if file_size <= TG_DOCUMENT_MAX_SIZE:
            await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            with open(file_path, 'rb') as f:
                return await self.application.bot.send_document(
                    chat_id=chat_id, document=f, caption=f"{caption} (原图)"
                )
        else:
            # 超过 50MB 文档限制，发压缩版文档
            if compressed:
                compressed.seek(0)
                await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                return await self.application.bot.send_document(
                    chat_id=chat_id, document=compressed,
                    filename=os.path.splitext(caption)[0] + '_compressed.jpg',
                    caption=f"{caption} (压缩后文档)"
                )
            raise Exception(f"文件过大 ({file_size/1024/1024:.1f}MB) 且无法压缩")

    async def send_message(self, chat_id: str, text: str) -> str:
        """
        发送消息，自动检测并发送嵌入的富媒体文件。
        
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
        # 1. 提取所有文件路径
        file_entries = []  # [(path, media_type)]
        missing_files = []
        for match in self.FILE_PATTERN.finditer(text):
            path = match.group(1) or match.group(2) or match.group(3) or match.group(4)
            if path:
                path = path.strip()
                if os.path.isfile(path):
                    media_type = self._classify_file(path)
                    file_entries.append((path, media_type))
                    logger.info(f"[{chat_id}] 检测到媒体文件: {path} ({media_type})")
                else:
                    missing_files.append(path)
                    logger.warning(f"[{chat_id}] 文件路径不存在，跳过: {path}")

        # 2. 清理文本中的文件引用（无论文件是否存在都清理，不要把 [image:...] 原样显示）
        clean_text = self.FILE_PATTERN.sub('', text).strip()
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
        
        # 如果有文件不存在，在文末追加提示
        if missing_files:
            not_found_hint = "\n\n⚠️ 以下文件未找到:\n" + "\n".join(f"  • {p}" for p in missing_files)
            clean_text += not_found_hint

        # 3. 发送文本部分
        last_message_id = None
        if clean_text:
            try:
                message = await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=clean_text,
                    parse_mode="HTML"
                )
            except Exception:
                message = await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=clean_text
                )
            last_message_id = str(message.message_id)

        # 4. 逐个发送媒体文件
        for file_path, media_type in file_entries:
            caption = os.path.basename(file_path)
            try:
                if media_type == 'photo':
                    message = await self._send_photo_smart(chat_id, file_path, caption)
                elif media_type == 'gif':
                    await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                    with open(file_path, 'rb') as f:
                        message = await self.application.bot.send_animation(
                            chat_id=chat_id, animation=f, caption=caption
                        )
                elif media_type == 'video':
                    await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
                    with open(file_path, 'rb') as f:
                        message = await self.application.bot.send_video(
                            chat_id=chat_id, video=f, caption=caption
                        )
                elif media_type == 'audio':
                    await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                    with open(file_path, 'rb') as f:
                        message = await self.application.bot.send_audio(
                            chat_id=chat_id, audio=f, caption=caption
                        )
                elif media_type == 'voice':
                    await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                    with open(file_path, 'rb') as f:
                        message = await self.application.bot.send_voice(
                            chat_id=chat_id, voice=f, caption=caption
                        )
                else:  # document (including code files)
                    await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                    with open(file_path, 'rb') as f:
                        message = await self.application.bot.send_document(
                            chat_id=chat_id, document=f, caption=caption
                        )
                last_message_id = str(message.message_id)
                logger.info(f"[{chat_id}] 已发送{media_type}: {file_path}")
            except Exception as e:
                logger.error(f"[{chat_id}] 发送{media_type}失败 {file_path}: {e}")
                message = await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ 无法发送文件: {file_path}\n类型: {media_type}\n错误: {e}"
                )
                last_message_id = str(message.message_id)

        # 5. 兜底：如果什么都没发出去，发原始文本
        if last_message_id is None:
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=text or "(empty message)"
            )
            last_message_id = str(message.message_id)

        return last_message_id

    async def start_typing(self, chat_id: str):
        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    async def stop_typing(self, chat_id: str):
        # Telegram doesn't have a "stop typing" action, it's implicit.
        pass

    async def send_sticker(self, chat_id: str, sticker: str) -> str:
        """
        发送贴纸
        :param sticker: 贴纸文件ID 或 本地文件路径
        """
        try:
            if os.path.isfile(sticker):
                with open(sticker, 'rb') as f:
                    message = await self.application.bot.send_sticker(chat_id=chat_id, sticker=f)
            else:
                message = await self.application.bot.send_sticker(chat_id=chat_id, sticker=sticker)
            
            logger.info(f"[{chat_id}] 已发送贴纸")
            return str(message.message_id)
        except Exception as e:
            logger.error(f"[{chat_id}] 发送贴纸失败: {e}")
            raise

    async def send_with_buttons(self, chat_id: str, text: str, buttons: List[List[Dict[str, str]]]) -> str:
        """
        发送带按钮的消息
        :param buttons: 按钮布局 [[{"text": "按钮1", "callback_data": "btn1"}]]
        """
        try:
            keyboard = []
            for row in buttons:
                keyboard_row = []
                for btn in row:
                    keyboard_row.append(
                        InlineKeyboardButton(text=btn.get("text", ""), callback_data=btn.get("callback_data", ""))
                    )
                keyboard.append(keyboard_row)
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup
            )
            
            logger.info(f"[{chat_id}] 已发送带按钮的消息")
            return str(message.message_id)
        except Exception as e:
            logger.error(f"[{chat_id}] 发送按钮消息失败: {e}")
            raise

    async def edit_message(self, chat_id: str, message_id: str, new_text: str) -> bool:
        """编辑已发送的消息"""
        try:
            await self.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=new_text
            )
            logger.info(f"[{chat_id}] 已编辑消息 {message_id}")
            return True
        except Exception as e:
            logger.error(f"[{chat_id}] 编辑消息失败: {e}")
            return False

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """删除消息"""
        try:
            await self.application.bot.delete_message(
                chat_id=chat_id,
                message_id=int(message_id)
            )
            logger.info(f"[{chat_id}] 已删除消息 {message_id}")
            return True
        except Exception as e:
            logger.error(f"[{chat_id}] 删除消息失败: {e}")
            return False

    async def get_chat_info(self, chat_id: str) -> Optional[Dict]:
        """获取聊天信息"""
        try:
            chat = await self.application.bot.get_chat(chat_id=chat_id)
            info = {
                "id": chat.id,
                "type": chat.type,
                "title": chat.title,
                "username": chat.username,
                "first_name": chat.first_name,
                "last_name": chat.last_name,
                "description": chat.description,
                "member_count": None
            }
            
            # 尝试获取成员数（仅群组）
            if chat.type in ["group", "supergroup"]:
                try:
                    count = await self.application.bot.get_chat_member_count(chat_id=chat_id)
                    info["member_count"] = count
                except:
                    pass
            
            logger.info(f"[{chat_id}] 获取聊天信息成功")
            return info
        except Exception as e:
            logger.error(f"[{chat_id}] 获取聊天信息失败: {e}")
            return None

    async def get_user_profile_photos(self, user_id: str, limit: int = 1) -> List[str]:
        """获取用户头像照片"""
        try:
            photos = await self.application.bot.get_user_profile_photos(user_id=int(user_id), limit=limit)
            photo_paths = []
            
            for i, photo_list in enumerate(photos.photos):
                if photo_list:
                    photo = photo_list[-1]  # 最高质量
                    file = await photo.get_file()
                    file_path = f"downloads/telegram/profile_{user_id}_{i}.jpg"
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    await file.download_to_drive(file_path)
                    photo_paths.append(file_path)
            
            logger.info(f"获取用户 {user_id} 的头像: {len(photo_paths)} 张")
            return photo_paths
        except Exception as e:
            logger.error(f"获取用户头像失败: {e}")
            return []

    async def pin_message(self, chat_id: str, message_id: str, disable_notification: bool = False) -> bool:
        """置顶消息"""
        try:
            await self.application.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=int(message_id),
                disable_notification=disable_notification
            )
            logger.info(f"[{chat_id}] 已置顶消息 {message_id}")
            return True
        except Exception as e:
            logger.error(f"[{chat_id}] 置顶消息失败: {e}")
            return False

    async def unpin_message(self, chat_id: str, message_id: Optional[str] = None) -> bool:
        """取消置顶消息"""
        try:
            if message_id:
                await self.application.bot.unpin_chat_message(
                    chat_id=chat_id,
                    message_id=int(message_id)
                )
            else:
                await self.application.bot.unpin_all_chat_messages(chat_id=chat_id)
            logger.info(f"[{chat_id}] 已取消置顶")
            return True
        except Exception as e:
            logger.error(f"[{chat_id}] 取消置顶失败: {e}")
            return False

    async def send_poll(self, chat_id: str, question: str, options: List[str], 
                       is_anonymous: bool = True, allows_multiple_answers: bool = False) -> str:
        """发送投票"""
        try:
            message = await self.application.bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=options,
                is_anonymous=is_anonymous,
                allows_multiple_answers=allows_multiple_answers
            )
            logger.info(f"[{chat_id}] 已发送投票: {question}")
            return str(message.message_id)
        except Exception as e:
            logger.error(f"[{chat_id}] 发送投票失败: {e}")
            raise

    async def send_contact(self, chat_id: str, phone_number: str, first_name: str, 
                          last_name: Optional[str] = None) -> str:
        """发送联系人"""
        try:
            message = await self.application.bot.send_contact(
                chat_id=chat_id,
                phone_number=phone_number,
                first_name=first_name,
                last_name=last_name
            )
            logger.info(f"[{chat_id}] 已发送联系人: {first_name}")
            return str(message.message_id)
        except Exception as e:
            logger.error(f"[{chat_id}] 发送联系人失败: {e}")
            raise

    async def send_location(self, chat_id: str, latitude: float, longitude: float) -> str:
        """发送位置"""
        try:
            message = await self.application.bot.send_location(
                chat_id=chat_id,
                latitude=latitude,
                longitude=longitude
            )
            logger.info(f"[{chat_id}] 已发送位置: ({latitude}, {longitude})")
            return str(message.message_id)
        except Exception as e:
            logger.error(f"[{chat_id}] 发送位置失败: {e}")
            raise

    async def forward_message(self, from_chat_id: str, to_chat_id: str, message_id: str) -> str:
        """转发消息"""
        try:
            message = await self.application.bot.forward_message(
                chat_id=to_chat_id,
                from_chat_id=from_chat_id,
                message_id=int(message_id)
            )
            logger.info(f"转发消息 {message_id} 从 {from_chat_id} 到 {to_chat_id}")
            return str(message.message_id)
        except Exception as e:
            logger.error(f"转发消息失败: {e}")
            raise

    async def get_sticker_set(self, name: str) -> Optional[Dict]:
        """获取贴纸包信息"""
        try:
            sticker_set = await self.application.bot.get_sticker_set(name=name)
            info = {
                "name": sticker_set.name,
                "title": sticker_set.title,
                "sticker_type": sticker_set.sticker_type,
                "sticker_count": len(sticker_set.stickers),
                "stickers": [
                    {
                        "file_id": s.file_id,
                        "emoji": s.emoji,
                        "is_animated": s.is_animated,
                        "is_video": s.is_video
                    }
                    for s in sticker_set.stickers
                ]
            }
            logger.info(f"获取贴纸包信息: {name} ({len(sticker_set.stickers)} 个贴纸)")
            return info
        except Exception as e:
            logger.error(f"获取贴纸包失败: {e}")
            return None
