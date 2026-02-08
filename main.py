import sys
import logging
import warnings
import threading
import asyncio

warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import config
from brain.logging_config import setup_logging as setup_unified_logging, get_chat_logger
from core.controller import NoraController
from platforms.telegram import TelegramAdapter
from tui import TUI

class TuiLogHandler(logging.Handler):
    """Custom handler to forward logs to TUI display."""
    def __init__(self, tui_app):
        super().__init__()
        self.tui_app = tui_app

    def emit(self, record):
        log_entry = self.format(record)
        if self.tui_app._is_running:
            self.tui_app.call_from_thread(self.tui_app.write_log, log_entry)

def setup_logging_with_tui(tui_app):
    """Initialize unified logging + TUI handler."""
    # Setup base logging (console + file)
    setup_unified_logging(console_level=logging.WARNING, file_level=logging.DEBUG)
    
    # Add TUI handler to root logger
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
        
        # Start the bot logic within the existing event loop if necessary, 
        # but run_polling is blocking, so we run it directly here.
        adapter.run(message_handler=controller.handle_new_message)
    except Exception as e:
        logging.critical(f"FATAL ERROR in bot thread: {e}", exc_info=True)
        tui_app.call_from_thread(tui_app.update_status, f"FATAL ERROR. Check nora_core.log.")

class NoraTUI(TUI):
    def __init__(self, tui_ready_event):
        super().__init__()
        self.tui_ready_event = tui_ready_event

    def on_mount(self) -> None:
        super().on_mount()
        self.tui_ready_event.set() # Signal that the TUI is ready

def main():
    """Application entry point with TUI."""
    try:
        config.load_config()
    except FileNotFoundError as e:
        print(f"FATAL: {e}. Please run 'python configure.py' first.")
        sys.exit(1)

    tui_ready_event = threading.Event()
    app = NoraTUI(tui_ready_event)
    setup_logging_with_tui(app)
    
    bot_thread = threading.Thread(target=run_bot_logic, args=(app, tui_ready_event), daemon=True)
    bot_thread.start()
    
    app.run()

if __name__ == '__main__':
    main()
