# N.O.R.A. Core - Configuration Loader
import yaml
import os

CONFIG_FILE = "config.yml"
_config = None


def _safe_config():
    """Return loaded config dict or empty dict as fallback."""
    cfg = get_config()
    return cfg if cfg is not None else {}

def load_config():
    """Loads the YAML configuration file."""
    global _config
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(
            f"{CONFIG_FILE} not found. "
            "Please run 'python configure.py' to generate it."
        )
    with open(CONFIG_FILE, 'r') as f:
        _config = yaml.safe_load(f)

def get_config():
    """Returns the loaded configuration dictionary."""
    if _config is None:
        load_config()
    return _config

# --- Helper accessors ---
def get_telegram_token():
    cfg = _safe_config()
    return cfg.get("telegram", {}).get("bot_token")

def get_llm_provider():
    cfg = _safe_config()
    return cfg.get("llm", {}).get("provider", "gemini")

def get_api_key(provider=None):
    provider = provider or get_llm_provider()
    cfg = _safe_config()
    return cfg.get("llm", {}).get("api_keys", {}).get(provider)

def get_base_url():
    cfg = _safe_config()
    return cfg.get("llm", {}).get("base_url")

def get_model_name(model_alias="smart"):
    """Gets the model name for a given alias (e.g., 'smart', 'fast')."""
    cfg = _safe_config()
    return cfg.get("llm", {}).get("models", {}).get(model_alias)

def get_llm_user_agent():
    """Optional custom User-Agent for LLM HTTP requests."""
    cfg = _safe_config()
    return cfg.get("llm", {}).get("user_agent")

def get_message_history_config():
    """Gets the message history configuration."""
    cfg = _safe_config()
    return cfg.get("memory", {}).get("message_history", {})

def get_workspace_config():
    """Gets the workspace configuration."""
    cfg = _safe_config()
    return cfg.get("workspace", {})

