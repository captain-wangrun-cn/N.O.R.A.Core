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

# 1. Custom Log Handler for TUI
class TuiLogHandler(logging.Handler):
    def __init__(self, tui_app):
        super().__init__()
        self.tui_app = tui_app

    def emit(self, record):
        log_entry = self.format(record)
        # Use call_from_thread to safely update the TUI from another thread
        self.tui_app.call_from_thread(self.tui_app.write_log, log_entry)

def setup_logging(tui_app):
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Keep file handler
    file_handler = logging.FileHandler("nora_core.log")
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    # Create our custom TUI handler
    tui_handler = TuiLogHandler(tui_app)
    tui_handler.setFormatter(log_formatter)
    tui_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    # Clear existing handlers to avoid duplicates if re-run
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(tui_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

def run_bot_logic(tui_app):
    """This function contains the core bot logic and runs in a separate thread."""
    
    # We need a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    logging.info("Initializing N.O.R.A. Core in background thread...")
    
    try:
        adapter = TelegramAdapter()
        # Pass the TUI's update_status method as a callback to the controller
        controller = NoraController(adapter, tui_callback=lambda text: tui_app.call_from_thread(tui_app.update_status, text))
        
        # The adapter's run method will now use the new event loop
        adapter.run(message_handler=controller.handle_new_message)
    except Exception as e:
        logging.error(f"FATAL ERROR in bot thread: {e}", exc_info=True)
    
def main():
    """N.O.R.A. Core application entry point with TUI."""
    try:
        config.load_config()
    except FileNotFoundError as e:
        # We can't log to TUI yet, so print and exit
        print(f"FATAL: {e}. Please run 'python configure.py' first.")
        sys.exit(1)

    # 2. Initialize the TUI App
    app = TUI()
    
    # 3. Setup logging to redirect to the TUI
    setup_logging(app)

    # 4. Start the bot logic in a separate thread
    bot_thread = threading.Thread(target=run_bot_logic, args=(app,), daemon=True)
    bot_thread.start()

    # 5. Run the TUI (this will block the main thread)
    app.run()

if __name__ == '__main__':
    main()
