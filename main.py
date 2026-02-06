import sys
import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

import config
from core.controller import NoraController

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING) # Reduce httpx noise

def main():
    """Entry point for the N.O.R.A. Core application."""
    if not config.TELEGRAM_BOT_TOKEN:
        logging.error("FATAL: TELEGRAM_BOT_TOKEN not found. Please check your .env file.")
        sys.exit(1)
        
    logging.info("Initializing N.O.R.A. Core...")
    
    # Initialize the main controller
    controller = NoraController()
    
    # Build the Telegram application
    application = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Register command and message handlers
    application.add_handler(CommandHandler('start', controller.start))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), controller.handle_message))
    
    logging.info("Application configured. Starting polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
