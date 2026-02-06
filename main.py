import sys
import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

import config
from core.controller import NoraController
from platforms.telegram import TelegramAdapter

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

def main():
    """N.O.R.A. Core 应用入口。"""
    try:
        config.load_config()
    except FileNotFoundError as e:
        logging.error(f"FATAL: {e}. Please run 'python configure.py' first.")
        sys.exit(1)

    logging.info("正在初始化 N.O.R.A. Core...")
    
    # 1. 初始化平台适配器 (目前只有 Telegram)
    adapter = TelegramAdapter()
    
    # 2. 初始化核心控制器，并把适配器注入进去
    controller = NoraController(adapter)
    
    # 3. 启动适配器，并将消息处理权交给控制器
    # 这是一个阻塞操作，会一直运行直到程序退出
    adapter.run(message_handler=controller.handle_new_message)

if __name__ == '__main__':
    main()
