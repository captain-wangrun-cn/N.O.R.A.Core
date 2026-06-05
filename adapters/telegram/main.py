from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.constants import ChatAction, ParseMode
from telegram.error import TimedOut, RetryAfter
from typing import Callable, Any, Optional, List, Dict, Tuple
from pathlib import Path
import os
import re
import logging
import io
import asyncio
import json
import time
import uuid
import importlib
import traceback

from adapters.base import BaseAdapter, PlatformFeatures
from adapters.aggregator import MessageAggregator
import config
from memory.message_history import MessageHistory
import sys
import platform
from brain.llm import get_llm_client
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

# Telegram limits
TG_PHOTO_MAX_SIZE = 10 * 1024 * 1024       # 10MB for photos
TG_DOCUMENT_MAX_SIZE = 50 * 1024 * 1024     # 50MB for documents
COMPRESS_TARGET_SIZE = 9 * 1024 * 1024       # Compress target: 9MB (safety margin)
TG_MSG_MAX_LENGTH = 4096                     # Telegram message text limit
HISTORICAL_REPLY_IMAGE_DIRECT_MARKER = "[NORA_HISTORICAL_REPLY_IMAGE_DIRECT]"


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

    DEBUG_CLEANUP_COMMAND = "/debug_cleanup"
    MODEL_COMMAND = "/model"
    NORA_PREFS_COMMAND = "/nora_prefs"
    _cleanup_confirm_tokens: Dict[str, float] = {}
    _cleanup_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 平台元信息
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "telegram"

    @property
    def platform_features(self) -> PlatformFeatures:
        return PlatformFeatures(
            supports_edit_message=True,
            supports_delete_message=True,
            supports_reactions=False,       # Telegram Bot API 对 reaction 支持有限
            supports_typing_indicator=True,
            supports_rich_media=True,
            supports_reply=True,
            supports_threads=False,
            supports_buttons=True,
            max_message_length=TG_MSG_MAX_LENGTH,
        )

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

    # 统一匹配：支持多种嵌入语法 + 原始文件路径 + HTTP URLs
    FILE_PATTERN = re.compile(
        r'(?:!\[.*?\]\((.*?)\))|'                  # Markdown: ![alt](path)
        r'(?:<img\s+src="(.*?)"[^>]*>)|'            # HTML: <img src="path">
        r'(?:\[(?:image|file|audio|video|doc):\s*(.*?)\])|'  # 自定义: [image: path]...
        r'(?:^|\s)((?:https?://[^\s]+\.(?:' +
        '|'.join(
            ext.lstrip('.') for exts in MEDIA_TYPES.values() for ext in exts
        ) + r'))\b)|'                               # URL匹配: http://example.com/image.png
        r'(?:^|\s)((?:/|\.{0,2}/|[a-zA-Z]:\\)[^\s<>"\']+\.(?:' +
        '|'.join(
            ext.lstrip('.') for exts in MEDIA_TYPES.values() for ext in exts
        ) + r'))\b',                                 # 原始文件路径
        re.IGNORECASE | re.MULTILINE
    )

    def __init__(self):
        super().__init__()
        token = config.get_telegram_token()
        if not token:
            raise ValueError("Telegram Bot Token not found in config.")

        workspace_manager = get_workspace_manager()
        self.workspace_root = str(workspace_manager.root)
        self.downloads_dir = str(workspace_manager.downloads_dir)
        self.data_dir = str(workspace_manager.data_dir)
        self.telegram_data_dir = os.path.join(self.data_dir, "telegram")
        os.makedirs(self.telegram_data_dir, exist_ok=True)
        
        self.application = ApplicationBuilder().token(token).build()
        self._aggregator: MessageAggregator | None = None
        self.message_history = MessageHistory()
        self._reply_contexts: Dict[str, Dict] = {}  # 存储回复上下文 {chat_id: {msg_id: context}}
        self.bot_username: Optional[str] = None
        # 媒体组（相册）缓冲：media_group_id -> {"photos": [...], "caption": str, "chat_id": str, ...}
        self._media_group_buffers: Dict[str, Dict[str, Any]] = {}
        self._media_group_timers: Dict[str, asyncio.Task] = {}
        self._model_option_tokens: Dict[str, Dict[str, Any]] = {}

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

    def run(self, message_handler: Callable[[Dict[str, Any]], Any]):
        self._message_handler = message_handler
        app_config = config.get_config()
        interaction_config = app_config.get("interaction", {}) if app_config else {}
        buffer_timeout = interaction_config.get("buffer_timeout", 3.0)

        self._aggregator = MessageAggregator(
            timeout=buffer_timeout,
            on_complete=self.on_aggregator_complete
        )

        start_handler = CommandHandler('start', self._start_command)
        stop_handler = CommandHandler('stop', self._stop_command)
        clear_handler = CommandHandler('clear', self._clear_command)
        status_handler = CommandHandler('status', self._status_command)
        debug_handler = CommandHandler('debug', self._debug_command)
        regenerate_proactive_handler = CommandHandler('regenerate_proactive', self._regenerate_proactive_command)
        schedule_today_handler = CommandHandler('schedule_today', self._schedule_today_command)
        custom_scope_handler = CommandHandler('custom_scope', self._custom_scope_command)
        nora_prefs_handler = CommandHandler('nora_prefs', self._nora_prefs_command)
        set_stream_handler = CommandHandler('set_stream', self._set_stream_command)
        legacy_nonstream_handler = CommandHandler('nonstream', self._set_stream_command)
        context_handler = CommandHandler('context', self._context_command)
        debug_cleanup_handler = CommandHandler('debug_cleanup', self._debug_cleanup_command)
        model_handler = CommandHandler('model', self._model_command)
        msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), self._handle_incoming_message)
        photo_handler = MessageHandler(filters.PHOTO, self._handle_photo)
        video_handler = MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, self._handle_video)
        document_handler = MessageHandler(filters.Document.ALL, self._handle_document)
        sticker_handler = MessageHandler(filters.Sticker.ALL, self._handle_sticker)
        callback_handler = CallbackQueryHandler(self._handle_callback)
        
        self.application.add_handler(start_handler)
        self.application.add_handler(stop_handler)
        self.application.add_handler(clear_handler)
        self.application.add_handler(status_handler)
        self.application.add_handler(debug_handler)
        self.application.add_handler(regenerate_proactive_handler)
        self.application.add_handler(schedule_today_handler)
        self.application.add_handler(custom_scope_handler)
        self.application.add_handler(nora_prefs_handler)
        self.application.add_handler(set_stream_handler)
        self.application.add_handler(legacy_nonstream_handler)
        self.application.add_handler(context_handler)
        self.application.add_handler(debug_cleanup_handler)
        self.application.add_handler(model_handler)
        self.application.add_handler(msg_handler)
        self.application.add_handler(photo_handler)
        self.application.add_handler(video_handler)
        self.application.add_handler(document_handler)
        self.application.add_handler(sticker_handler)
        self.application.add_handler(callback_handler)
        
        # Use post_init to run async setup, which is more reliable
        self.application.post_init = self._post_init_setup

        logger.info("Telegram Adapter is running...")
        self.application.run_polling(stop_signals=None)

    async def _post_init_setup(self, application: Application):
        """完成 Telegram 启动后初始化，并触发生命周期钩子。"""
        await self.startup()
        try:
            bot_info = await application.bot.get_me()
            self.bot_username = bot_info.username
            logger.info(f"成功获取机器人用户名: @{self.bot_username}")
        except Exception as e:
            logger.error(f"无法获取机器人用户名: {e}")
            self.bot_username = None
        # 触发基类 on_ready 钩子
        await self.on_ready()

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
            
            # 2a. 检查是否是回复机器人的消息
            if update.message.reply_to_message and update.message.reply_to_message.from_user:
                if update.message.reply_to_message.from_user.id == self.application.bot.id:
                    return True
            
            # 2b. 检查是否 @机器人 (case-insensitive)
            if self.bot_username:
                # 检查文本消息或媒体消息的标题
                text_to_check = update.message.text or update.message.caption or ""
                if f"@{self.bot_username.lower()}" in text_to_check.lower():
                    return True
        
        # 默认不处理
        return False

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if self._message_handler:
            # Propagate start command as a special message
            event_context = {
                "platform": "telegram",
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": "/start",
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
            }
            await self._message_handler(event_context)

    async def _clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        # 优先交由 controller 统一清理（包含内存上下文）
        if self._message_handler:
            event_context = {
                "platform": "telegram",
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": "/clear",
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
            }
            await self._message_handler(event_context)
            return

        # fallback: 直接清数据库（清理所有，不保留 pinned）
        try:
            storage_id = chat_id if update.effective_chat.type != "private" else (str(update.effective_user.id) if update.effective_user else chat_id)
            self.message_history.clear_chat_history("telegram", storage_id, keep_pinned=False)
            if update.message:
                await update.message.reply_text("✅ 当前聊天的历史记录已清空。")
            logger.info(f"[{chat_id}] 聊天历史已通过 /clear 命令清空。")
        except Exception as e:
            logger.error(f"[{chat_id}] 清空历史记录时出错: {e}")
            if update.message:
                await update.message.reply_text("❌ 清空历史记录时发生错误。")

    async def _stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        # 交由 controller 统一停止前/后脑任务
        if self._message_handler:
            event_context = {
                "platform": "telegram",
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": "/stop",
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
            }
            await self._message_handler(event_context)
            return
        # fallback: 没有 controller 时提示
        if update.message:
            await update.message.reply_text("⚠️ 当前无法停止任务。")

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

    async def _context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """显示上下文窗口使用情况，透传给 controller。"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = {
            "platform": "telegram",
            "chat_id": chat_id,
            "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
            "text": "/context",
            "chat_type": update.effective_chat.type,
            "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
        }
        await self._message_handler(event_context)

    async def _debug_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """切换 debug 模式，透传给 controller。"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = {
            "platform": "telegram",
            "chat_id": chat_id,
            "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
            "text": "/debug",
            "chat_type": update.effective_chat.type,
            "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
        }
        await self._message_handler(event_context)

    async def _debug_cleanup_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """调试清空入口：弹出危险操作按钮（需二次确认）。"""
        if not update.effective_chat or not update.message:
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 清空 Qdrant 记忆", callback_data="debug_cleanup:qdrant")],
            [InlineKeyboardButton("🧹 清空 Mongo 图片库", callback_data="debug_cleanup:mongo")],
            [InlineKeyboardButton("🧹 清空消息镜像库", callback_data="debug_cleanup:mirror")],
            [InlineKeyboardButton("🧹 清空上下文压缩库", callback_data="debug_cleanup:context")],
            [InlineKeyboardButton("🧹 清空聊天记录", callback_data="debug_cleanup:chat")],
            [InlineKeyboardButton("💥 全部清空", callback_data="debug_cleanup:all")],
            [InlineKeyboardButton("取消", callback_data="debug_cleanup:cancel")],
        ])

        warning_text = (
            "⚠️ <b>危险操作</b>\n"
            "以下清空操作均为不可恢复的永久删除。\n"
            "请选择要执行的清理范围。"
        )
        await update.message.reply_text(warning_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    async def _model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """展示模型切换按钮并允许保存配置。"""
        if not update.effective_chat or not update.message:
            return

        text, markup = self._build_model_alias_menu()
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    def _build_model_alias_menu(self):
        """构造 /model 顶层 alias 选择菜单。"""
        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}

        buttons = []
        for alias in ["smart", "fast", "coder", "image", "video", "security", "summary"]:
            current = models_cfg.get(alias, "")
            label = f"{alias}: {current or '未设置'}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model_pick:{alias}")])
        buttons.append([InlineKeyboardButton("关闭", callback_data="model_pick:cancel")])

        text = (
            "🧠 <b>模型切换</b>\n"
            "点击要修改的模型别名，然后输入新的模型名称。\n"
            "将立即保存到 config.yml。"
        )
        return text, InlineKeyboardMarkup(buttons)

    def _get_provider_for_alias(self, cfg: Dict[str, Any], alias: str) -> str:
        llm_cfg = cfg.get("llm", {}) or {}
        model_providers = llm_cfg.get("model_providers", {}) or {}
        return model_providers.get(alias) or llm_cfg.get("provider", "gemini")

    def _get_provider_config(self, cfg: Dict[str, Any], provider_name: str) -> Dict[str, Any]:
        llm_cfg = cfg.get("llm", {}) or {}
        providers_cfg = llm_cfg.get("providers", {}) or {}
        if provider_name in providers_cfg:
            return providers_cfg.get(provider_name) or {}

        api_keys = llm_cfg.get("api_keys", {}) or {}
        legacy = {
            "type": provider_name,
            "api_key": api_keys.get(provider_name, ""),
        }
        if provider_name == "openai":
            if llm_cfg.get("base_url"):
                legacy["base_url"] = llm_cfg.get("base_url")
            if llm_cfg.get("user_agent"):
                legacy["user_agent"] = llm_cfg.get("user_agent")
        return legacy

    def _fetch_provider_models(self, provider_name: str, provider_cfg: Dict[str, Any]) -> List[str]:
        provider_type = provider_cfg.get("type", provider_name)
        api_key = provider_cfg.get("api_key", "")
        if not api_key:
            return []

        try:
            if provider_type == "gemini":
                genai = importlib.import_module("google.generativeai")
                if hasattr(genai, "configure"):
                    genai.configure(api_key=api_key)
                if hasattr(genai, "list_models"):
                    models = [
                        m.name.replace("models/", "")
                        for m in genai.list_models()
                        if "generateContent" in getattr(m, "supported_generation_methods", [])
                    ]
                    return sorted(models)
                return []
            if provider_type == "openai":
                openai_module = importlib.import_module("openai")
                base_url = provider_cfg.get("base_url") or None
                client = openai_module.OpenAI(api_key=api_key, base_url=base_url)
                return sorted([model.id for model in client.models.list()])
        except Exception:
            return []

        return []

    async def _regenerate_proactive_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        手动触发“重新生成主动消息计划”。

        用法:
        - /regenerate_proactive
        - /regenerate_proactive append   (追加)
        - /regenerate_proactive replace  (覆盖，默认)
        """
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        mode = (context.args[0].strip().lower() if context and context.args else "replace")
        if mode not in ("replace", "append"):
            mode = "replace"

        # 透传为控制器命令，统一在 controller 层执行调度逻辑
        event_context = {
            "platform": "telegram",
            "chat_id": chat_id,
            "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
            "text": f"/regenerate_proactive {mode}",
            "chat_type": update.effective_chat.type,
            "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
        }
        await self._message_handler(event_context)

    async def _schedule_today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看今日主动消息计划。"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        event_context = {
            "platform": "telegram",
            "chat_id": chat_id,
            "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
            "text": "/schedule_today",
            "chat_type": update.effective_chat.type,
            "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
        }
        await self._message_handler(event_context)

    async def _custom_scope_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """设置 CUSTOM.md 注入范围。"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        try:
            scopes = self._load_custom_scopes()
            if update.message:
                await update.message.reply_text(
                    self._render_custom_scope_text(scopes),
                    reply_markup=self._render_custom_scope_keyboard(scopes),
                )
        except Exception:
            logger.error(f"[{chat_id}] /custom_scope 按钮初始化失败", exc_info=True)
            if update.message:
                await update.message.reply_text("❌ 无法加载按钮。")

    async def _nora_prefs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """展示 Nora 偏好设置按钮。"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)

        try:
            prefs = self._load_nora_preferences()
            if update.message:
                await update.message.reply_text(
                    self._render_nora_prefs_text(prefs),
                    reply_markup=self._render_nora_prefs_keyboard(prefs),
                )
        except Exception:
            logger.error(f"[{chat_id}] /nora_prefs 按钮初始化失败", exc_info=True)
            if update.message:
                await update.message.reply_text("❌ 无法加载 Nora 偏好设置。")

    async def _set_stream_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """切换当前会话的输出模式。用法：/set_stream on|off，或点击按钮选择。"""
        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        if not self._message_handler:
            if update.message:
                await update.message.reply_text("❌ 指令处理器未就绪。")
            return

        arg = (context.args[0].strip().lower() if context and context.args else "").replace("\n", " ")
        if arg in ("on", "off"):
            event_context = {
                "platform": "telegram",
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": f"/set_stream {arg}",
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
            }
            await self._message_handler(event_context)
            return

        if arg:
            if update.message:
                await update.message.reply_text("用法：/set_stream on|off（on=一次性输出，off=流式输出）")
            return

        event_context = {
            "platform": "telegram",
            "chat_id": chat_id,
            "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
            "text": "/set_stream",
            "chat_type": update.effective_chat.type,
            "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
        }
        await self._message_handler(event_context)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 启用一次性输出", callback_data="set_stream:on")],
            [InlineKeyboardButton("🔵 恢复流式输出", callback_data="set_stream:off")],
        ])
        prompt_text = (
            "请选择输出模式：\n"
            "- 🟢 启用一次性输出（非流式，完整内容一次发出）\n"
            "- 🔵 恢复流式输出（逐步流式发送）"
        )
        if update.message:
            await update.message.reply_text(prompt_text, reply_markup=keyboard)

    async def _handle_incoming_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._should_process_message(update):
            return

        if not update.effective_chat:
            return
        chat_id = str(update.effective_chat.id)
        self.current_chat_id = chat_id # Set current chat_id
        text = update.message.text if update.message else ""

        
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
            full_context = {
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": text,
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown",
                "platform": "telegram",
                "platform_message_id": str(update.message.message_id) if update.message else None,
            }
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
        caption = update.message.caption or ""
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
                    "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                    "chat_type": update.effective_chat.type,
                    "user_name": update.effective_user.first_name if update.effective_user else "Unknown",
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
                full_context = {
                    "chat_id": chat_id,
                    "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                    "text": text,
                    "chat_type": update.effective_chat.type,
                    "user_name": update.effective_user.first_name if update.effective_user else "Unknown",
                    "platform": "telegram",
                    "platform_message_id": str(update.message.message_id) if update.message else None,
                }
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
        platform_msg_id = buf.get("message_ids", [None])[0]
        if self._aggregator:
            full_context = {
                "chat_id": chat_id,
                "user_id": buf["user_id"],
                "text": text,
                "chat_type": buf["chat_type"],
                "user_name": buf["user_name"],
                "platform": buf.get("platform", "telegram"),
                "platform_message_id": platform_msg_id,
            }
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

        caption = update.message.caption or ""
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
                    "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                    "chat_type": update.effective_chat.type,
                    "user_name": update.effective_user.first_name if update.effective_user else "Unknown",
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
                full_context = {
                    "chat_id": chat_id,
                    "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                    "text": text,
                    "chat_type": update.effective_chat.type,
                    "user_name": update.effective_user.first_name if update.effective_user else "Unknown",
                    "platform": "telegram",
                    "platform_message_id": str(update.message.message_id) if update.message else None,
                }
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
        caption = update.message.caption or ""
        
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
            full_context = {
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": text,
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
            }
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
            full_context = {
                "chat_id": chat_id,
                "user_id": str(update.effective_user.id) if update.effective_user else chat_id,
                "text": text,
                "chat_type": update.effective_chat.type,
                "user_name": update.effective_user.first_name if update.effective_user else "Unknown"
            }
            await self._aggregator.add_message(chat_id, text, full_context)
        
        logger.info(f"[{chat_id}] 收到贴纸: {rel_file_path}")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 inline keyboard 回调"""
        query = update.callback_query
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
        callback_data = query.data
        
        # 确认回调
        await query.answer()

        if callback_data and callback_data.startswith("debug_cleanup_confirm:"):
            action = callback_data.split(":", 1)[1]
            await self._handle_debug_cleanup_confirm(query, action)
            return

        if callback_data and callback_data.startswith("model_pick:"):
            await self._handle_model_pick_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_clear:"):
            await self._handle_model_clear_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_set:"):
            await self._handle_model_set_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_page:"):
            await self._handle_model_page_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("model_provider:"):
            await self._handle_model_provider_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("debug_cleanup:"):
            await self._handle_debug_cleanup_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("custom_scope:"):
            await self._handle_custom_scope_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("nora_prefs:"):
            await self._handle_nora_prefs_callback(query, callback_data)
            return

        if callback_data and callback_data.startswith("set_stream:"):
            await self._handle_set_stream_callback(query, callback_data)
            return
        
        # 构造消息
        text = f"[按钮点击: {callback_data}]"
        
        if self._aggregator:
            full_context = {
                "chat_id": chat_id,
                "user_id": str(query.from_user.id) if query.from_user else chat_id,
                "text": text,
                "chat_type": query.message.chat.type if query.message else "private",
                "user_name": query.from_user.first_name if query.from_user else "Unknown"
            }
            await self._aggregator.add_message(chat_id, text, full_context)
        
        logger.info(f"[{chat_id}] 收到回调: {callback_data}")

    async def _handle_debug_cleanup_callback(self, query, callback_data: str):
        """处理调试清空按钮回调（带二次确认）。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)

        action = callback_data.split(":", 1)[1]
        if action == "cancel":
            await query.edit_message_text("已取消清空操作。")
            return

        token_key = f"{chat_id}:{action}"
        now_ts = time.time()

        async with self._cleanup_lock:
            if token_key not in self._cleanup_confirm_tokens:
                self._cleanup_confirm_tokens[token_key] = now_ts
                confirm_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❗确认删除", callback_data=f"debug_cleanup_confirm:{action}")],
                    [InlineKeyboardButton("取消", callback_data="debug_cleanup:cancel")],
                ])
                await query.edit_message_text(
                    "⚠️ <b>二次确认</b>\n"
                    "该操作将永久删除数据，且不可恢复。\n"
                    "若确认，请点击下方按钮。",
                    reply_markup=confirm_keyboard,
                    parse_mode=ParseMode.HTML,
                )
                return

        await query.edit_message_text("⚠️ 确认信息已过期，请重新发起 /debug_cleanup。")

    async def _handle_debug_cleanup_confirm(self, query, action: str):
        """执行清空操作。"""
        chat_id = str(query.message.chat.id)
        token_key = f"{chat_id}:{action}"
        now_ts = time.time()

        async with self._cleanup_lock:
            created_at = self._cleanup_confirm_tokens.get(token_key)
            if not created_at or now_ts - created_at > 60:
                if token_key in self._cleanup_confirm_tokens:
                    self._cleanup_confirm_tokens.pop(token_key, None)
                await query.edit_message_text("⚠️ 确认已超时，请重新发起 /debug_cleanup。")
                return
            self._cleanup_confirm_tokens.pop(token_key, None)

        result_lines = ["✅ 清空完成"]

        if action in ("qdrant", "all"):
            result_lines.append(self._purge_qdrant())
        if action in ("mongo", "all"):
            result_lines.append(self._purge_mongo())
        if action in ("mirror", "all"):
            result_lines.append(self._purge_message_log())
        if action in ("context", "all"):
            result_lines.append(self._purge_context_db())
        if action in ("chat", "all"):
            result_lines.append(self._purge_chat_history())

        await query.edit_message_text("\n".join(result_lines))

    async def _handle_set_stream_callback(self, query, callback_data: str):
        """处理 /set_stream 选择按钮回调。"""
        if not query or not query.message:
            return
        if not self._message_handler:
            await query.answer("指令处理器未就绪", show_alert=True)
            return

        chat_id = str(query.message.chat.id)
        user_id = str(query.from_user.id) if query.from_user else chat_id
        action = callback_data.split(":", 1)[1] if ":" in callback_data else ""
        if action not in ("on", "off"):
            await query.answer("未知选项", show_alert=True)
            return

        event_context = {
            "platform": "telegram",
            "chat_id": chat_id,
            "user_id": user_id,
            "text": f"/set_stream {action}",
            "chat_type": query.message.chat.type if query.message else "private",
            "user_name": query.from_user.first_name if query.from_user else "Unknown",
        }

        await self._message_handler(event_context)

        status_text = "✅ 已启用一次性输出" if action == "on" else "✅ 已恢复流式输出"
        try:
            await query.edit_message_text(status_text)
        except Exception:
            logger.debug(f"[{chat_id}] set_stream edit_message_text 失败", exc_info=True)
        try:
            await query.answer(status_text, show_alert=False)
        except Exception:
            pass

    async def _handle_custom_scope_callback(self, query, callback_data: str):
        """处理 CUSTOM scope 按钮回调。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
        action = callback_data.split(":", 1)[1]

        if action == "close":
            scopes = self._load_custom_scopes()
            await query.edit_message_text(self._render_custom_scope_text(scopes))
            return

        if action not in {"fast", "smart", "image", "coder", "none"}:
            await query.edit_message_text("❌ 无效范围。")
            return

        try:
            scopes = self._load_custom_scopes()
            # Toggle logic
            if action == "none":
                # 切换 none：如果当前是 none 则恢复为 all（空列表），否则设为 none
                if any(s in ("none", "off") for s in scopes):
                    scopes = []
                else:
                    scopes = ["none"]
            else:
                scopes = [s for s in scopes if s not in ("none", "off")]
                if action in scopes:
                    scopes = [s for s in scopes if s != action]
                else:
                    scopes.append(action)

            self._save_custom_scopes(scopes)

            # Refresh keyboard & text
            scopes = self._load_custom_scopes()
            await query.edit_message_text(
                self._render_custom_scope_text(scopes),
                reply_markup=self._render_custom_scope_keyboard(scopes),
            )
        except Exception as e:
            logger.error(f"[{chat_id}] custom_scope 回调失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 失败: {e}")

    def _load_custom_scopes(self) -> list:
        cfg = config.get_config() or {}
        scopes = (cfg.get("custom_injection", {}) or {}).get("scopes") or []
        # normalize
        scopes = [str(s).strip().lower() for s in scopes if str(s).strip()]
        return scopes

    def _save_custom_scopes(self, scopes: list):
        config.set_custom_injection_scopes(scopes)

    def _render_custom_scope_text(self, scopes: list) -> str:
        if any(s in ("none", "off") for s in scopes):
            desc = "none"
        elif not scopes:
            desc = "all"
        else:
            desc = ", ".join(scopes)
        return (
            "🧭 CUSTOM 注入范围\n"
            f"当前: {desc}\n"
            "点击勾选/取消需要的 scope，实时保存。\n"
            "空列表=全量注入，none=关闭全部。"
        )

    def _render_custom_scope_keyboard(self, scopes: list) -> InlineKeyboardMarkup:
        def mark(name: str) -> str:
            return "✅ " if name in scopes else "☑ "

        none_mark = "✅ " if any(s in ("none", "off") for s in scopes) else "☑ "
        rows = [
            [
                InlineKeyboardButton(f"{mark('fast')}fast", callback_data="custom_scope:fast"),
                InlineKeyboardButton(f"{mark('smart')}smart", callback_data="custom_scope:smart"),
            ],
            [
                InlineKeyboardButton(f"{mark('image')}image", callback_data="custom_scope:image"),
                InlineKeyboardButton(f"{mark('coder')}coder", callback_data="custom_scope:coder"),
            ],
            [InlineKeyboardButton(f"{none_mark}关闭全部", callback_data="custom_scope:none")],
            [InlineKeyboardButton("取消", callback_data="custom_scope:close")],
        ]
        return InlineKeyboardMarkup(rows)

    async def _handle_nora_prefs_callback(self, query, callback_data: str):
        """处理 Nora 偏好按钮回调。"""
        if not query or not query.message:
            return
        chat_id = str(query.message.chat.id)
        action = callback_data.split(":", 1)[1]

        if action == "close":
            prefs = self._load_nora_preferences()
            await query.edit_message_text(self._render_nora_prefs_text(prefs))
            return

        editable = {
            "nora_followup_probability",
            "proactive_message_probability",
            "split_reply_probability",
            "short_reply_preference",
            "verbosity_preference",
            "pause_between_splits_seconds",
            "warmth_level",
            "playfulness_level",
            "emotional_expressiveness",
            "assertiveness_level",
        }
        if action not in editable:
            await query.edit_message_text("❌ 无效偏好项。")
            return

        try:
            prefs = self._load_nora_preferences()
            prefs[action] = self._next_nora_pref_value(action, prefs.get(action))
            self._save_nora_preferences(prefs)
            prefs = self._load_nora_preferences()
            await query.edit_message_text(
                self._render_nora_prefs_text(prefs),
                reply_markup=self._render_nora_prefs_keyboard(prefs),
            )
        except Exception as e:
            logger.error(f"[{chat_id}] nora_prefs 回调失败: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 失败: {e}")

    def _load_nora_preferences(self) -> dict:
        return dict(config.get_nora_preferences())

    def _save_nora_preferences(self, prefs: dict):
        config.set_nora_preferences(prefs)

    def _format_nora_pref_value(self, key: str, value: Any) -> str:
        if key == "pause_between_splits_seconds":
            return f"{float(value):.1f}s"
        return f"{float(value):.2f}"

    def _next_nora_pref_value(self, key: str, current: Any) -> float:
        defaults = config.get_nora_preferences()
        try:
            value = float(current)
        except (TypeError, ValueError):
            value = float(defaults.get(key, 0.0))

        if key == "pause_between_splits_seconds":
            choices = [0.3, 0.6, 1.0, 1.4, 1.8, 2.2]
        else:
            choices = [0.0, 0.25, 0.5, 0.75, 1.0]

        rounded = round(value, 1 if key == "pause_between_splits_seconds" else 2)
        for idx, candidate in enumerate(choices):
            if abs(candidate - rounded) < 1e-6:
                return choices[(idx + 1) % len(choices)]
        higher = [candidate for candidate in choices if candidate > rounded]
        if higher:
            return higher[0]
        return choices[0]

    def _render_nora_prefs_text(self, prefs: dict) -> str:
        lines = [
            "🎛️ Nora 偏好设置",
            "点击按钮循环切换数值，修改会立即保存到 `config.yml`。",
            "",
            f"- followup 概率: {self._format_nora_pref_value('nora_followup_probability', prefs.get('nora_followup_probability', 1.0))}",
            f"- 自主主动消息概率: {self._format_nora_pref_value('proactive_message_probability', prefs.get('proactive_message_probability', 1.0))}",
            f"- 分段聊天倾向: {self._format_nora_pref_value('split_reply_probability', prefs.get('split_reply_probability', 0.7))}",
            f"- 短回复偏好: {self._format_nora_pref_value('short_reply_preference', prefs.get('short_reply_preference', 0.5))}",
            f"- 详细解释偏好: {self._format_nora_pref_value('verbosity_preference', prefs.get('verbosity_preference', 0.5))}",
            f"- 默认分段停顿: {self._format_nora_pref_value('pause_between_splits_seconds', prefs.get('pause_between_splits_seconds', 1.0))}",
            f"- 温柔度: {self._format_nora_pref_value('warmth_level', prefs.get('warmth_level', 0.7))}",
            f"- 俏皮度: {self._format_nora_pref_value('playfulness_level', prefs.get('playfulness_level', 0.4))}",
            f"- 情绪表达: {self._format_nora_pref_value('emotional_expressiveness', prefs.get('emotional_expressiveness', 0.6))}",
            f"- 主张程度: {self._format_nora_pref_value('assertiveness_level', prefs.get('assertiveness_level', 0.5))}",
        ]
        return "\n".join(lines)

    def _render_nora_prefs_keyboard(self, prefs: dict) -> InlineKeyboardMarkup:
        def button(label: str, key: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                f"{label}: {self._format_nora_pref_value(key, prefs.get(key))}",
                callback_data=f"nora_prefs:{key}",
            )

        rows = [
            [button("Followup", "nora_followup_probability")],
            [button("Proactive", "proactive_message_probability")],
            [button("Split", "split_reply_probability"), button("Pause", "pause_between_splits_seconds")],
            [button("Short", "short_reply_preference"), button("Verbose", "verbosity_preference")],
            [button("Warmth", "warmth_level"), button("Playful", "playfulness_level")],
            [button("Emotion", "emotional_expressiveness"), button("Assertive", "assertiveness_level")],
            [InlineKeyboardButton("关闭", callback_data="nora_prefs:close")],
        ]
        return InlineKeyboardMarkup(rows)

    async def _handle_model_pick_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        alias = callback_data.split(":", 1)[1]

        if alias == "cancel":
            await query.edit_message_text("已关闭模型切换。")
            return

        if alias == "back":
            text, markup = self._build_model_alias_menu()
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        providers_cfg = llm_cfg.get("providers", {}) or {}
        provider_names = list(providers_cfg.keys())

        if not provider_names:
            default_provider = llm_cfg.get("provider", "gemini")
            provider_names = [default_provider]

        buttons = []
        for provider_name in provider_names:
            provider_type = (providers_cfg.get(provider_name, {}) or {}).get("type", provider_name)
            label = f"{provider_name} ({provider_type})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model_provider:{alias}:{provider_name}")])
        # 可选模型（video, security）允许清除配置
        if alias in ("video", "security"):
            buttons.append([InlineKeyboardButton("🗑 清除（不使用）", callback_data=f"model_clear:{alias}")])
        buttons.append([
            InlineKeyboardButton("⬅ 返回", callback_data="model_pick:back"),
            InlineKeyboardButton("取消", callback_data="model_pick:cancel"),
        ])

        await query.edit_message_text(
            f"请选择 {alias} 的 Provider：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _handle_model_clear_callback(self, query, callback_data: str):
        """清除可选模型（video/security）的配置。"""
        alias = callback_data.split(":", 1)[1]
        try:
            cfg = config.get_config() or {}
            llm_cfg = cfg.setdefault("llm", {})
            models_cfg = llm_cfg.setdefault("models", {})
            model_providers = llm_cfg.setdefault("model_providers", {})
            models_cfg.pop(alias, None)
            model_providers.pop(alias, None)
            config.save_config(cfg)
            await query.edit_message_text(f"✅ 已清除 {alias} 模型配置。")
        except Exception as e:
            await query.edit_message_text(f"❌ 清除失败: {e}")

    async def _apply_model_update(self, update: Update, alias: str, model_name: str):
        if not update.effective_chat or not update.message:
            return

        try:
            cfg = config.get_config() or {}
            llm_cfg = cfg.setdefault("llm", {})
            models_cfg = llm_cfg.setdefault("models", {})
            models_cfg[alias] = model_name
            config.save_config(cfg)
            await update.message.reply_text(f"✅ 已更新 {alias} 模型为: {model_name}")
        except Exception as e:
            await update.message.reply_text(f"❌ 更新失败: {e}")

    async def _handle_model_set_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ 回调参数错误。")
            return
        _, alias, token = parts
        token_entry = self._model_option_tokens.pop(token, None)
        if not token_entry or token_entry.get("kind") != "model_set":
            await query.edit_message_text("⚠️ 选项已过期，请重新发起 /model。")
            return
        chat_id = token_entry.get("chat_id")
        model_name = token_entry.get("model")
        provider_name = token_entry.get("provider")
        if str(query.message.chat.id) != str(chat_id):
            await query.edit_message_text("⚠️ 当前会话无法使用该选项。")
            return

        try:
            cfg = config.get_config() or {}
            llm_cfg = cfg.setdefault("llm", {})
            models_cfg = llm_cfg.setdefault("models", {})
            models_cfg[alias] = model_name
            if provider_name:
                llm_cfg.setdefault("model_providers", {})[alias] = provider_name
            config.save_config(cfg)
            await query.edit_message_text(f"✅ 已更新 {alias} 模型为: {model_name}\n正在刷新模型...")

            if self._message_handler:
                event_context = {
                    "platform": "telegram",
                    "chat_id": str(query.message.chat.id),
                    "user_id": str(query.from_user.id) if query.from_user else str(query.message.chat.id),
                    "text": "/reload_models",
                    "chat_type": query.message.chat.type if query.message else "private",
                    "user_name": query.from_user.first_name if query.from_user else "Unknown"
                }
                await self._message_handler(event_context)
        except Exception as e:
            await query.edit_message_text(f"❌ 更新失败: {e}")

    async def _handle_model_page_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ 回调参数错误。")
            return
        _, alias, token = parts
        token_entry = self._model_option_tokens.pop(token, None)
        if not token_entry or token_entry.get("kind") != "model_page":
            await query.edit_message_text("⚠️ 选项已过期，请重新发起 /model。")
            return
        chat_id = token_entry.get("chat_id")
        model_options = token_entry.get("models") or []
        offset = int(token_entry.get("offset") or 0)
        provider_name = token_entry.get("provider") or ""
        if str(query.message.chat.id) != str(chat_id):
            await query.edit_message_text("⚠️ 当前会话无法使用该选项。")
            return

        if not model_options:
            await query.edit_message_text("⚠️ 未找到更多模型。")
            return

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}
        current_model = models_cfg.get(alias, "")

        await self._render_model_page(
            query,
            alias=alias,
            provider_name=provider_name,
            model_options=model_options,
            offset=offset,
        )

    async def _handle_model_provider_callback(self, query, callback_data: str):
        if not query or not query.message:
            return
        parts = callback_data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ 回调参数错误。")
            return
        _, alias, provider_name = parts

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        models_cfg = llm_cfg.get("models", {}) or {}
        current_model = models_cfg.get(alias, "")

        provider_cfg = self._get_provider_config(cfg, provider_name)
        provider_models = self._fetch_provider_models(provider_name, provider_cfg)
        model_options = list(dict.fromkeys(m for m in provider_models if m))
        if current_model and current_model not in model_options:
            model_options.insert(0, current_model)

        if not model_options:
            await query.edit_message_text("⚠️ 未找到可选模型，请检查 provider API Key 或网络。")
            return

        await self._render_model_page(
            query,
            alias=alias,
            provider_name=provider_name,
            model_options=model_options,
            offset=0,
        )

    async def _render_model_page(
        self,
        query,
        alias: str,
        provider_name: str,
        model_options: List[str],
        offset: int,
        page_size: int = 12,
    ):
        if not query or not query.message:
            return

        cfg = config.get_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        current_model = (llm_cfg.get("models", {}) or {}).get(alias, "")

        start = max(offset, 0)
        end = min(start + page_size, len(model_options))
        page_models = model_options[start:end]
        buttons = []
        for model_name in page_models:
            token = uuid.uuid4().hex[:10]
            self._model_option_tokens[token] = {
                "kind": "model_set",
                "chat_id": str(query.message.chat.id),
                "model": model_name,
                "provider": provider_name,
            }
            label = f"{model_name}"
            if model_name == current_model:
                label = f"✅ {label}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"model_set:{alias}:{token}")])

        nav_buttons = []
        if start > 0:
            prev_token = uuid.uuid4().hex[:10]
            self._model_option_tokens[prev_token] = {
                "kind": "model_page",
                "chat_id": str(query.message.chat.id),
                "models": model_options,
                "offset": max(start - page_size, 0),
                "provider": provider_name,
            }
            nav_buttons.append(InlineKeyboardButton("◀ 上一页", callback_data=f"model_page:{alias}:{prev_token}"))
        if end < len(model_options):
            next_token = uuid.uuid4().hex[:10]
            self._model_option_tokens[next_token] = {
                "kind": "model_page",
                "chat_id": str(query.message.chat.id),
                "models": model_options,
                "offset": end,
                "provider": provider_name,
            }
            nav_buttons.append(InlineKeyboardButton("下一页 ▶", callback_data=f"model_page:{alias}:{next_token}"))

        if nav_buttons:
            buttons.append(nav_buttons)
        buttons.append([
            InlineKeyboardButton("⬅ 返回", callback_data=f"model_pick:{alias}"),
            InlineKeyboardButton("取消", callback_data="model_pick:cancel"),
        ])

        await query.edit_message_text(
            f"选择 {alias} 的模型（来源: {provider_name}）：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    def _purge_qdrant(self) -> str:
        """清空所有 Qdrant 相关集合。"""
        messages = []
        try:
            from memory.vector import VectorStore
            vs = VectorStore()
            if not vs.client:
                return "⚠️ Qdrant 未连接，未执行。"
            vs.client.delete_collection(collection_name=vs.collection_name)
            vs._ensure_collection()
            messages.append(f"- Qdrant 记忆集合已清空: {vs.collection_name}")
        except Exception as e:
            messages.append(f"- Qdrant 记忆集合清空失败: {e}")

        try:
            from memory.image_store import ImageStore, IMAGE_COLLECTION
            img_store = ImageStore()
            if not img_store.qdrant:
                return "⚠️ Qdrant 未连接（图片库），未执行。"
            img_store.qdrant.delete_collection(collection_name=IMAGE_COLLECTION)
            img_store._ensure_qdrant_collection()
            messages.append(f"- Qdrant 图片集合已清空: {IMAGE_COLLECTION}")
        except Exception as e:
            messages.append(f"- Qdrant 图片集合清空失败: {e}")

        return "\n".join(messages) if messages else "⚠️ Qdrant 未执行。"

    def _purge_mongo(self) -> str:
        """清空 MongoDB 图片库集合。"""
        try:
            from memory.image_store import ImageStore, MONGO_COLLECTION
            img_store = ImageStore()
            if img_store.mongo_col is None:
                return "⚠️ MongoDB 未连接，未执行。"
            img_store.mongo_col.delete_many({})
            return f"- MongoDB 图片集合已清空: {MONGO_COLLECTION}"
        except Exception as e:
            return f"- MongoDB 清空失败: {e}"

    def _purge_chat_history(self) -> str:
        """清空全部聊天记录。"""
        try:
            self.message_history.clear_all_history(include_pinned=True)
            return "- 聊天记录已全部清空（含 pinned）"
        except Exception as e:
            return f"- 聊天记录清空失败: {e}"

    def _purge_message_log(self) -> str:
        """清空原文镜像库（message_log.db）。"""
        try:
            path = Path(self.message_history.message_log.db_path)
            if path.exists():
                path.unlink()
            # 重新初始化以确保表结构存在
            self.message_history.message_log._init_db()
            return f"- 消息镜像库已清空: {path.name}"
        except Exception as e:
            return f"- 消息镜像库清空失败: {e}"

    def _purge_context_db(self) -> str:
        """清空上下文压缩库（context_compression.db）。"""
        try:
            path = Path(self.message_history.context_compressor.db_path)
            if path.exists():
                path.unlink()
            # 重新初始化以确保表结构存在
            self.message_history.context_compressor._init_db()
            return f"- 上下文压缩库已清空: {path.name}"
        except Exception as e:
            return f"- 上下文压缩库清空失败: {e}"

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
                image_id = None
                if isinstance(md, dict):
                    image_id = md.get("image_id") or md.get("image_ids", [None])[0]
                if image_id:
                    image_input = await self._image_id_to_image_input(image_id, reply_msg)
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
                        image_id = md.get("image_id") or (md.get("image_ids") or [None])[0]
                        if image_id:
                            image_input = await self._image_id_to_image_input(image_id, reply_msg)
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
                store = self._get_image_store()
                if store is not None:
                    doc = store.get_by_platform_message_id("telegram", reply_msg_id, chat_id=reply_chat_id)
                    if doc:
                        image_input = self._image_doc_to_image_input(doc)
                        image_from_existing_store_file = bool(image_input)
                        if not image_input and doc.get("image_id"):
                            image_input = await self._image_id_to_image_input(doc["image_id"], reply_msg)
                        if not image_input:
                            image_input = await self._redownload_replied_photo_as_image_input(reply_msg)
                        if image_input:
                            if image_from_existing_store_file:
                                image_input = self._with_historical_reply_image_note(image_input)
                            image_id = doc.get("image_id", "")
                            caption_text = (reply_msg.caption or "").strip()
                            logger.info(
                                f"[telegram] reply 历史图片在 message_history 找不到，"
                                f"已通过 ImageStore 反查到图片文件" + (f" image_id={image_id}" if image_id else "")
                            )
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
            abs_file_path = os.path.join(self.telegram_data_dir, f"photo_{photo.file_id}.jpg")
            # 已存在就直接复用，不重复下载
            if not os.path.isfile(abs_file_path):
                file = await photo.get_file()
                await file.download_to_drive(abs_file_path)
            rel_file_path = os.path.relpath(abs_file_path, self.workspace_root).replace('\\', '/')
            return rel_file_path
        except Exception as e:
            logger.warning(f"[telegram] 重新下载历史图片失败: {e}")
            return None

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
        if clean_text:
            # Markdown → HTML 转换（LLM 有时会输出 Markdown 格式）
            html_text = _markdown_to_html(clean_text)
            
            # 分割超长消息（Telegram 限制 4096 字符）
            html_chunks = _split_long_text(html_text, TG_MSG_MAX_LENGTH)
            
            for chunk_html in html_chunks:
                try:
                    message = await self._send_with_retry(
                        lambda: self.application.bot.send_message(
                            chat_id=chat_id,
                            text=chunk_html,
                            parse_mode="HTML"
                        )
                    )
                except Exception as html_err:
                    logger.warning(f"[{chat_id}] HTML 发送失败 ({html_err})，降级为纯文本。")
                    # HTML 解析失败，降级发送纯文本（去除所有标签）
                    plain_chunk = re.sub(r'<[^>]+>', '', chunk_html)
                    # 纯文本也可能过长（标签去掉后不一定短多少），再次分割
                    plain_parts = _split_long_text(plain_chunk or chunk_html, TG_MSG_MAX_LENGTH)
                    for part in plain_parts:
                        message = await self._send_with_retry(
                            lambda: self.application.bot.send_message(
                                chat_id=chat_id,
                                text=part
                            )
                        )
                all_message_ids.append(str(message.message_id))

        # 4. 逐个发送媒体文件
        for file_path, media_type, is_url in file_entries:
            caption = os.path.basename(file_path)
            try:
                if is_url:
                    # URL 直接通过 Telegram 发送，不走本地文件读取
                    if media_type == 'photo':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                        message = await self._send_with_retry(lambda: self.application.bot.send_photo(chat_id=chat_id, photo=file_path, caption=caption))
                    elif media_type == 'gif':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                        message = await self._send_with_retry(lambda: self.application.bot.send_animation(chat_id=chat_id, animation=file_path, caption=caption))
                    elif media_type == 'video':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
                        message = await self._send_with_retry(lambda: self.application.bot.send_video(chat_id=chat_id, video=file_path, caption=caption))
                    elif media_type in ('audio', 'voice'):
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                        message = await self._send_with_retry(lambda: self.application.bot.send_audio(chat_id=chat_id, audio=file_path, caption=caption))
                    else:
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                        message = await self._send_with_retry(lambda: self.application.bot.send_document(chat_id=chat_id, document=file_path, caption=caption))
                else:
                    if media_type == 'photo':
                        message = await self._send_photo_smart(chat_id, file_path, caption)
                    elif media_type == 'gif':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_animation(
                                chat_id=chat_id, animation=f, caption=caption
                            ))
                    elif media_type == 'video':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_video(
                                chat_id=chat_id, video=f, caption=caption
                            ))
                    elif media_type == 'audio':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_audio(
                                chat_id=chat_id, audio=f, caption=caption
                            ))
                    elif media_type == 'voice':
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_voice(
                                chat_id=chat_id, voice=f, caption=caption
                            ))
                    else:  # document (including code files)
                        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                        with open(file_path, 'rb') as f:
                            message = await self._send_with_retry(lambda: self.application.bot.send_document(
                                chat_id=chat_id, document=f, caption=caption
                            ))
                all_message_ids.append(str(message.message_id))
                logger.info(f"[{chat_id}] 已发送{media_type}: {file_path}")
            except Exception as e:
                logger.error(f"[{chat_id}] 发送{media_type}失败 {file_path}: {e}")
                message = await self._send_with_retry(
                    lambda: self.application.bot.send_message(
                        chat_id=chat_id,
                        text=("⚠️ 无法发送文件: {file_path}\n类型: {media_type}\n错误: {err}\n"
                             "已达到重试上限，建议检查网络或稍后再试。"
                        ).format(file_path=file_path, media_type=media_type, err=e)
                    )
                )
                all_message_ids.append(str(message.message_id))

        # 5. 兜底：如果什么都没发出去，发原始文本
        if not all_message_ids:
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=text or "(empty message)"
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
            html_text = _markdown_to_html(new_text)
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

    async def _notify_debug_exception(self, chat_id: Optional[str], error: Exception):
        """在 debug 模式下将异常详情输出给用户。"""
        if not chat_id:
            return

        controller = getattr(self._message_handler, "__self__", None)
        debug_map = getattr(controller, "debug_mode", {}) if controller else {}
        try:
            is_debug = bool(debug_map.get(chat_id, False))
        except Exception:
            is_debug = False

        if not is_debug:
            return

        tb = traceback.format_exc()
        detail = f"{type(error).__name__}: {error}\n{tb}"
        if len(detail) > 1800:
            detail = detail[:1800] + "\n... (truncated)"

        try:
            await self.send_message(
                chat_id,
                f"🐛 调试：请求处理失败\n```\n{detail}\n```",
                parse_media=False,
            )
        except Exception as send_err:
            logger.debug(f"[{chat_id}] 调试错误信息发送失败: {send_err}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """优雅关闭 Telegram 适配器。"""
        logger.info("[telegram] 正在关闭 Telegram 适配器…")
        try:
            await self.application.stop()
            await self.application.shutdown()
        except Exception as e:
            logger.error(f"[telegram] 关闭时出错: {e}")
        await super().shutdown()

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def health_check(self) -> Dict[str, Any]:
        """返回 Telegram 适配器的健康状态。"""
        base = await super().health_check()
        base["bot_username"] = self.bot_username
        try:
            me = await self.application.bot.get_me()
            base["bot_id"] = me.id
            base["ok"] = True
        except Exception:
            base["ok"] = False
        return base

    async def on_aggregator_complete(self, context: Dict[str, Any]):
        """当消息聚合完成时调用。"""
        if not self._message_handler:
            return

        processed_context = await self.before_message(context)
        try:
            result = await self._message_handler(processed_context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            chat_id = processed_context.get("chat_id")
            logger.error(f"[{chat_id}] 聚合消息处理异常: {e}", exc_info=True)
            if chat_id:
                try:
                    await self.send_message(
                        chat_id,
                        "❌ 抱歉，处理请求时发生错误，请稍后重试。",
                        parse_media=False,
                    )
                except Exception as send_err:
                    logger.debug(f"[{chat_id}] 错误提示发送失败: {send_err}")
            await self._notify_debug_exception(chat_id, e)
            return

        await self.after_message(processed_context, result)
