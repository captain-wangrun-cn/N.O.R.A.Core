import sys
import logging
import warnings
import threading
import asyncio
import argparse

warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import config
from adapters.config_utils import is_placeholder_value, load_adapter_config, read_adapter_config
from brain.logging_config import setup_logging as setup_unified_logging, get_chat_logger
from core.controller import NoraController

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
    setup_unified_logging(console_level=logging.INFO, file_level=logging.INFO)

    tui_handler = TuiLogHandler(tui_app)
    tui_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    tui_handler.setFormatter(tui_formatter)
    tui_handler.setLevel(logging.INFO)  # TUI shows INFO and above

    root_logger = logging.getLogger()
    root_logger.addHandler(tui_handler)

AUTO_ADAPTER_ORDER = ("onebotv11", "telegram")


def _telegram_configured() -> bool:
    cfg = read_adapter_config("telegram")
    if not cfg:
        return False
    return not is_placeholder_value(cfg.get("bot_token"))


def _onebotv11_configured() -> bool:
    try:
        from adapters.onebotv11.main import DEFAULT_CONFIG
    except Exception:
        DEFAULT_CONFIG = {}
    raw_cfg = read_adapter_config("onebotv11")
    if not raw_cfg:
        return False
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw_cfg)
    connection_type = str(cfg.get("connection_type") or "websocket").strip().lower()
    if connection_type in {"reverse", "reverse_websocket", "ws_reverse"}:
        return bool(str(cfg.get("reverse_host") or "").strip() and str(cfg.get("reverse_port") or "").strip())
    return not is_placeholder_value(cfg.get("websocket_url"))


def resolve_adapter_names(adapter_name: str = "auto") -> list[str]:
    """解析 --adapter 参数为一个或多个适配器名。

    支持：``auto``（所有已配置平台）、``all``、逗号分隔多值、单个名。
    """
    normalized = (adapter_name or "auto").strip().lower()

    def _canonical(name: str) -> str:
        n = name.strip().lower()
        if n in {"onebot", "onebot-11", "qq"}:
            return "onebotv11"
        return n

    if normalized in {"", "auto", "all"}:
        configured: list[str] = []
        if _onebotv11_configured():
            configured.append("onebotv11")
        if _telegram_configured():
            configured.append("telegram")
        if configured:
            return configured
        raise ValueError(
            "No configured adapter found. Please edit one adapter config.json, "
            "or start with --adapter telegram / --adapter onebotv11 to configure explicitly."
        )

    names = [_canonical(part) for part in normalized.split(",") if part.strip()]
    # 去重且保序
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    if not result:
        raise ValueError(f"Unsupported adapter: {adapter_name}")
    return result


def resolve_adapter_name(adapter_name: str = "auto") -> str:
    """单适配器兼容入口：返回解析后的第一个适配器名。"""
    return resolve_adapter_names(adapter_name)[0]


def _instantiate_adapter(normalized: str):
    if normalized == "telegram":
        from adapters.telegram import TelegramAdapter

        return TelegramAdapter()
    if normalized == "onebotv11":
        from adapters.onebotv11 import OneBotV11Adapter

        return OneBotV11Adapter()
    raise ValueError(f"Unsupported adapter: {normalized}")


def create_adapter(adapter_name: str = "auto"):
    return _instantiate_adapter(resolve_adapter_name(adapter_name))


def create_adapters(adapter_name: str = "auto") -> list:
    """创建一个或多个适配器实例（多平台同时在线）。"""
    return [_instantiate_adapter(n) for n in resolve_adapter_names(adapter_name)]


async def _run_adapters_async(controller, adapters: list):
    """在单个事件循环中并发运行所有适配器，on_ready 服务只启动一次。"""
    services_started = {"done": False}
    services_lock = asyncio.Lock()

    async def _maybe_start_services():
        async with services_lock:
            if services_started["done"]:
                return
            services_started["done"] = True
            controller.start_scheduler()
            await controller.start_triggers()

    # 每个 adapter 就绪后尝试启动一次公共服务（scheduler/triggers）。
    for adapter in adapters:
        _original_on_ready = adapter.on_ready

        def _make_hook(orig):
            async def _on_ready_with_services():
                await orig()
                await _maybe_start_services()
            return _on_ready_with_services

        adapter.on_ready = _make_hook(_original_on_ready)

    await asyncio.gather(
        *[adapter.run_async(controller.handle_new_message) for adapter in adapters]
    )


def run_bot_logic(tui_app, tui_ready_event, adapter_name: str = "auto"):
    """Contains the core bot logic, runs in a separate thread."""
    tui_ready_event.wait() # Wait for the TUI to be ready

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    logging.info("TUI is ready. Initializing N.O.R.A. Core in background thread...")
    try:
        adapters = create_adapters(adapter_name)
        logging.info("使用平台适配器: %s", ", ".join(a.platform_name for a in adapters))
        controller = NoraController(adapters, tui_callback=lambda text: tui_app.call_from_thread(tui_app.update_status, text))

        loop.run_until_complete(_run_adapters_async(controller, adapters))
    except Exception as e:
        logging.critical(f"FATAL ERROR in bot thread: {e}", exc_info=True)
        tui_app.call_from_thread(tui_app.update_status, f"FATAL ERROR. Check nora_core.log.")

class NoraTUI:
    """Placeholder for type hints; real class is loaded lazily when TUI is enabled."""


def run_with_tui(adapter_name: str = "auto"):
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

    bot_thread = threading.Thread(target=run_bot_logic, args=(app, tui_ready_event, adapter_name), daemon=True)
    bot_thread.start()

    app.run()


def run_headless(console_level=logging.INFO, adapter_name: str = "auto"):
    """Run bot without TUI (pure command-line)."""
    try:
        config.load_config()
    except FileNotFoundError as e:
        print(f"FATAL: {e}. Please run 'python configure.py' first.")
        sys.exit(1)

    # Console logging for headless mode
    setup_unified_logging(console_level=console_level, file_level=logging.INFO)
    logging.info("启动 N.O.R.A. Core (无 TUI 模式)...")

    def headless_status_callback(text: str):
        """在无 TUI 模式下输出原本 TUI 底部状态。"""
        logging.info(f"[STATUS] {text}")

    try:
        adapters = create_adapters(adapter_name)
        logging.info("使用平台适配器: %s", ", ".join(a.platform_name for a in adapters))
        controller = NoraController(adapters, tui_callback=headless_status_callback)

        asyncio.run(_run_adapters_async(controller, adapters))
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
    parser.add_argument(
        "--adapter",
        default="auto",
        help=(
            "选择平台适配器；auto 会使用所有已配置平台，"
            "可逗号分隔多选（如 'telegram,onebotv11'），或 'all'"
        ),
    )

    args = parser.parse_args()

    if args.no_tui:
        level = getattr(logging, args.console_level.upper(), logging.INFO)
        run_headless(console_level=level, adapter_name=args.adapter)
    else:
        run_with_tui(adapter_name=args.adapter)

if __name__ == '__main__':
    main()
