import logging
import logging.handlers
import os
import sys
from datetime import datetime

# Log directories
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Log file paths
MAIN_LOG = os.path.join(LOG_DIR, "nora.log")
ERROR_LOG = os.path.join(LOG_DIR, "error.log")

# Custom formatter with color support for console
class ColoredFormatter(logging.Formatter):
    """Adds color to console logs based on level."""
    
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record):
        # Ensure chat_id exists (fallback to 'SYSTEM')
        if not hasattr(record, 'chat_id'):
            record.chat_id = 'SYSTEM'
        
        if hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
            levelname = record.levelname
            if levelname in self.COLORS:
                record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"
        return super().format(record)


def setup_logging(console_level=logging.WARNING, file_level=logging.DEBUG):
    """
    Setup centralized logging with console and file handlers.
    
    Args:
        console_level: Minimum level for console output (default: WARNING)
        file_level: Minimum level for file output (default: DEBUG)
    """
    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture everything, filter at handler level
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # --- Console Handler (WARNING and above by default) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_formatter = ColoredFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - [%(chat_id)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # --- Main Log File (DEBUG and above, rotating) ---
    # RotatingFileHandler: max 10MB per file, keep 5 backups
    main_file_handler = logging.handlers.RotatingFileHandler(
        MAIN_LOG,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    main_file_handler.setLevel(file_level)
    file_formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - [%(chat_id)s] %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    main_file_handler.setFormatter(file_formatter)
    root_logger.addHandler(main_file_handler)
    
    # --- Error Log File (ERROR and above only) ---
    error_file_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8'
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(file_formatter)
    root_logger.addHandler(error_file_handler)
    
    # --- Module-specific log levels ---
    # Controller: INFO (important flow, not too noisy)
    logging.getLogger('core.controller').setLevel(logging.INFO)
    
    # Tools: DEBUG (detailed tool execution)
    logging.getLogger('brain.tools').setLevel(logging.DEBUG)
    
    # LLM: WARNING (only show issues, not every token)
    logging.getLogger('brain.llm').setLevel(logging.WARNING)
    logging.getLogger('brain.providers').setLevel(logging.WARNING)
    
    # RAG: INFO (memory operations are interesting but not spammy)
    logging.getLogger('memory').setLevel(logging.INFO)
    
    # Platforms: INFO (message flow)
    logging.getLogger('platforms').setLevel(logging.INFO)
    
    # Suppress noisy third-party libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    
    # Log startup message
    root_logger.info(
        "Logging initialized",
        extra={'chat_id': 'SYSTEM'}
    )
    root_logger.info(
        f"Console: {logging.getLevelName(console_level)}+, File: {logging.getLevelName(file_level)}+",
        extra={'chat_id': 'SYSTEM'}
    )
    root_logger.info(
        f"Logs: {MAIN_LOG} (main), {ERROR_LOG} (errors only)",
        extra={'chat_id': 'SYSTEM'}
    )


# Utility: Context-aware logger factory
class ChatLoggerAdapter(logging.LoggerAdapter):
    """
    Automatically inject chat_id into log records.
    Usage: logger = get_chat_logger(__name__, chat_id)
    """
    def process(self, msg, kwargs):
        # Ensure 'extra' dict exists and has chat_id
        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        if 'chat_id' not in kwargs['extra']:
            # self.extra is guaranteed to be a dict when using this adapter
            default_chat_id = self.extra['chat_id'] if isinstance(self.extra, dict) else 'N/A'
            kwargs['extra']['chat_id'] = default_chat_id
        return msg, kwargs


def get_chat_logger(name: str, chat_id: str = 'N/A'):
    """
    Get a logger with automatic chat_id injection.
    
    Args:
        name: Logger name (usually __name__)
        chat_id: Chat/session identifier
    
    Returns:
        ChatLoggerAdapter: Logger that auto-includes chat_id in all logs
    """
    base_logger = logging.getLogger(name)
    return ChatLoggerAdapter(base_logger, {'chat_id': chat_id})
