from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from telegram.constants import ChatAction
from typing import Callable, Any
import os
import re
import logging
import io

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

    def run(self, message_handler: Callable[[str, str], Any]):
        self._message_handler = message_handler
        self._aggregator = MessageAggregator(
            timeout=config.get_config().get("interaction", {}).get("buffer_timeout", 3.0),
            on_complete=self._message_handler
        )

        start_handler = CommandHandler('start', self._start_command)
        msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), self._handle_incoming_message)
        
        self.application.add_handler(start_handler)
        self.application.add_handler(msg_handler)
        
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
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)

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
        for match in self.FILE_PATTERN.finditer(text):
            path = match.group(1) or match.group(2) or match.group(3) or match.group(4)
            if path:
                path = path.strip()
                if os.path.isfile(path):
                    media_type = self._classify_file(path)
                    file_entries.append((path, media_type))

        # 2. 清理文本中的文件引用
        clean_text = text
        if file_entries:
            clean_text = self.FILE_PATTERN.sub('', text).strip()
            clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

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
