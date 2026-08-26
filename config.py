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


# images.edit 参考图上传格式（仅 draw_api=images 时生效）
DRAW_EDIT_ENCODING_FORM = "form"
DRAW_EDIT_ENCODING_JSON = "json"


def get_draw_edit_encoding():
    """
    openai 类型 provider 的 images.edit 请求体格式。

    - "form"（默认）：multipart/form-data，OpenAI SDK 官方行为（OpenAI 官方、gpt-image-1）
    - "json"：application/json，参考图以 base64 data URI 内嵌在 image 字段
      （部分中转站把 /v1/images/edits 实现成只收 JSON，form 会直接 415）

    gemini / openrouter 类型 provider 忽略此项。
    """
    cfg = _safe_config()
    raw = str(cfg.get("llm", {}).get("draw_edit_encoding", "") or "").strip().lower()
    return DRAW_EDIT_ENCODING_JSON if raw == DRAW_EDIT_ENCODING_JSON else DRAW_EDIT_ENCODING_FORM


# draw_desc 产出的提示词写法。两类生图模型吃的东西完全不同，跟接口形态无关：
#   natural —— 自然语言句子（Gemini / nano-banana、Grok、GPT-Image 按语言理解画面）
#   tags    —— 逗号分隔的关键词标签（Stable Diffusion / NovelAI 系按标签匹配）
DRAW_PROMPT_STYLE_NATURAL = "natural"
DRAW_PROMPT_STYLE_TAGS = "tags"


def get_draw_prompt_style():
    """
    draw_desc 生成的提示词写法，默认 "natural"。

    选错不会报错，只会让出图变差：给 nano-banana 喂标签串会丢掉空间关系
    （谁在哪、手放哪、光从哪来），给 SD 喂长句则会被 CLIP 截断。
    """
    cfg = _safe_config()
    raw = str(cfg.get("llm", {}).get("draw_prompt_style", "") or "").strip().lower()
    return DRAW_PROMPT_STYLE_TAGS if raw == DRAW_PROMPT_STYLE_TAGS else DRAW_PROMPT_STYLE_NATURAL


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


def get_llm_temperature(model_alias: str = "smart"):
    """获取某个模型别名的采样温度（temperature）。

    优先级：llm.temperature_by_alias[alias] → llm.temperature → None（不传字段，用端点默认）。
    返回 float 或 None；非法值（无法转 float）按 None 处理。
    取值范围各家不同（OpenAI/Gemini 0~2，Anthropic 0~1），这里不裁剪，交给端点校验。
    """
    cfg = _safe_config()
    llm_cfg = cfg.get("llm", {}) or {}
    per_alias = (llm_cfg.get("temperature_by_alias") or {})
    raw = per_alias.get(model_alias) if model_alias in per_alias else llm_cfg.get("temperature")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# 推理强度（reasoning effort）。档位表照 OpenAI 官方 reasoning 文档的枚举取：
# none / minimal / low / medium / high / xhigh / max（原文：「Supported values are
# model-dependent and can include ...」——注意是 model-dependent，同一家不同模型
# 认的子集都不一样，所以 provider 侧必须有降级重试，不能假设配了就能用）。
#
# `auto` 是**本项目自己加的第 8 档**，不是任何一家的合法字符串值，语义是"让端点
# 自己决定思考多少"。三家都有这个能力但都没有这个名字，由 provider 翻译成各自的
# 原生自动模式：openai 不传该字段 / gemini 2.5 用 thinkingBudget=-1（dynamic）/
# anthropic 新模型用 thinking={"type":"adaptive"}。
#
# 各家字段互不通用（详见各 provider 的 _apply_effort / _effort_generation_config）：
#   openai(Chat)      → reasoning_effort="high"           ← 平铺
#   openai(Responses) → reasoning={"effort":"high"}        ← 嵌套，写平铺会 400
#   openrouter        → reasoning={"effort":"high"}        ← 全档位照收，自己往下游翻
#   anthropic 4.6+    → output_config={"effort":"high"}    ← 顶层兄弟字段，不在 thinking 里
#   anthropic 4.5-    → thinking={"type":"enabled","budget_tokens":N}
#   gemini 3.x        → thinking_config={"thinking_level":"HIGH"}
#   gemini 2.5        → thinking_config={"thinking_budget":N}
#
# 未配置（None）表示完全不传该字段；"none" 是显式关闭思考，两者不是一回事
# （所以判据一律是 `is None`，写 `if not effort` 会把 none 档吞掉）。
EFFORT_NONE = "none"
EFFORT_AUTO = "auto"
EFFORT_LEVELS = (
    EFFORT_NONE,
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    EFFORT_AUTO,
)

# 档位 → thinking token 预算。给 anthropic 旧模型 / gemini 2.5 这类要整数预算的 API 用。
# 数值是经验取值，够区分档位即可；none=0 是关闭思考。
# auto=-1 不是"负预算"，是 Gemini 的 dynamic thinking 哨兵值（模型自己按复杂度调预算）；
# 不吃这个哨兵的 API（anthropic）必须自己判负数，别直接塞进 budget_tokens。
EFFORT_BUDGET_TOKENS = {
    EFFORT_NONE: 0,
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
    "max": 32768,
    EFFORT_AUTO: -1,
}

# 档位 → Gemini 3.x 的 thinkingLevel 枚举。Gemini 只有 4 档，所以 xhigh/max 一起
# 降到 HIGH（OpenRouter 官方映射表也是这么降的，原文标注 "mapped down"）。
# none 也映射成 MINIMAL 而不是不传：Gemini 3.x 关不掉思考，最低档就是 MINIMAL。
EFFORT_THINKING_LEVELS = {
    EFFORT_NONE: "MINIMAL",
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}


def normalize_effort(value):
    """把任意输入规整成合法档位；无法识别时返回 None（= 不传该字段）。"""
    raw = str(value if value is not None else "").strip().lower()
    if not raw:
        return None
    if raw in ("off", "disable", "disabled", "false"):
        return EFFORT_NONE
    if raw in ("adaptive", "dynamic", "default"):
        # 三家的"自动"各叫各的名字，统一收进 auto 档，省得用户按某一家的叫法写就失效。
        return EFFORT_AUTO
    return raw if raw in EFFORT_LEVELS else None


def get_effort_thinking_level(effort):
    """档位 → Gemini 3.x 的 thinkingLevel 枚举值；auto/未配置返回 None（不传字段）。"""
    return EFFORT_THINKING_LEVELS.get(effort or "")


def get_llm_effort(model_alias: str = "smart"):
    """
    获取某个模型别名的推理强度档位。

    优先级：llm.effort_by_alias[alias] → llm.effort → None（不传字段）。
    返回值是 EFFORT_LEVELS 里的档位字符串，或 None 表示"不设置、用端点默认"。
    """
    cfg = _safe_config()
    llm_cfg = cfg.get("llm", {}) or {}
    per_alias = (llm_cfg.get("effort_by_alias") or {})
    if model_alias in per_alias:
        return normalize_effort(per_alias.get(model_alias))
    return normalize_effort(llm_cfg.get("effort"))


def get_llm_effort_budget_tokens(model_alias: str = "smart"):
    """
    推理强度对应的 thinking token 预算（anthropic / gemini 用）。

    返回 None 表示不设置；返回 0 表示显式关闭思考。
    """
    effort = get_llm_effort(model_alias)
    if effort is None:
        return None
    return EFFORT_BUDGET_TOKENS.get(effort)


def set_llm_effort_by_alias(model_alias: str, effort) -> dict:
    """
    持久化单个别名的推理强度。传 None / 空值表示清除该别名的设置（回落全局）。

    返回写入后的完整 effort_by_alias 映射。
    """
    cfg = dict(_safe_config())
    llm_cfg = dict(cfg.get("llm", {}) or {})
    per_alias = dict(llm_cfg.get("effort_by_alias") or {})

    normalized = normalize_effort(effort)
    if normalized is None:
        per_alias.pop(model_alias, None)
    else:
        per_alias[model_alias] = normalized

    if per_alias:
        llm_cfg["effort_by_alias"] = per_alias
    else:
        llm_cfg.pop("effort_by_alias", None)
    cfg["llm"] = llm_cfg
    save_config(cfg)
    return per_alias

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

