import os
import sys
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# Load environment
load_dotenv()

# Configuration
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="N.O.R.A. Core (Echo) is online. System initialized."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message loop."""
    user_text = update.message.text
    chat_id = update.effective_chat.id
    
    # Placeholder for Brain logic
    response_text = f"Echo received: {user_text}\n(Brain module not yet connected)"
    
    await context.bot.send_message(chat_id=chat_id, text=response_text)

if __name__ == '__main__':
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env")
        sys.exit(1)
        
    application = ApplicationBuilder().token(TOKEN).build()
    
    start_handler = CommandHandler('start', start)
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    
    application.add_handler(start_handler)
    application.add_handler(msg_handler)
    
    print("N.O.R.A. Core is polling...")
    application.run_polling()
