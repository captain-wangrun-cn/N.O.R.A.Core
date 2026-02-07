import sys
import logging
import warnings
import threading
import asyncio

warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import config
from core.controller import NoraController
from platforms.telegram import TelegramAdapter
from tui import TUI

class TuiLogHandler(logging.Handler):
    def __init__(self, tui_app):
        super().__init__()
        self.tui_app = tui_app

    def emit(self, record):
        log_entry = self.format(record)
        if self.tui_app._is_running:
            self.tui_app.call_from_thread(self.tui_app.write_log, log_entry)

def setup_logging(tui_app):
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler("nora_core.log")
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)
    tui_handler = TuiLogHandler(tui_app)
    tui_handler.setFormatter(log_formatter)
    tui_handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(tui_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    setup_logging(app)
    
    bot_thread = threading.Thread(target=run_bot_logic, args=(app, tui_ready_event), daemon=True)
    bot_thread.start()
    
    app.run()

if __name__ == '__main__':
    main()
