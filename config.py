# N.O.R.A. Core - Configuration Loader
import yaml
import os
import logging

CONFIG_FILE = "config.yml"
TRIGGERS_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "triggers", "config.yml")
_config = None
logger = logging.getLogger(__name__)


def _clear_proxy_env():
    """清理进程级代理环境变量，避免旧配置残留。"""
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        os.environ.pop(key, None)


def _apply_proxy_env():
    """根据配置应用全局代理到进程环境变量。"""
    proxy_cfg = get_proxy_config()
    enabled = bool(proxy_cfg.get("enabled", False))

    # 每次加载都先清理，确保切换配置时行为可预期
    _clear_proxy_env()

    if not enabled:
        return

    http_proxy = proxy_cfg.get("http")
    https_proxy = proxy_cfg.get("https")
    all_proxy = proxy_cfg.get("all")
    no_proxy = proxy_cfg.get("no_proxy")

    # 单一 URL 快捷配置：未分协议时同时作为 http/https/all
    url = proxy_cfg.get("url")
    if url:
        http_proxy = http_proxy or url
        https_proxy = https_proxy or url
        all_proxy = all_proxy or url

    if http_proxy:
        os.environ["HTTP_PROXY"] = str(http_proxy)
        os.environ["http_proxy"] = str(http_proxy)
    if https_proxy:
        os.environ["HTTPS_PROXY"] = str(https_proxy)
        os.environ["https_proxy"] = str(https_proxy)
    if all_proxy:
        os.environ["ALL_PROXY"] = str(all_proxy)
        os.environ["all_proxy"] = str(all_proxy)
    if no_proxy:
        os.environ["NO_PROXY"] = str(no_proxy)
        os.environ["no_proxy"] = str(no_proxy)

    logger.info("已应用全局代理配置到环境变量")


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
        _config = yaml.safe_load(f) or {}
    _apply_proxy_env()

def save_config(config_data: dict):
    """Save config dict to CONFIG_FILE and update cache."""
    global _config
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        yaml.safe_dump(config_data, f, allow_unicode=True, sort_keys=False)
    _config = config_data
    _apply_proxy_env()


def set_custom_injection_scopes(scopes):
    """标准化并持久化 CUSTOM.md 注入范围配置。"""
    cfg = dict(_safe_config())
    normalized = []
    for scope in scopes or []:
        value = str(scope).strip().lower()
        if value:
            normalized.append(value)

    # 去重并保持顺序
    normalized = list(dict.fromkeys(normalized))

    if any(s in ("none", "off") for s in normalized):
        normalized = ["none"]

    custom_injection = dict((cfg.get("custom_injection", {}) or {}))
    custom_injection["scopes"] = normalized
    cfg["custom_injection"] = custom_injection
    save_config(cfg)
    return normalized

def get_config():
    """Returns the loaded configuration dictionary."""
    if _config is None:
        load_config()
    return _config


def get_proxy_config():
    """获取全局代理配置（兼容新旧结构）。"""
    cfg = _safe_config()

    # 新结构：network.proxy
    network_proxy = (cfg.get("network", {}) or {}).get("proxy", {}) or {}
    if network_proxy:
        return network_proxy

    # 旧结构兼容：proxy
    legacy_proxy = cfg.get("proxy", {}) or {}
    return legacy_proxy

# --- Helper accessors ---
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


def get_provider_option(provider=None, key: str = ""):
    """获取提供商自定义配置项。"""
    if not key:
        return None
    provider = provider or get_llm_provider()
    provider_cfg = get_provider_config(provider)
    return provider_cfg.get(key)

def get_model_name(model_alias="smart"):
    """Gets the model name for a given alias (e.g., 'smart', 'fast')."""
    cfg = _safe_config()
    return cfg.get("llm", {}).get("models", {}).get(model_alias)


# 生图接口形态（仅 openai 类型 provider 需要区分；gemini 只有一条 :generateContent 路径）
DRAW_API_IMAGES = "images"
DRAW_API_CHAT = "chat"


def get_draw_api():
    """
    openai 类型 provider 的生图接口形态。

    - "images"（默认）：走 /v1/images/generations 与 /v1/images/edits
    - "chat"：走 /v1/chat/completions + modalities=["text","image"]，
      图片以 data URI 内嵌在 message 里（部分中转站只实现这条）

    gemini 类型 provider 忽略此项。
    """
    cfg = _safe_config()
    raw = str(cfg.get("llm", {}).get("draw_api", "") or "").strip().lower()
    return DRAW_API_CHAT if raw == DRAW_API_CHAT else DRAW_API_IMAGES

def get_llm_user_agent(provider=None):
    """Optional custom User-Agent for LLM HTTP requests."""
    provider = provider or get_llm_provider()
    provider_cfg = get_provider_config(provider)
    if provider_cfg.get("user_agent") is not None:
        return provider_cfg.get("user_agent")

    cfg = _safe_config()
    return cfg.get("llm", {}).get("user_agent")

def get_llm_max_output_tokens(model_alias: str = "smart"):
    """Gets max output tokens for a given model alias (if configured)."""
    cfg = _safe_config()
    llm_cfg = cfg.get("llm", {})
    per_alias = (llm_cfg.get("max_output_tokens_by_alias") or {})
    if model_alias in per_alias:
        return per_alias.get(model_alias)
    return llm_cfg.get("max_output_tokens")

def get_message_history_config():
    """Gets the message history configuration."""
    cfg = _safe_config()
    return cfg.get("memory", {}).get("message_history", {})

def get_group_listener_config() -> dict:
    """获取群聊独立在线监听配置，并补齐安全默认值。"""
    cfg = _safe_config()
    raw = ((cfg.get("interaction", {}) or {}).get("group_listener", {}) or {})
    defaults = {
        "enabled": True,
        "evaluation_batch_size": 2,
        "idle_seconds": 90.0,
        "max_pending_messages": 20,
        "decision_timeout_seconds": 30.0,
        # 硬超时降级（不经过模型），防止 ONLINE 永久挂着
        "watchdog_interval_seconds": 30.0,
        "no_interaction_timeout_seconds": 1800.0,
        "idle_exit_seconds": 1200.0,
        "max_online_seconds": 21600.0,
        # 窗口内定向消息（@/回复 Nora）一票否决 SEMI_ONLINE 的时效
        "directed_veto_seconds": 300.0,
        # 连续多少轮 fast 都投 SEMI_ONLINE 时，允许带并发后缀强制降级
        "semi_online_vote_threshold": 2,
    }
    merged = dict(defaults)
    if isinstance(raw, dict):
        merged.update(raw)
        legacy_mode = str(raw.get("default_mode") or "").strip().lower()
        if legacy_mode == "online":
            logger.warning(
                "interaction.group_listener.default_mode=online 已废弃并被忽略；"
                "未登记群固定为 semi_online，且全局最多一个群 ONLINE"
            )
    merged.pop("default_mode", None)

    merged["enabled"] = bool(merged.get("enabled", True))

    numeric_fields = {
        "evaluation_batch_size": (int, 1),
        "idle_seconds": (float, 0.01),
        "max_pending_messages": (int, 1),
        "decision_timeout_seconds": (float, 0.01),
        "watchdog_interval_seconds": (float, 1.0),
        # 0 表示关闭对应的硬超时
        "no_interaction_timeout_seconds": (float, 0.0),
        "idle_exit_seconds": (float, 0.0),
        "max_online_seconds": (float, 0.0),
        "directed_veto_seconds": (float, 0.0),
        "semi_online_vote_threshold": (int, 1),
    }
    for key, (converter, minimum) in numeric_fields.items():
        try:
            value = converter(merged.get(key, defaults[key]))
            merged[key] = max(minimum, value)
        except (TypeError, ValueError):
            merged[key] = defaults[key]
    return merged


def get_workspace_config():
    """Gets the workspace configuration."""
    cfg = _safe_config()
    return cfg.get("workspace", {})


def get_custom_injection_scopes():
    """Gets the CUSTOM.md injection scopes configuration."""
    cfg = _safe_config()
    return (cfg.get("custom_injection", {}) or {}).get("scopes")


def get_triggers_config():
    """Gets external triggers configuration.

    Priority:
    1) triggers/config.yml (new location)
    2) config.yml -> triggers (legacy fallback)
    """
    if os.path.exists(TRIGGERS_CONFIG_FILE):
        try:
            with open(TRIGGERS_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            pass

    cfg = _safe_config()
    return cfg.get("triggers", {}) or {}


def get_logging_config():
    """Gets logging configuration (retention, directory, etc.)."""
    cfg = _safe_config()
    return cfg.get("logging", {}) or {}


def get_owner_config() -> dict:
    """获取主人识别配置。

    结构：
      owner:
        identities:
          - platform: telegram
            user_id: "123456"
            display_name: "..."   # 可选
    缺省返回空 dict（启用纯自动绑定）。
    """
    cfg = _safe_config()
    return cfg.get("owner", {}) or {}


def get_nora_preferences() -> dict:
    """获取 Nora 偏好设置，并补齐默认值。"""
    cfg = _safe_config()
    raw = (cfg.get("nora_preferences", {}) or {})

    defaults = {
        "nora_followup_probability": 1.0,
        "proactive_message_probability": 1.0,
        "split_reply_probability": 0.7,
        "short_reply_preference": 0.5,
        "verbosity_preference": 0.5,
        "pause_between_splits_seconds": 1.0,
        "warmth_level": 0.7,
        "playfulness_level": 0.4,
        "emotional_expressiveness": 0.6,
        "assertiveness_level": 0.5,
        "followup_skip_end_after": 3,
    }

    merged = dict(defaults)
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


def set_nora_preferences(preferences: dict) -> dict:
    """持久化 Nora 偏好设置，自动保留默认值与未知字段。"""
    cfg = dict(_safe_config())
    current = get_nora_preferences()
    current.update(preferences or {})
    cfg["nora_preferences"] = current
    save_config(cfg)
    return current

