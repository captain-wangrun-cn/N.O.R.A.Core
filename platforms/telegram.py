from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from telegram.constants import ChatAction
from typing import Callable, Any

from platforms.base import BaseAdapter
from platforms.aggregator import MessageAggregator
import config

class TelegramAdapter(BaseAdapter):
    """Telegram 平台适配器。"""

    def __init__(self):
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
        self.application.run_polling()

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if self._message_handler:
            # Propagate start command as a special message
            await self._message_handler(chat_id, "/start")

    async def _handle_incoming_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        text = update.message.text
        if self._aggregator:
            await self._aggregator.add_message(chat_id, text)

    async def send_message(self, chat_id: str, text: str) -> str:
        message = await self.application.bot.send_message(chat_id=chat_id, text=text)
        return str(message.message_id)

    async def start_typing(self, chat_id: str):
        await self.application.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    async def stop_typing(self, chat_id: str):
        # Telegram doesn't have a "stop typing" action, it's implicit.
        pass
