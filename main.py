import sys
import logging
from telegram.ext import ApplicationBuilder

import config
from core.controller import NoraController
from platforms.telegram import TelegramAdapter

# Set up logging to both file and console
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# File handler
file_handler = logging.FileHandler("nora_core.log")
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

# Get the root logger and add handlers
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logging.getLogger("httpx").setLevel(logging.WARNING)

def main():
    """N.O.R.A. Core 应用入口。"""
    try:
        config.load_config()
    except FileNotFoundError as e:
        logging.error(f"FATAL: {e}. Please run 'python configure.py' first.")
        sys.exit(1)

    logging.info("正在初始化 N.O.R.A. Core...")
    
    adapter = TelegramAdapter()
    controller = NoraController(adapter)
    
    adapter.run(message_handler=controller.handle_new_message)

if __name__ == '__main__':
    main()
