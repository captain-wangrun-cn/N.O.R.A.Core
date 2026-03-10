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

def save_config(config_data: dict):
    """Save config dict to CONFIG_FILE and update cache."""
    global _config
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)
    _config = config_data

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


def get_provider_config(provider_name=None):
    """获取指定提供商配置（兼容新旧两种配置结构）。"""
    cfg = _safe_config()
    llm_cfg = cfg.get("llm", {})

    providers_cfg = llm_cfg.get("providers", {}) or {}
    if provider_name and provider_name in providers_cfg:
        return providers_cfg[provider_name] or {}

    # 旧配置兼容: llm.api_keys + llm.base_url + llm.user_agent
    provider = provider_name or get_llm_provider()
    api_keys = llm_cfg.get("api_keys", {}) or {}
    legacy = {
        "type": provider,
        "api_key": api_keys.get(provider, ""),
    }
    if provider == "openai":
        if llm_cfg.get("base_url"):
            legacy["base_url"] = llm_cfg.get("base_url")
        if llm_cfg.get("user_agent"):
            legacy["user_agent"] = llm_cfg.get("user_agent")
    return legacy


def get_provider_type(provider_name=None):
    """获取提供商类型（如 gemini/openai）。"""
    provider = provider_name or get_llm_provider()
    provider_cfg = get_provider_config(provider)
    return provider_cfg.get("type", provider)


def get_model_provider(model_alias="smart"):
    """获取某个模型别名绑定的提供商名称。"""
    cfg = _safe_config()
    llm_cfg = cfg.get("llm", {})
    return llm_cfg.get("model_providers", {}).get(model_alias) or llm_cfg.get("provider", "gemini")

def get_api_key(provider=None):
    provider = provider or get_llm_provider()
    provider_cfg = get_provider_config(provider)
    api_key = provider_cfg.get("api_key")
    if api_key is not None:
        return api_key

    # 旧配置兜底
    cfg = _safe_config()
    return cfg.get("llm", {}).get("api_keys", {}).get(provider)

def get_base_url(provider=None):
    provider = provider or get_llm_provider()
    provider_cfg = get_provider_config(provider)
    if provider_cfg.get("base_url") is not None:
        return provider_cfg.get("base_url")

    cfg = _safe_config()
    return cfg.get("llm", {}).get("base_url")

def get_model_name(model_alias="smart"):
    """Gets the model name for a given alias (e.g., 'smart', 'fast')."""
    cfg = _safe_config()
    return cfg.get("llm", {}).get("models", {}).get(model_alias)

def get_llm_user_agent(provider=None):
    """Optional custom User-Agent for LLM HTTP requests."""
    provider = provider or get_llm_provider()
    provider_cfg = get_provider_config(provider)
    if provider_cfg.get("user_agent") is not None:
        return provider_cfg.get("user_agent")

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

