# N.O.R.A. Core - Configuration Loader
import yaml
import os

CONFIG_FILE = "config.yml"
_config = None

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
    return get_config().get("telegram", {}).get("bot_token")

def get_llm_provider():
    return get_config().get("llm", {}).get("provider", "gemini")

def get_api_key(provider=None):
    provider = provider or get_llm_provider()
    return get_config().get("llm", {}).get("api_keys", {}).get(provider)

def get_model_name(model_alias="smart"):
    """Gets the model name for a given alias (e.g., 'smart', 'fast')."""
    return get_config().get("llm", {}).get("models", {}).get(model_alias)

