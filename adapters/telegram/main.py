from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import Any, Callable, Dict, Optional

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from adapters.aggregator import MessageAggregator
from adapters.base import BaseAdapter, PlatformFeatures
from adapters.config_utils import adapter_config_path, load_adapter_config
from memory.message_history import MessageHistory
from workspace_config import get_workspace_manager

from .callbacks import TelegramCallbacksMixin
from .cleanup import TelegramCleanupMixin
from .commands import TelegramCommandsMixin
from .constants import FILE_PATTERN, MEDIA_TYPES, TG_MSG_MAX_LENGTH
from .group_context import TelegramGroupContextMixin
from .incoming import TelegramIncomingMixin
from .reply import TelegramReplyMixin
from .sender import TelegramSenderMixin
from .tools import TelegramToolsMixin

logger = logging.getLogger(__name__)


class TelegramAdapter(
    TelegramCommandsMixin,
    TelegramGroupContextMixin,
    TelegramIncomingMixin,
    TelegramCallbacksMixin,
    TelegramCleanupMixin,
    TelegramReplyMixin,
    TelegramSenderMixin,
    TelegramToolsMixin,
    BaseAdapter,
):
    """Telegram 平台适配器。"""

    DEBUG_CLEANUP_COMMAND = "/debug_cleanup"
    MODEL_COMMAND = "/model"
    NORA_PREFS_COMMAND = "/nora_prefs"
    MEDIA_TYPES = MEDIA_TYPES
    FILE_PATTERN = FILE_PATTERN
    _cleanup_confirm_tokens: Dict[str, float] = {}
    _cleanup_lock = asyncio.Lock()

    @property
    def platform_name(self) -> str:
        return "telegram"

    @property
    def platform_features(self) -> PlatformFeatures:
        return PlatformFeatures(
            supports_edit_message=True,
            supports_delete_message=True,
            supports_reactions=False,
            supports_typing_indicator=True,
            supports_rich_media=True,
            supports_reply=True,
            supports_threads=False,
            supports_buttons=True,
            supports_chat_member_lookup=True,
            supports_chat_moderation=True,
            supports_member_tags=True,
            max_message_length=TG_MSG_MAX_LENGTH,
        )

    def __init__(self):
        super().__init__()
        adapter_config = load_adapter_config(
            "telegram",
            {"bot_token": "YOUR_TELEGRAM_BOT_TOKEN"},
        )
        token = str(adapter_config.get("bot_token") or "").strip()
        if not token:
            raise ValueError(
                f"Telegram bot_token not found. Please edit {adapter_config_path('telegram')}."
            )
        if token == "YOUR_TELEGRAM_BOT_TOKEN":
            raise ValueError(
                f"Telegram bot_token is still the placeholder. Please edit {adapter_config_path('telegram')}."
            )

        workspace_manager = get_workspace_manager()
        self.workspace_root = str(workspace_manager.root)
        self.downloads_dir = str(workspace_manager.downloads_dir)
        self.data_dir = str(workspace_manager.data_dir)
        self.telegram_data_dir = os.path.join(self.data_dir, "telegram")
        os.makedirs(self.telegram_data_dir, exist_ok=True)

        self.application = ApplicationBuilder().token(token).build()
        self._aggregator: MessageAggregator | None = None
        self.message_history = MessageHistory()
        self._reply_contexts: Dict[str, Dict] = {}
        self.bot_username: Optional[str] = None
        self._media_group_buffers: Dict[str, Dict[str, Any]] = {}
        self._media_group_timers: Dict[str, asyncio.Task] = {}
        self._model_option_tokens: Dict[str, Dict[str, Any]] = {}

    def run(self, message_handler: Callable[[Dict[str, Any]], Any]):
        self._message_handler = message_handler
        import config

        app_config = config.get_config()
        interaction_config = app_config.get("interaction", {}) if app_config else {}
        buffer_timeout = interaction_config.get("buffer_timeout", 3.0)

        self._aggregator = MessageAggregator(
            timeout=buffer_timeout,
            on_complete=self.on_aggregator_complete,
        )

        self.application.add_handler(CommandHandler('start', self._start_command))
        self.application.add_handler(CommandHandler('stop', self._stop_command))
        self.application.add_handler(CommandHandler('clear', self._clear_command))
        self.application.add_handler(CommandHandler('status', self._status_command))
        self.application.add_handler(CommandHandler('debug', self._debug_command))
        self.application.add_handler(CommandHandler('regenerate_proactive', self._regenerate_proactive_command))
        self.application.add_handler(CommandHandler('schedule_today', self._schedule_today_command))
        self.application.add_handler(CommandHandler('custom_scope', self._custom_scope_command))
        self.application.add_handler(CommandHandler('nora_prefs', self._nora_prefs_command))
        self.application.add_handler(CommandHandler('set_stream', self._set_stream_command))
        self.application.add_handler(CommandHandler('nonstream', self._set_stream_command))
        self.application.add_handler(CommandHandler('context', self._context_command))
        self.application.add_handler(CommandHandler('debug_cleanup', self._debug_cleanup_command))
        self.application.add_handler(CommandHandler('model', self._model_command))
        self.application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self._handle_incoming_message))
        self.application.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self.application.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, self._handle_video))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self._handle_document))
        self.application.add_handler(MessageHandler(filters.Sticker.ALL, self._handle_sticker))
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))

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
        await self.on_ready()

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
                f"调试：请求处理失败\n```\n{detail}\n```",
                parse_media=False,
            )
        except Exception as send_err:
            logger.debug(f"[{chat_id}] 调试错误信息发送失败: {send_err}")

    async def shutdown(self) -> None:
        """优雅关闭 Telegram 适配器。"""
        logger.info("[telegram] 正在关闭 Telegram 适配器...")
        try:
            await self.application.stop()
            await self.application.shutdown()
        except Exception as e:
            logger.error(f"[telegram] 关闭时出错: {e}")
        await super().shutdown()

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
                        "抱歉，处理请求时发生错误，请稍后重试。",
                        parse_media=False,
                    )
                except Exception as send_err:
                    logger.debug(f"[{chat_id}] 错误提示发送失败: {send_err}")
            await self._notify_debug_exception(chat_id, e)
            return

        await self.after_message(processed_context, result)
