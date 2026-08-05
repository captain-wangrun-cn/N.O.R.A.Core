import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, Optional


def _resolve_display_timezone():
    """日志时间戳所用时区。

    与调度器/消息时间戳共用 memory.message_history.timezone，避免出现
    「日志写 02:37、调度器写 14:37 CST」这种同一事件两套时间的读日志陷阱。
    返回 None 表示回退系统本地时区。
    """
    try:
        import config
        from zoneinfo import ZoneInfo

        tz_name = ((config.get_message_history_config() or {}).get("timezone") or "").strip()
        if not tz_name:
            return None
        return ZoneInfo(tz_name)
    except Exception:
        return None


class _DisplayTimezoneMixin:
    """让 Formatter 的 %(asctime)s 走配置时区而不是系统本地时区。"""

    display_tz = None

    def converter(self, timestamp):  # type: ignore[override]
        if self.display_tz is not None:
            return datetime.fromtimestamp(timestamp, tz=self.display_tz).timetuple()
        return datetime.fromtimestamp(timestamp).timetuple()


# Custom formatter with color support for console
class ColoredFormatter(_DisplayTimezoneMixin, logging.Formatter):
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


class SafeFormatter(_DisplayTimezoneMixin, logging.Formatter):
    """Formatter that safely handles missing chat_id attribute."""

    def format(self, record):
        # Ensure chat_id exists (fallback to 'SYSTEM')
        if not hasattr(record, 'chat_id'):
            record.chat_id = 'SYSTEM'
        return super().format(record)


def _get_logging_config() -> Dict:
    try:
        import config

        return (config.get_logging_config() or {})
    except Exception:
        return {}


def _resolve_log_dir(override: Optional[str] = None) -> str:
    """Resolve log directory (workspace/logs by default)."""
    try:
        from workspace_config import get_workspace_manager

        workspace_dir = get_workspace_manager().logs_dir
    except Exception:
        workspace_dir = os.path.abspath(os.path.join(os.getcwd(), "logs"))

    log_dir = override or workspace_dir
    log_dir = os.path.abspath(os.path.expanduser(log_dir))
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _cleanup_old_logs(log_dir: str, retention_days: int, prefixes: Optional[list[str]] = None):
    """Remove log files older than retention_days based on modified time."""
    if retention_days is None or retention_days <= 0:
        return

    prefixes = prefixes or ["nora.log", "nora-error.log", "nora", "error"]
    cutoff = datetime.now() - timedelta(days=retention_days)

    for name in os.listdir(log_dir):
        if name in {"nora.log", "nora-error.log"}:
            # 基础文件保留，由 TimedRotatingFileHandler 负责轮转
            continue
        if not any(name.startswith(p) for p in prefixes):
            continue
        path = os.path.join(log_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if mtime < cutoff:
                os.remove(path)
        except Exception:
            # 清理失败不应中断主流程
            continue


def setup_logging(console_level=logging.INFO, file_level=logging.INFO):
    """
    Setup centralized logging with console and file handlers.
    Logs rotate daily and old files are cleaned automatically.

    Args:
        console_level: Minimum level for console output (default: INFO)
        file_level: Minimum level for file output (default: INFO)
    """
    logging_cfg = _get_logging_config()
    retention_days = logging_cfg.get("retention_days", 14)
    log_dir_override = logging_cfg.get("dir") or logging_cfg.get("path")

    log_dir = _resolve_log_dir(log_dir_override)
    main_log_path = os.path.join(log_dir, "nora.log")
    error_log_path = os.path.join(log_dir, "nora-error.log")
    # 日志时间戳统一走配置时区，与调度器/消息时间戳对齐
    display_tz = _resolve_display_timezone()
    ColoredFormatter.display_tz = display_tz
    SafeFormatter.display_tz = display_tz
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
    main_file_handler = logging.handlers.TimedRotatingFileHandler(
        main_log_path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding='utf-8',
    )
    main_file_handler.suffix = "%Y-%m-%d"
    main_file_handler.setLevel(file_level)
    file_formatter = SafeFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - [%(chat_id)s] %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    main_file_handler.setFormatter(file_formatter)
    root_logger.addHandler(main_file_handler)
    
    # --- Error Log File (ERROR and above only) ---
    error_file_handler = logging.handlers.TimedRotatingFileHandler(
        error_log_path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding='utf-8',
    )
    error_file_handler.suffix = "%Y-%m-%d"
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
    logging.getLogger('adapters').setLevel(logging.INFO)
    
    # Suppress noisy third-party libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    # APScheduler executor 对每次 job 触发都打两条 INFO。群监听 tick 是每分钟一次的
    # cron job，会把日志刷成"Running job / executed successfully"的流水账。
    # 真正需要关注的调度信息由 core/scheduler.py 自己打点。
    logging.getLogger('apscheduler.executors').setLevel(logging.WARNING)
    logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
    
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
        f"Logs: {main_log_path} (daily rotate), {error_log_path} (errors)",
        extra={'chat_id': 'SYSTEM'}
    )

    # 清理过期日志
    _cleanup_old_logs(log_dir, retention_days)


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
