import sys
import logging
import warnings
import threading
import asyncio
import argparse

warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import config
from brain.logging_config import setup_logging as setup_unified_logging, get_chat_logger
from core.controller import NoraController
from adapters.telegram import TelegramAdapter

class TuiLogHandler(logging.Handler):
    """Custom handler to forward logs to TUI display."""

    def __init__(self, tui_app):
        super().__init__()
        self.tui_app = tui_app

    def emit(self, record):
        log_entry = self.format(record)
        if getattr(self.tui_app, "_is_running", False):
            self.tui_app.call_from_thread(self.tui_app.write_log, log_entry)


def setup_logging_with_tui(tui_app):
    """Initialize unified logging + TUI handler."""
    setup_unified_logging(console_level=logging.WARNING, file_level=logging.DEBUG)

    tui_handler = TuiLogHandler(tui_app)
    tui_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    tui_handler.setFormatter(tui_formatter)
    tui_handler.setLevel(logging.INFO)  # TUI shows INFO and above

    root_logger = logging.getLogger()
    root_logger.addHandler(tui_handler)

def run_bot_logic(tui_app, tui_ready_event):
    """Contains the core bot logic, runs in a separate thread."""
    tui_ready_event.wait() # Wait for the TUI to be ready
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    logging.info("TUI is ready. Initializing N.O.R.A. Core in background thread...")
    try:
        adapter = TelegramAdapter()
        controller = NoraController(adapter, tui_callback=lambda text: tui_app.call_from_thread(tui_app.update_status, text))
        
        # 注册 on_ready 钩子：适配器就绪后启动 scheduler
        _original_on_ready = adapter.on_ready
        async def _on_ready_with_scheduler():
            await _original_on_ready()
            controller.start_scheduler()
        adapter.on_ready = _on_ready_with_scheduler
        
        # Start the bot logic within the existing event loop if necessary, 
        # but run_polling is blocking, so we run it directly here.
        adapter.run(message_handler=controller.handle_new_message)
    except Exception as e:
        logging.critical(f"FATAL ERROR in bot thread: {e}", exc_info=True)
        tui_app.call_from_thread(tui_app.update_status, f"FATAL ERROR. Check nora_core.log.")

class NoraTUI:
    """Placeholder for type hints; real class is loaded lazily when TUI is enabled."""


def run_with_tui():
    """Application entry point with TUI."""
    try:
        from tui import TUI as TextualTUI  # Lazy import to avoid dependency in headless mode
    except ImportError as exc:
        print(f"FATAL: 无法加载 TUI (textual) 依赖: {exc}. 请安装后重试，或使用 --no-tui 模式。")
        sys.exit(1)

    class NoraTUI(TextualTUI):
        def __init__(self, tui_ready_event):
            super().__init__()
            self.tui_ready_event = tui_ready_event

        def on_mount(self) -> None:
            super().on_mount()
            self.tui_ready_event.set()  # Signal that the TUI is ready

    try:
        config.load_config()
    except FileNotFoundError as e:
        print(f"FATAL: {e}. Please run 'python cli.py' first.")
        sys.exit(1)

    tui_ready_event = threading.Event()
    app = NoraTUI(tui_ready_event)
    setup_logging_with_tui(app)

    bot_thread = threading.Thread(target=run_bot_logic, args=(app, tui_ready_event), daemon=True)
    bot_thread.start()

    app.run()


def run_headless(console_level=logging.INFO):
    """Run bot without TUI (pure command-line)."""
    try:
        config.load_config()
    except FileNotFoundError as e:
        print(f"FATAL: {e}. Please run 'python configure.py' first.")
        sys.exit(1)

    # Console logging for headless mode
    setup_unified_logging(console_level=console_level, file_level=logging.DEBUG)
    logging.info("启动 N.O.R.A. Core (无 TUI 模式)...")

    def headless_status_callback(text: str):
        """在无 TUI 模式下输出原本 TUI 底部状态。"""
        logging.info(f"[STATUS] {text}")

    try:
        adapter = TelegramAdapter()
        controller = NoraController(adapter, tui_callback=headless_status_callback)
        
        # 注册 on_ready 钩子：适配器就绪后启动 scheduler
        _original_on_ready = adapter.on_ready
        async def _on_ready_with_scheduler():
            await _original_on_ready()
            controller.start_scheduler()
        adapter.on_ready = _on_ready_with_scheduler
        
        adapter.run(message_handler=controller.handle_new_message)
    except Exception as exc:
        logging.critical(f"启动失败: {exc}", exc_info=True)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="N.O.R.A. Core Launcher")
    parser.add_argument("--no-tui", "--headless", action="store_true", help="禁用 TUI，纯命令行运行")
    parser.add_argument(
        "--console-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="无 TUI 模式下的控制台日志级别",
    )

    args = parser.parse_args()

    if args.no_tui:
        level = getattr(logging, args.console_level.upper(), logging.INFO)
        run_headless(console_level=level)
    else:
        run_with_tui()

if __name__ == '__main__':
    main()
