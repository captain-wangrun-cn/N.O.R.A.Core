from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.constants import ChatAction, ParseMode
from telegram.error import TimedOut, RetryAfter
from typing import Callable, Any, Optional, List, Dict
import os
import re
import logging
import io
import asyncio

from adapters.base import BaseAdapter
from adapters.aggregator import MessageAggregator
import config
from memory.message_history import MessageHistory
import sys
import platform
from brain.llm import get_llm_client

logger = logging.getLogger(__name__)

# Telegram limits
TG_PHOTO_MAX_SIZE = 10 * 1024 * 1024       # 10MB for photos
TG_DOCUMENT_MAX_SIZE = 50 * 1024 * 1024     # 50MB for documents
COMPRESS_TARGET_SIZE = 9 * 1024 * 1024       # Compress target: 9MB (safety margin)
TG_MSG_MAX_LENGTH = 4096                     # Telegram message text limit


def _markdown_to_html(text: str) -> str:
    """
    将 Markdown 格式转为 Telegram 兼容的 HTML。
    
    处理策略：先保护代码块和行内代码，转义特殊字符，再做格式转换，最后还原代码。
    """
    # === Phase 0: 提取并保护代码块和行内代码（避免内部内容被错误转换） ===
    code_blocks = []
    inline_codes = []
    
    def _save_code_block(m):
        idx = len(code_blocks)
        lang = m.group(1) or ""
        code = m.group(2)
        # 代码块内部需要转义 HTML 特殊字符
        escaped_code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        if lang:
            code_blocks.append(f'<pre><code class="language-{lang}">{escaped_code}</code></pre>')
        else:
            code_blocks.append(f'<pre><code>{escaped_code}</code></pre>')
        return f'\x00CODEBLOCK{idx}\x00'
    
    def _save_inline_code(m):
        idx = len(inline_codes)
        code = m.group(1)
        escaped_code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        inline_codes.append(f'<code>{escaped_code}</code>')
        return f'\x00INLINECODE{idx}\x00'
    
    # 先保护代码块 (```...```)
    text = re.sub(r'```(\w*)\n(.*?)```', _save_code_block, text, flags=re.DOTALL)
    # 再保护行内代码 (`...`)
    text = re.sub(r'`([^`\n]+?)`', _save_inline_code, text)
    
    # === Phase 1: 转义 HTML 特殊字符（在非代码区域） ===
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    
    # === Phase 2: Markdown → HTML 格式转换 ===
    # 粗体+斜体: ***text***
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    # 粗体: **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # 粗体: __text__
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    # 斜体: *text* (不匹配乘号或路径中的星号)
    text = re.sub(r'(?<!\w)\*([^*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    # 删除线: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    # 链接: [text](url)
    text = re.sub(r'\[([^\]]+?)\]\((https?://[^\)]+?)\)', r'<a href="\2">\1</a>', text)
    # 标题: # Title → 加粗（Telegram 不支持 h1-h6）
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # 引用: > text → blockquote
    # 匹配连续的 > 行，合并为一个 blockquote
    text = re.sub(
        r'(^&gt;\s?.+(?:\n^&gt;\s?.+)*)',
        lambda m: '<blockquote>' + re.sub(r'^&gt;\s?', '', m.group(0), flags=re.MULTILINE) + '</blockquote>',
        text, flags=re.MULTILINE
    )
    
    # === Phase 3: 还原代码块和行内代码 ===
    for idx, block in enumerate(code_blocks):
        text = text.replace(f'\x00CODEBLOCK{idx}\x00', block)
    for idx, code in enumerate(inline_codes):
        text = text.replace(f'\x00INLINECODE{idx}\x00', code)
    
    return text


def _split_long_text(text: str, max_length: int = TG_MSG_MAX_LENGTH) -> List[str]:
    """
    将超长文本按 Telegram 限制分割成多条消息。
    优先在段落边界分割，其次在行边界，最后在字符边界。
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    remaining = text
    
    while len(remaining) > max_length:
        # 在 max_length 范围内找最佳分割点
        split_at = max_length
        
        # 优先：在段落边界 (\n\n) 分割
        para_break = remaining.rfind('\n\n', 0, max_length)
        if para_break > max_length // 4:  # 至少用到 1/4 的空间
            split_at = para_break + 2  # 包含 \n\n
        else:
            # 其次：在行边界 (\n) 分割
            line_break = remaining.rfind('\n', 0, max_length)
            if line_break > max_length // 4:
                split_at = line_break + 1
            # 最后：硬切
        
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip('\n')
    
    if remaining.strip():
        chunks.append(remaining.strip())
    
    return chunks if chunks else [text]


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
        self.message_history = MessageHistory()
        self._reply_contexts: Dict[str, Dict] = {}  # 存储回复上下文 {chat_id: {msg_id: context}}

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

    def run(self, message_handler: Callable[[str, str], Any]):
        self._message_handler = message_handler
        app_config = config.get_config()
        interaction_config = app_config.get("interaction", {}) if app_config else {}
        buffer_timeout = interaction_config.get("buffer_timeout", 3.0)

        self._aggregator = MessageAggregator(
            timeout=buffer_timeout,
            on_complete=self._message_handler
        )

        start_handler = CommandHandler('start', self._start_command)
        clear_handler = CommandHandler('clear', self._clear_command)
        status_handler = CommandHandler('status', self._status_command)
        msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), self._handle_incoming_message)
        photo_handler = MessageHandler(filters.PHOTO, self._handle_photo)
        document_handler = MessageHandler(filters.Document.ALL, self._handle_document)
        sticker_handler = MessageHandler(filters.Sticker.ALL, self._handle_sticker)
        callback_handler = CallbackQueryHandler(self._handle_callback)
        
        self.application.add_handler(start_handler)
        self.application.add_handler(clear_handler)
        self.application.add_handler(status_handler)
        self.application.add_handler(msg_handler)
        self.application.add_handler(photo_handler)
        self.application.add_handler(document_handler)
        self.application.add_handler(sticker_handler)
        self.application.add_handler(callback_handler)
        
        print("Telegram Adapter is running...")
        self.application.run_polling(stop_signals=None)

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if self._message_handler:
            # Propagate start command as a special message
            await self._message_handler(chat_id, "/start")

    async def _clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        try:
            self.message_history.clear_chat_history("telegram", chat_id)
            if update.message:
                await update.message.reply_text("✅ 当前聊天的历史记录已清空。")
            logger.info(f"[{chat_id}] 聊天历史已通过 /clear 命令清空。")
        except Exception as e:
            logger.error(f"[{chat_id}] 清空历史记录时出错: {e}")
            if update.message:
                await update.message.reply_text("❌ 清空历史记录时发生错误。")

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        
        try:
            # N.O.R.A. Core and System Info
            app_config = config.get_config()
            core_version = app_config.get("version", "N/A") if app_config else "N/A"
            python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            os_info = f"{platform.system()} {platform.release()}"
            
            # LLM Info
            llm_client = get_llm_client()
            llm_provider = llm_client.__class__.__name__.replace("LLM", "")
            llm_model = getattr(llm_client, 'model', 'N/A')

            # Memory/DB Info
            db_path = self.message_history.db_path
            db_exists = "✅ Connected" if db_path.exists() else "❌ Not Found"
            
            # RAG/VectorDB Info
            from memory.rag import RAGEngine
            rag_engine = RAGEngine()
            rag_status = "✅ Online" if rag_engine.enabled else "❌ Offline"

            status_text = (
                f"<b>N.O.R.A. Core Status</b>\n\n"
                f"<b><u>System</u></b>\n"
                f"  <b>Version:</b> {core_version}\n"
                f"  <b>Python:</b> {python_version}\n"
                f"  <b>OS:</b> {os_info}\n\n"
                f"<b><u>Language Model</u></b>\n"
                f"  <b>Provider:</b> {llm_provider}\n"
                f"  <b>Model:</b> {llm_model}\n\n"
                f"<b><u>Memory</u></b>\n"
                f"  <b>History DB:</b> {db_exists}\n"
                f"  <b>RAG Engine:</b> {rag_status}\n"
            )

            if update.message:
                await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)
            logger.info(f"[{chat_id}] 已发送状态信息。")
        except Exception as e:
            logger.error(f"[{chat_id}] 获取状态时出错: {e}", exc_info=True)
            if update.message:
                await update.message.reply_text("❌ 获取系统状态时发生错误。")

    async def _handle_incoming_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id # Set current chat_id
        if not update.message or not update.message.text:
            return
        text = update.message.text
        
        # 处理回复消息
        reply_info = await self._extract_reply_info(update.message)
        if reply_info:
            text = f"[回复: {reply_info}]\n{text}"
        
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理图片消息"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        if not update.message or not update.message.photo:
            return
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
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id
        
        if not update.message or not update.message.document:
            return
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
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
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
        
        reply_msg = message.reply_to_message
        reply_chat_id = str(reply_msg.chat.id)
        reply_msg_id = str(reply_msg.message_id)
        
        # 从历史记录中获取回复内容
        history = self.message_history.get_context_messages("telegram", reply_chat_id)
        if not history:
            return None
        
        # 查找对应的回复消息
        for msg in history:
            if msg["msg_id"] == reply_msg_id:
                return msg["content"]
        
        return None
