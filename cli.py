#!/usr/bin/env python3
# N.O.R.A. Core - 命令行管理工具
import argparse
import questionary
import os
import yaml
import warnings
import logging
import sys
import sqlite3
import requests
from pathlib import Path

# Suppress warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import google.generativeai as genai
import openai
from openai import PermissionDeniedError
from core.cost_tracker import CostTracker
from typing import Dict, Any, List, Tuple, Optional

from memory.message_history import MessageHistory, get_default_message_history_db

# Configure logger for CLI operations
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Default fake User-Agent for OpenAI-compatible endpoints
DEFAULT_FAKE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)

# --- i18n 配置 ---
LOCALE_DIR = "locales"
DEFAULT_LOCALE = "zh_CN"
_strings = {}

def t(key: str, **kwargs) -> str:
    """简单的 i18n 翻译函数"""
    keys = key.split('.')
    value = _strings
    for k in keys:
        value = value.get(k)
        if value is None:
            return key # fallback
    if isinstance(value, str):
        return value.format(**kwargs)
    return str(value)

def load_locale():
    """加载语言文件"""
    global _strings
    locale_file = os.path.join(LOCALE_DIR, f"{DEFAULT_LOCALE}.yml")
    if not os.path.exists(locale_file):
        print(f"警告: 语言文件 {locale_file} 未找到。")
        return
    with open(locale_file, 'r', encoding='utf-8') as f:
        _strings = yaml.safe_load(f)

# --- 模型获取 ---
def get_gemini_models(api_key: str):
    try:
        genai.configure(api_key=api_key)
        models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        return sorted(models)
    except Exception as e:
        questionary.print(t('wizard.model_fetch_error', error=e), style="bold red")
        return None

def get_openai_models(api_key: str, base_url: Optional[str] = None):
    def fetch_models(client):
        return sorted([model.id for model in client.models.list()])

    try:
        client = openai.OpenAI(api_key=api_key, base_url=base_url or None)
        return fetch_models(client)
    except PermissionDeniedError as e:
        status = getattr(e, "status_code", None) or getattr(e, "status", None)
        if status == 403:
            try:
                ua_client = openai.OpenAI(
                    api_key=api_key,
                    base_url=base_url or None,
                    default_headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    },
                )
                return fetch_models(ua_client)
            except Exception as retry_err:
                questionary.print(t('wizard.model_fetch_error', error=retry_err), style="bold red")
                return None
        questionary.print(t('wizard.model_fetch_error', error=e), style="bold red")
        return None


def get_anthropic_models(api_key: str):
    # Anthropic 暂无稳定模型列表接口，这里提供常用模型兜底。
    _ = api_key
    return [
        "claude-3-5-haiku-latest",
        "claude-3-5-sonnet-latest",
        "claude-3-7-sonnet-latest",
        "claude-sonnet-4-0",
        "claude-opus-4-0",
    ]


def detect_embedding_dimensions(base_url: str, api_key: str, model: str) -> Optional[int]:
    """调用 embeddings 接口自动探测向量维度（OpenAI 兼容协议）。"""
    if not base_url or not api_key or not model:
        return None

    endpoint = f"{base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": "dimension probe",
        "model": model,
        "encoding_format": "float",
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        embedding = data.get("data", [{}])[0].get("embedding")
        if isinstance(embedding, list) and embedding:
            return len(embedding)
    except Exception as e:
        logger.warning(f"自动探测 embedding 维度失败，将使用默认值。原因: {e}")

    return None


def get_model_price_info(provider: str, model_name: str) -> str:
    """获取模型的价格信息（用于显示）"""
    from core.cost_tracker import CostTracker
    
    tracker = CostTracker(db_path=":memory:")  # 仅用于查询价格
    prices = tracker.get_model_price(provider, model_name)
    
    if not prices:
        return ""
    
    input_price = prices["input"]
    output_price = prices["output"]
    
    if input_price == 0 and output_price == 0:
        return " 💚 FREE"
    
    # 格式化价格显示
    if input_price < 1 and output_price < 1:
        return f" 💰 ${input_price:.3f}/${output_price:.2f} per M"
    else:
        return f" 💰 ${input_price:.2f}/${output_price:.2f} per M"


# --- 聊天记录管理 ---

def _load_message_history() -> Tuple[MessageHistory, Path]:
    import config
    history_cfg = config.get_message_history_config()
    raw_db_path = history_cfg.get("db_path")
    db_path = Path(raw_db_path) if raw_db_path else get_default_message_history_db()
    mh = MessageHistory(
        db_path=str(db_path),
        raw_window=history_cfg.get("raw_window", 50),
        compress_window=history_cfg.get("compress_window", 200),
        compress_ratio=history_cfg.get("compress_ratio", 10),
        archive_threshold=history_cfg.get("archive_threshold", 500),
        timezone=history_cfg.get("timezone", "Asia/Shanghai"),
    )
    return mh, db_path


def _list_chats(db_path: Path) -> List[sqlite3.Row]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT platform, chat_id, COUNT(*) as count,
               SUM(is_pinned) as pinned,
               SUM(is_archived) as archived
        FROM messages
        GROUP BY platform, chat_id
        ORDER BY count DESC
        """
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def _summaries_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM summaries")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def show_history_stats():
    mh, db_path = _load_message_history()
    if not db_path.exists():
        logger.info("ℹ️  未找到聊天记录数据库，路径: %s", db_path)
        return

    chats = _list_chats(db_path)
    summaries_count = _summaries_count(db_path)

    total_messages = sum(row["count"] for row in chats)
    pinned_messages = sum(row["pinned"] or 0 for row in chats)
    archived_messages = sum(row["archived"] or 0 for row in chats)

    logger.info("💾 聊天记录文件: %s", db_path)
    logger.info(
        "📊 总消息: %d | 已归档: %d | 已标记: %d | 总结: %d",
        total_messages,
        archived_messages,
        pinned_messages,
        summaries_count,
    )

    if not chats:
        logger.info("ℹ️  没有可展示的聊天记录。")
        return

    logger.info("📚 按聊天统计:")
    for row in chats:
        logger.info(
            "- %s/%s -> %d 条 (标记 %d, 归档 %d)",
            row["platform"],
            row["chat_id"],
            row["count"],
            row["pinned"] or 0,
            row["archived"] or 0,
        )


def clear_history_all(include_pinned: bool = False):
    mh, db_path = _load_message_history()
    chats = _list_chats(db_path)

    if not chats:
        logger.info("ℹ️  没有可清理的聊天记录。")
        return

    for row in chats:
        mh.clear_chat_history(
            platform=row["platform"],
            chat_id=row["chat_id"],
            keep_pinned=not include_pinned,
        )
    logger.info("✅ 已清理全部聊天记录 (包含标记: %s)", include_pinned)


def clear_history_chat(chat_id: str, platform: str = "telegram", include_pinned: bool = False):
    mh, db_path = _load_message_history()
    chats = _list_chats(db_path)
    exists = any(row["platform"] == platform and row["chat_id"] == chat_id for row in chats)
    if not exists:
        logger.warning("⚠️  未找到聊天 %s/%s，无法清理。", platform, chat_id)
        return
    mh.clear_chat_history(platform=platform, chat_id=chat_id, keep_pinned=not include_pinned)
    logger.info("✅ 已清理聊天 %s/%s (包含标记: %s)", platform, chat_id, include_pinned)


def history_menu():
    """交互式聊天记录管理"""
    mh, db_path = _load_message_history()
    while True:
        choices = [
            "📊 查看聊天记录统计",
            "🧹 清空全部 (保留标记)",
            "🧹 清空全部 (包含标记)",
            "🧹 按聊天清理 (保留标记)",
            "🧹 按聊天清理 (包含标记)",
            "⬅️ 返回主菜单",
        ]

        choice = questionary.select("选择操作:", choices=choices).ask()
        if choice is None or choice == "⬅️ 返回主菜单":
            break

        if choice == "📊 查看聊天记录统计":
            show_history_stats()
        elif choice == "🧹 清空全部 (保留标记)":
            if questionary.confirm("确定清空全部非标记聊天记录？").ask():
                clear_history_all(include_pinned=False)
        elif choice == "🧹 清空全部 (包含标记)":
            if questionary.confirm("确定清空全部聊天记录（包含已标记）？").ask():
                clear_history_all(include_pinned=True)
        elif choice in ["🧹 按聊天清理 (保留标记)", "🧹 按聊天清理 (包含标记)"]:
            chats = _list_chats(db_path)
            if not chats:
                logger.info("ℹ️  没有可清理的聊天记录。")
                continue
            options = [
                questionary.Choice(
                    title=f"{row['platform']}/{row['chat_id']} ({row['count']} 条, 标记 {row['pinned'] or 0})",
                    value=(row['platform'], row['chat_id'])
                )
                for row in chats
            ]
            selected = questionary.select("选择要清理的聊天:", choices=options).ask()
            if selected is None:
                continue
            include_pinned = choice.endswith("包含标记)")
            clear_history_chat(chat_id=selected[1], platform=selected[0], include_pinned=include_pinned)


# --- 向导步骤 ---
class ConfigStep:
    def __init__(self, state: Dict[str, Any]):
        self.state = state

    def run(self):
        raise NotImplementedError

class StepTelegram(ConfigStep):
    def run(self):
        self.state['telegram_token'] = questionary.password(
            t('wizard.token_prompt'),
            default=self.state.get("telegram_token", "")
        ).ask()
        return self.state['telegram_token'] is not None

class StepWorkspace(ConfigStep):
    def run(self):
        workspace_def = self.state.get('workspace', {}).get('root_path', '~/.nora/workspace')
        
        workspace_path = questionary.text(
            t('wizard.workspace_prompt'),
            default=workspace_def
        ).ask()
        
        if workspace_path is None:
            return False
        
        self.state['workspace'] = {'root_path': workspace_path}
        return True


class StepProxy(ConfigStep):
    """全局代理配置步骤。"""

    def run(self):
        network_cfg = self.state.get('network', {}) or {}
        proxy_cfg = network_cfg.get('proxy', {}) or self.state.get('proxy', {}) or {}

        enabled = questionary.confirm(
            "是否启用全局网络代理？",
            default=bool(proxy_cfg.get('enabled', False))
        ).ask()
        if enabled is None:
            return False

        if not enabled:
            self.state['network'] = {'proxy': {'enabled': False}}
            return True

        # 默认优先使用单一 URL（同时作用于 http/https/all）
        default_url = (
            proxy_cfg.get('url')
            or proxy_cfg.get('http')
            or proxy_cfg.get('https')
            or proxy_cfg.get('all')
            or "http://127.0.0.1:7890"
        )
        use_single_url = questionary.confirm(
            "是否使用单一代理地址（推荐）？",
            default=bool(proxy_cfg.get('url', True))
        ).ask()
        if use_single_url is None:
            return False

        next_proxy_cfg: Dict[str, Any] = {'enabled': True}

        if use_single_url:
            url = questionary.text(
                "代理地址（示例: http://127.0.0.1:7890）:",
                default=str(default_url)
            ).ask()
            if url is None:
                return False
            url = url.strip()
            if not url:
                questionary.print("代理地址不能为空。", style="bold red")
                return False
            next_proxy_cfg['url'] = url
        else:
            http_proxy = questionary.text(
                "HTTP 代理（可留空）:",
                default=str(proxy_cfg.get('http', ''))
            ).ask()
            if http_proxy is None:
                return False

            https_proxy = questionary.text(
                "HTTPS 代理（可留空）:",
                default=str(proxy_cfg.get('https', ''))
            ).ask()
            if https_proxy is None:
                return False

            all_proxy = questionary.text(
                "ALL 代理（可留空，支持 socks5://）:",
                default=str(proxy_cfg.get('all', ''))
            ).ask()
            if all_proxy is None:
                return False

            http_proxy = http_proxy.strip()
            https_proxy = https_proxy.strip()
            all_proxy = all_proxy.strip()

            if http_proxy:
                next_proxy_cfg['http'] = http_proxy
            if https_proxy:
                next_proxy_cfg['https'] = https_proxy
            if all_proxy:
                next_proxy_cfg['all'] = all_proxy

            if not any([http_proxy, https_proxy, all_proxy]):
                questionary.print("至少需要填写一个代理地址。", style="bold red")
                return False

        no_proxy = questionary.text(
            "NO_PROXY（可留空，如 127.0.0.1,localhost,.local）:",
            default=str(proxy_cfg.get('no_proxy', ''))
        ).ask()
        if no_proxy is None:
            return False
        no_proxy = no_proxy.strip()
        if no_proxy:
            next_proxy_cfg['no_proxy'] = no_proxy

        self.state['network'] = {'proxy': next_proxy_cfg}
        return True

class StepProvider(ConfigStep):
    def _default_provider_name(self, provider_type: str, providers: Dict[str, Any]) -> str:
        base = provider_type
        if base not in providers:
            return base
        idx = 2
        while f"{base}_{idx}" in providers:
            idx += 1
        return f"{base}_{idx}"

    def _configure_provider(self, existing: Optional[Dict[str, Any]] = None, preset_type: Optional[str] = None):
        providers = self.state.setdefault("providers", {})

        provider_type = preset_type or (existing or {}).get("type", "gemini")
        if provider_type not in ["gemini", "openai", "anthropic"]:
            provider_type = "openai"

        default_name = (existing or {}).get("name") or self._default_provider_name(provider_type, providers)
        provider_name = questionary.text("提供商名称（唯一标识）:", default=default_name).ask()
        if provider_name is None:
            return None
        provider_name = provider_name.strip()
        if not provider_name:
            questionary.print("提供商名称不能为空", style="bold red")
            return None

        api_key = questionary.password(
            "请输入 API Key:",
            default=(existing or {}).get("api_key", "")
        ).ask()
        if api_key is None:
            return None

        provider_cfg = {
            "type": provider_type,
            "api_key": api_key,
        }

        if provider_type == "openai":
            base_url = questionary.text(
                t('wizard.openai_base_url_prompt'),
                default=(existing or {}).get("base_url") or "https://api.openai.com/v1"
            ).ask()
            if base_url is None:
                return None
            provider_cfg["base_url"] = base_url

            use_fake_ua = questionary.confirm(
                t('wizard.openai_user_agent_prompt'),
                default=bool((existing or {}).get("user_agent"))
            ).ask()
            if use_fake_ua is None:
                return None
            provider_cfg["user_agent"] = DEFAULT_FAKE_UA if use_fake_ua else ""
            provider_cfg["use_responses_api"] = bool(questionary.confirm(
                "是否优先使用 OpenAI Responses API？",
                default=bool((existing or {}).get("use_responses_api", False))
            ).ask())

        providers[provider_name] = provider_cfg
        # 保证至少有一个默认 provider
        if not self.state.get("provider"):
            self.state["provider"] = provider_name
        return provider_name

    def run(self):
        providers = self.state.setdefault("providers", {})

        while True:
            default_provider = self.state.get("provider")
            summary = "\n".join([
                f"  - {name} ({cfg.get('type', 'unknown')}){' [default]' if name == default_provider else ''}"
                for name, cfg in providers.items()
            ]) or "  (暂无)"
            questionary.print("\n当前已配置提供商:\n" + summary, style="bold")

            choices = [
                questionary.Choice("➕ 添加 Gemini 提供商", value="add_gemini"),
                questionary.Choice("➕ 添加 OpenAI 兼容提供商", value="add_openai"),
                questionary.Choice("➕ 添加 Anthropic 提供商", value="add_anthropic"),
            ]
            if providers:
                choices.extend([
                    questionary.Choice("✏️ 编辑已有提供商", value="edit"),
                    questionary.Choice("🗑️ 删除提供商", value="delete"),
                    questionary.Choice("⭐ 设置默认提供商", value="set_default"),
                    questionary.Choice("✅ 完成提供商配置", value="done"),
                ])

            action = questionary.select("配置 LLM 提供商:", choices=choices).ask()
            if action is None:
                return False

            if action == "add_gemini":
                self._configure_provider(preset_type="gemini")
            elif action == "add_openai":
                self._configure_provider(preset_type="openai")
            elif action == "add_anthropic":
                self._configure_provider(preset_type="anthropic")
            elif action == "edit":
                name = questionary.select("选择要编辑的提供商:", choices=list(providers.keys())).ask()
                if name is None:
                    continue
                existing = dict(providers[name])
                existing["name"] = name
                new_name = self._configure_provider(existing=existing, preset_type=existing.get("type"))
                if not new_name:
                    continue
                if new_name != name and name in providers:
                    providers.pop(name, None)
                    if self.state.get("provider") == name:
                        self.state["provider"] = new_name
            elif action == "delete":
                if len(providers) <= 1:
                    questionary.print("至少需要保留一个提供商。", style="bold yellow")
                    continue
                name = questionary.select("选择要删除的提供商:", choices=list(providers.keys())).ask()
                if name is None:
                    continue
                providers.pop(name, None)
                if self.state.get("provider") == name:
                    self.state["provider"] = next(iter(providers.keys()), None)
                # 清理模型绑定
                model_providers = self.state.setdefault("model_providers", {})
                for role, provider_name in list(model_providers.items()):
                    if provider_name == name:
                        model_providers.pop(role, None)
            elif action == "set_default":
                name = questionary.select("选择默认提供商:", choices=list(providers.keys())).ask()
                if name is not None:
                    self.state["provider"] = name
            elif action == "done":
                if not providers:
                    questionary.print("请至少添加一个提供商。", style="bold red")
                    continue
                if not self.state.get("provider"):
                    self.state["provider"] = next(iter(providers.keys()))
                return True

class StepAPIKeys(ConfigStep):
    def run(self):
        # 已合并到 StepProvider，保留该步骤以兼容旧流程。
        return True

class StepDatabase(ConfigStep):
    def run(self):
        # Qdrant Config
        qdrant_host_def = self.state.get('memory', {}).get('qdrant', {}).get('host', 'localhost')
        qdrant_port_def = str(self.state.get('memory', {}).get('qdrant', {}).get('port', 6333))
        
        q_host = questionary.text(t('wizard.qdrant_host'), default=qdrant_host_def).ask()
        if q_host is None: return False
        
        q_port = questionary.text(t('wizard.qdrant_port'), default=qdrant_port_def).ask()
        if q_port is None: return False

        # Mongo Config
        mongo_uri_def = self.state.get('memory', {}).get('mongo', {}).get('uri', 'mongodb://nora:nora_password@localhost:27017/')
        mongo_uri = questionary.text(t('wizard.mongo_uri'), default=mongo_uri_def).ask()
        if mongo_uri is None: return False

        # Save to state
        if 'memory' not in self.state: self.state['memory'] = {}
        self.state['memory']['qdrant'] = {'host': q_host, 'port': int(q_port)}
        self.state['memory']['mongo'] = {'uri': mongo_uri}
        return True

class StepEmbedding(ConfigStep):
    def run(self):
        mem_cfg = self.state.get('memory', {})
        embed_cfg = mem_cfg.get('embedding', {})

        # Provider Selection
        prov_def = embed_cfg.get('provider', 'siliconflow')
        choices = ["siliconflow", "openai", "openrouter", "custom"]
        
        # Ensure default is in choices
        if prov_def not in choices: choices.append(prov_def)
        
        provider = questionary.select(
            t('wizard.embed_provider_prompt'),
            choices=choices,
            default=prov_def
        ).ask()
        if provider is None: return False

        # Smart Base URL Defaults
        url_def = embed_cfg.get('base_url', '')
        if not url_def: # Only set default if not already configured
            if provider == 'siliconflow': url_def = 'https://api.siliconflow.cn/v1'
            elif provider == 'openai': url_def = 'https://api.openai.com/v1'
            elif provider == 'openrouter': url_def = 'https://openrouter.ai/api/v1'
        
        base_url = questionary.text(t('wizard.embed_base_url_prompt'), default=url_def).ask()
        if base_url is None: return False

        # API Key
        key_def = embed_cfg.get('api_key', '')
        api_key = questionary.password(t('wizard.embed_key_prompt'), default=key_def).ask()
        if api_key is None: return False

        # Model
        model_def = embed_cfg.get('model', 'BAAI/bge-m3')
        if provider == 'openai' and model_def == 'BAAI/bge-m3': model_def = 'text-embedding-3-small' # Adjust default for OpenAI
        
        model = questionary.text(t('wizard.embed_model_prompt'), default=model_def).ask()
        if model is None: return False

        # 自动探测向量维度；失败则回退到已有配置或 1536
        existing_dims = embed_cfg.get('dimensions', 1536)
        detected_dims = detect_embedding_dimensions(base_url, api_key, model)
        dimensions = detected_dims if detected_dims else existing_dims
        if detected_dims:
            questionary.print(f"✅ 已自动探测 embedding 维度: {detected_dims}", style="bold green")
        else:
            questionary.print(f"⚠️ 自动探测失败，使用维度: {dimensions}", style="bold yellow")

        # Save
        if 'memory' not in self.state: self.state['memory'] = {}
        self.state['memory']['embedding'] = {
            'provider': provider,
            'base_url': base_url,
            'api_key': api_key,
            'model': model,
            'dimensions': dimensions
        }
        return True

class StepTavily(ConfigStep):
    """Tavily 网络搜索 API 配置"""
    def run(self):
        tavily_cfg = self.state.get('tavily', {})
        
        # 询问是否配置 Tavily
        configure = questionary.confirm(
            "是否配置 Tavily 网络搜索 API？(可选，用于 web_search 技能)",
            default=bool(tavily_cfg.get('api_key'))
        ).ask()
        
        if not configure:
            # 如果用户选择不配置，保持现有配置或设置为空
            return True
        
        # API Key
        key_def = tavily_cfg.get('api_key', '')
        api_key = questionary.password(
            "请输入 Tavily API Key (从 https://tavily.com/ 获取):",
            default=key_def
        ).ask()
        
        if api_key is None:
            return False
        
        # Timeout
        timeout_def = str(tavily_cfg.get('timeout', 10))
        timeout = questionary.text(
            "API 请求超时时间（秒）:",
            default=timeout_def
        ).ask()
        
        if timeout is None:
            return False
        
        # Save
        self.state['tavily'] = {
            'api_key': api_key,
            'timeout': int(timeout)
        }
        return True

class StepTimezone(ConfigStep):
    """时区配置"""
    def run(self):
        memory_cfg = self.state.get('memory', {})
        msg_history_cfg = memory_cfg.get('message_history', {})
        
        # 常用时区选项
        common_timezones = [
            "Asia/Shanghai",
            "UTC",
            "Asia/Tokyo",
            "Europe/London",
            "America/New_York",
            "America/Los_Angeles",
            questionary.Separator(),
            "手动输入其他时区"
        ]
        
        current_tz = msg_history_cfg.get('timezone', 'Asia/Shanghai')
        
        # 如果当前时区在常用列表中，设为默认
        kwargs = {}
        if current_tz in common_timezones:
            kwargs['default'] = current_tz
        
        timezone = questionary.select(
            "选择消息时间戳显示的时区:",
            choices=common_timezones,
            **kwargs
        ).ask()
        
        if timezone is None:
            return False
        
        if timezone == "手动输入其他时区":
            timezone = questionary.text(
                "请输入时区名称 (如 Asia/Hong_Kong):",
                default=current_tz
            ).ask()
            
            if timezone is None:
                return False
        
        # 验证时区
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone)
        except Exception as e:
            questionary.print(f"⚠️  无效的时区: {e}", style="bold red")
            return False
        
        # 保存配置
        if 'memory' not in self.state:
            self.state['memory'] = {}
        if 'message_history' not in self.state['memory']:
            self.state['memory']['message_history'] = {}
        
        self.state['memory']['message_history']['timezone'] = timezone
        questionary.print(f"✓ 时区已设置为: {timezone}", style="bold green")
        return True

class StepModels(ConfigStep):
    def _prompt_price(self, model_name: str) -> Optional[Dict[str, float]]:
        questionary.print(f"⚠️ 模型 {model_name} 未配置价格，请手动输入（USD per million tokens）", style="bold yellow")
        try:
            input_price = questionary.text("  输入 token 价格:").ask()
            if input_price is None:
                return None
            output_price = questionary.text("  输出 token 价格:").ask()
            if output_price is None:
                return None
            return {
                "input": float(input_price or 0),
                "output": float(output_price or 0)
            }
        except ValueError:
            questionary.print("价格格式错误，已跳过该模型。", style="bold red")
            return None
    def _resolve_provider_entries(self) -> Dict[str, Dict[str, Any]]:
        """统一解析 providers（兼容旧状态字段）。"""
        providers = dict(self.state.get("providers") or {})
        if providers:
            return providers

        # 兼容旧版 state: provider + gemini_key/openai_key + openai_base_url/user_agent
        legacy_provider = self.state.get("provider")
        if legacy_provider == "gemini" and self.state.get("gemini_key"):
            providers["gemini"] = {
                "type": "gemini",
                "api_key": self.state.get("gemini_key", ""),
            }
        elif legacy_provider == "openai" and self.state.get("openai_key"):
            providers["openai"] = {
                "type": "openai",
                "api_key": self.state.get("openai_key", ""),
                "base_url": self.state.get("openai_base_url") or "https://api.openai.com/v1",
                "user_agent": self.state.get("openai_user_agent", ""),
            }
        return providers

    def _select_provider_for_role(self, role_key: str, provider_entries: Dict[str, Dict[str, Any]]) -> Optional[str]:
        role_name = t(f'roles.{role_key}')
        model_providers = self.state.setdefault("model_providers", {})

        if len(provider_entries) == 1:
            only_one = next(iter(provider_entries.keys()))
            model_providers[role_key] = only_one
            return only_one

        default_provider = model_providers.get(role_key) or self.state.get("provider")
        if default_provider not in provider_entries:
            default_provider = next(iter(provider_entries.keys()))

        choices = [
            questionary.Choice(
                title=f"{name} ({cfg.get('type', 'unknown')})",
                value=name,
            )
            for name, cfg in provider_entries.items()
        ]

        selected = questionary.select(
            f"为『{role_name}』选择提供商:",
            choices=choices,
            default=default_provider,
        ).ask()
        if selected is None:
            return None

        model_providers[role_key] = selected
        return selected

    def _fetch_models_for_provider(self, provider_name: str, provider_cfg: Dict[str, Any]):
        provider_type = provider_cfg.get("type", provider_name)
        api_key = provider_cfg.get("api_key", "")
        if not api_key:
            questionary.print(f"提供商 {provider_name} 缺少 API Key", style="bold red")
            return None

        if provider_type == "gemini":
            return get_gemini_models(api_key)
        if provider_type == "openai":
            return get_openai_models(api_key, provider_cfg.get("base_url"))
        if provider_type == "anthropic":
            return get_anthropic_models(api_key)

        questionary.print(f"不支持的提供商类型: {provider_type}", style="bold red")
        return None

    def select_model(self, model_list, role_key, provider):
        role_name = t(f'roles.{role_key}')
        current_models = self.state.get('models', {})
        default_model = current_models.get(role_key) # Get existing value
        
        if not model_list:
            return questionary.text(t('wizard.manual_prompt', role=role_name), default=default_model or "").ask()
        
        # 为每个模型添加价格信息
        choices_with_prices = []
        for model in model_list:
            price_info = get_model_price_info(provider, model)
            display_name = f"{model}{price_info}" if price_info else model
            choices_with_prices.append(questionary.Choice(title=display_name, value=model))
        
        choices_with_prices.extend([questionary.Separator(), questionary.Choice(title=t('wizard.manual_entry'), value='__manual__')])
        
        # Ensure default exists in choices for Questionary to select it
        kwargs = {}
        if default_model and default_model in model_list:
            kwargs['default'] = default_model

        selection = questionary.select(
            t('wizard.model_select_prompt', role=role_name),
            choices=choices_with_prices,
            **kwargs
        ).ask()
        
        if selection == '__manual__':
            return questionary.text(t('wizard.manual_prompt', role=role_name), default=default_model or "").ask()
        return selection

    def run(self):
        provider_entries = self._resolve_provider_entries()
        if not provider_entries:
            questionary.print("未配置任何 LLM 提供商，请先在上一环节添加。", style="bold red")
            return False

        provider_type_cache: Dict[str, str] = {}
        provider_model_cache: Dict[str, List[str]] = {}
        models = self.state.get('models', {})
        model_providers = self.state.setdefault('model_providers', {})

        for role_key in ['smart', 'fast', 'coder', 'image', 'summary']:
            provider_name = self._select_provider_for_role(role_key, provider_entries)
            if provider_name is None:
                return False

            provider_cfg = provider_entries.get(provider_name, {})
            provider_type = provider_cfg.get("type", provider_name)
            provider_type_cache[provider_name] = provider_type

            if provider_name not in provider_model_cache:
                model_list = self._fetch_models_for_provider(provider_name, provider_cfg)
                if model_list is None:
                    return False
                provider_model_cache[provider_name] = model_list

            selected_model = self.select_model(provider_model_cache[provider_name], role_key, provider_type)
            if selected_model is None:
                return False

            models[role_key] = selected_model
            model_providers[role_key] = provider_name

        self.state['models'] = models

        # 记录选定模型的价格用于后续配置写入
        tracker = CostTracker(db_path=":memory:")
        model_prices = {}
        for model_alias, model_name in models.items():
            # 同一个模型可能被多个 role 复用（如 smart/fast 选同一模型）
            # 已经获取过价格就直接复用，避免重复提示手动输入。
            if model_name in model_prices:
                continue

            provider_name = model_providers.get(model_alias) or self.state.get('provider')
            provider_name = str(provider_name) if provider_name else ""
            provider_type = provider_type_cache.get(provider_name, provider_name)
            if not provider_type:
                provider_type = "gemini"

            price = tracker.get_model_price(provider_type, model_name)
            if price:
                model_prices[model_name] = price
            else:
                manual_price = self._prompt_price(model_name)
                if manual_price:
                    model_prices[model_name] = manual_price
        # 保存到 state，稍后写入 cost_tracking.custom_prices
        self.state['model_prices'] = model_prices
        return True


class StepCostTracking(ConfigStep):
    """成本跟踪配置步骤"""
    
    def run(self):
        print("\n📊 成本跟踪配置")
        print("-" * 50)
        print("N.O.R.A. 可以自动跟踪 LLM 调用的 token 使用量和成本。")
        print("内置了 Gemini / OpenAI / Anthropic 的官方或兼容定价，也支持自定义价格。")
        
        # 是否启用成本跟踪
        cost_config = self.state.get('cost_tracking', {})
        current_enabled = cost_config.get('enabled', True)
        
        enabled = questionary.confirm(
            "是否启用成本跟踪？",
            default=current_enabled
        ).ask()
        
        if not enabled:
            self.state['cost_tracking'] = {'enabled': False}
            return True
        
        # 询问是否需要自定义价格
        has_custom = questionary.confirm(
            "是否需要配置自定义模型价格？\n（如果使用官方 Gemini/OpenAI/Anthropic 模型，通常不需要）",
            default=False
        ).ask()
        
        custom_prices = {}
        
        if has_custom:
            print("\n💰 自定义模型价格（USD per million tokens）")
            print("提示: 输入空白跳过，完成后输入 'done'")
            
            while True:
                model_name = questionary.text(
                    "模型名称（或输入 'done' 完成）:"
                ).ask()
                
                if not model_name or model_name.lower() == 'done':
                    break
                
                try:
                    input_price = float(questionary.text(
                        f"  输入 token 价格（每百万）:"
                    ).ask() or 0)
                    
                    output_price = float(questionary.text(
                        f"  输出 token 价格（每百万）:"
                    ).ask() or 0)
                    
                    custom_prices[model_name] = {
                        "input": input_price,
                        "output": output_price
                    }
                    
                    print(f"  ✅ 已添加 {model_name}: ${input_price:.4f} / ${output_price:.4f}")
                except ValueError:
                    print("  ⚠️  价格格式错误，已跳过")
        
        self.state['cost_tracking'] = {
            'enabled': True,
            'custom_prices': custom_prices if custom_prices else {}
        }
        
        print("\n✅ 成本跟踪配置完成")
        return True


class StepOutputLimit(ConfigStep):
    """输出 token 上限配置步骤"""

    def run(self):
        print("\n🧷 输出 token 上限")
        print("-" * 50)
        print("限制 LLM 单次响应的最大输出 token，避免回复过长。")

        llm_cfg = self.state.get("llm", {})
        current_limit = llm_cfg.get("max_output_tokens")
        if current_limit is None:
            current_limit = 768

        raw = questionary.text(
            "默认输出上限（空=不限制）:",
            default=str(current_limit)
        ).ask()

        if raw is None:
            return False

        raw = raw.strip()
        if raw == "":
            llm_cfg.pop("max_output_tokens", None)
        else:
            try:
                value = int(raw)
                if value <= 0:
                    questionary.print(t('wizard.invalid_positive_integer'), style="bold red")
                    return False
                llm_cfg["max_output_tokens"] = value
            except ValueError:
                questionary.print(t('wizard.invalid_positive_integer'), style="bold red")
                return False

        self.state["llm"] = llm_cfg
        return True


# --- 配置向导主流程 ---
def run_wizard():
    """运行交互式配置向导"""
    load_locale()
    print(t('wizard.welcome'))

    # 加载现有配置
    config_file = "config.yml"
    state = {}
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
            llm_cfg = cfg.get("llm", {})
            providers = llm_cfg.get("providers", {}) or {}

            # 旧配置自动迁移到 providers
            if not providers:
                api_keys = llm_cfg.get("api_keys", {}) or {}
                if api_keys.get("gemini"):
                    providers["gemini"] = {
                        "type": "gemini",
                        "api_key": api_keys.get("gemini", ""),
                    }
                if api_keys.get("openai"):
                    providers["openai"] = {
                        "type": "openai",
                        "api_key": api_keys.get("openai", ""),
                        "base_url": llm_cfg.get("base_url") or "https://api.openai.com/v1",
                        "user_agent": llm_cfg.get("user_agent", ""),
                    }

            provider_default = llm_cfg.get("provider")
            if not provider_default and providers:
                provider_default = next(iter(providers.keys()))

            state = {
                'telegram_token': cfg.get("telegram", {}).get("bot_token"),
                'workspace': cfg.get("workspace", {}),
                'network': cfg.get("network", {}) or ({'proxy': cfg.get('proxy', {})} if cfg.get('proxy') else {}),
                'provider': provider_default,
                'providers': providers,
                'model_providers': llm_cfg.get("model_providers", {}) or {},
                'models': llm_cfg.get("models", {}),
                'llm': llm_cfg,
                'memory': cfg.get("memory", {}),
                'tavily': cfg.get("tavily", {}),
                'cost_tracking': cfg.get("cost_tracking", {'enabled': True})
            }

    steps = [StepTelegram, StepWorkspace, StepProxy, StepProvider, StepAPIKeys, StepDatabase, StepEmbedding, StepTavily, StepTimezone, StepModels, StepOutputLimit, StepCostTracking]
    current_step = 0
    
    while current_step < len(steps):
        step_instance = steps[current_step](state)
        success = step_instance.run()

        if not success: # User pressed Ctrl+C
            print(t('wizard.aborted_message'))
            return
        
        # 检查是否选择了 "返回"
        if state.get('provider') == t('wizard.back_option'):
            current_step = max(0, current_step - 1)
            continue
            
        current_step += 1

    # --- 确认保存 ---
    final_config: Dict[str, Any] = {
        'workspace': state.get('workspace', {'root_path': '~/.nora/workspace'}),
        'network': state.get('network', {}),
        'telegram': {'bot_token': state.get('telegram_token')},
        'llm': {
            'provider': state.get('provider'),
            'providers': state.get('providers', {}),
            'model_providers': state.get('model_providers', {}),
            'models': state.get('models', {})
        },
        'memory': state.get('memory', {}),
        'cost_tracking': state.get('cost_tracking', {'enabled': True})
    }
    if isinstance(state.get('llm'), dict):
        llm_state = state.get('llm') or {}
        if llm_state.get('max_output_tokens') is not None:
            final_config['llm']['max_output_tokens'] = llm_state.get('max_output_tokens')

    # 兼容旧配置结构：保留 api_keys/base_url/user_agent（取默认 provider 对应值）
    providers_cfg = final_config['llm'].get('providers', {}) or {}
    legacy_api_keys = {}
    for pname, pcfg in providers_cfg.items():
        ptype = pcfg.get('type')
        if ptype in ('gemini', 'openai') and ptype not in legacy_api_keys:
            legacy_api_keys[ptype] = pcfg.get('api_key', '')
    if legacy_api_keys:
        final_config['llm']['api_keys'] = legacy_api_keys

    default_provider_name = final_config['llm'].get('provider')
    default_provider_cfg = providers_cfg.get(default_provider_name, {}) if default_provider_name else {}
    if default_provider_cfg.get('type') == 'openai':
        if default_provider_cfg.get('base_url'):
            final_config['llm']['base_url'] = default_provider_cfg.get('base_url')
        if default_provider_cfg.get('user_agent'):
            final_config['llm']['user_agent'] = default_provider_cfg.get('user_agent')
    
    # 添加 Tavily 配置（如果存在）
    if state.get('tavily'):
        final_config['tavily'] = state['tavily']

    # 将模型选择的价格写入配置，便于后续成本计算
    model_prices = state.get('model_prices', {})
    if model_prices:
        cost_tracking_cfg: Dict[str, Any] = final_config.get('cost_tracking')  # type: ignore[assignment]
        if not isinstance(cost_tracking_cfg, dict):
            cost_tracking_cfg = {'enabled': True}
            final_config['cost_tracking'] = cost_tracking_cfg
        existing_custom = cost_tracking_cfg.get('custom_prices', {}) or {}
        if not isinstance(existing_custom, dict):
            existing_custom = {}
        existing_custom.update(model_prices)
        cost_tracking_cfg['custom_prices'] = existing_custom

    print(t('wizard.summary_title'))
    print(yaml.dump(final_config, indent=2, allow_unicode=True))

    if questionary.confirm(t('wizard.save_confirm')).ask():
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(final_config, f, indent=2, allow_unicode=True)
        print(t('wizard.success_message', file=config_file))
    else:
        print(t('wizard.aborted_message'))


# --- RAG/Qdrant 管理功能 ---

def clean_qdrant():
    """清理 Qdrant 向量数据库（删除所有 collections）"""
    try:
        from qdrant_client import QdrantClient
        import config
        
        logger.info("🔧 正在连接 Qdrant...")
        
        # 加载配置
        cfg = config.get_config()
        mem_cfg = cfg.get("memory", {})
        qdrant_cfg = mem_cfg.get("qdrant", {})
        
        host = qdrant_cfg.get("host", "localhost")
        port = qdrant_cfg.get("port", 6333)
        # 连接 Qdrant
        client = QdrantClient(host=host, port=port)
        
        # 列出所有 collections
        collections = client.get_collections()
        collection_names = [c.name for c in collections.collections]

        if not collection_names:
            logger.info("ℹ️  Qdrant 中没有任何 collection，无需清理。")
            return

        logger.warning("⚠️⚠️⚠️ 高危操作：即将删除 Qdrant 中所有 collections（不可恢复）！")
        logger.warning(f"将删除 {len(collection_names)} 个 collection: {collection_names}")

        # 第一次确认
        confirm = questionary.confirm(
            "确认继续删除 Qdrant 所有 collections？",
            default=False
        ).ask()
        
        if not confirm:
            logger.info("❌ 操作已取消。")
            return

        # 第二次确认：输入指定短语
        second_confirm = questionary.text(
            "二次确认：请输入 DELETE ALL 才会执行删除：",
            default=""
        ).ask()
        if second_confirm != "DELETE ALL":
            logger.info("❌ 二次确认未通过，操作已取消。")
            return

        # 删除全部 collections
        deleted = 0
        failed = 0
        for name in collection_names:
            try:
                client.delete_collection(collection_name=name)
                deleted += 1
                logger.info(f"✅ 已删除 Qdrant collection: {name}")
            except Exception as e:
                failed += 1
                logger.error(f"❌ 删除 Qdrant collection 失败: {name} ({e})")

        logger.info(f"🧹 Qdrant 清理完成：成功 {deleted}，失败 {failed}")
        
    except ImportError:
        logger.error("❌ 找不到 qdrant_client 库。请先安装: pip install qdrant-client")
    except Exception as e:
        logger.error(f"❌ 清理 Qdrant 时出错: {e}")


def test_qdrant():
    """测试 Qdrant 连接和基本操作"""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models
        import config
        
        logger.info("🔍 测试 Qdrant 连接...")
        
        # 加载配置
        cfg = config.get_config()
        mem_cfg = cfg.get("memory", {})
        qdrant_cfg = mem_cfg.get("qdrant", {})
        
        host = qdrant_cfg.get("host", "localhost")
        port = qdrant_cfg.get("port", 6333)
        
        # 连接测试
        client = QdrantClient(host=host, port=port)
        logger.info(f"✅ 成功连接到 Qdrant ({host}:{port})")
        
        # 列出所有 collections
        collections = client.get_collections()
        logger.info(f"📋 当前 Collections: {[c.name for c in collections.collections]}")
        
        # 检查 nora_memory collection
        collection_name = "nora_memory"
        exists = any(c.name == collection_name for c in collections.collections)
        
        if exists:
            # 获取统计信息
            collection_info = client.get_collection(collection_name=collection_name)
            logger.info(f"✅ Collection '{collection_name}' 存在")
            logger.info(f"   - 向量维度: {collection_info.config.params.vectors.size}")
            logger.info(f"   - 距离度量: {collection_info.config.params.vectors.distance}")
            logger.info(f"   - 点数量: {collection_info.points_count}")
        else:
            logger.warning(f"⚠️  Collection '{collection_name}' 不存在")
            
            # 询问是否创建
            create = questionary.confirm(
                f"是否创建 Collection '{collection_name}'？",
                default=True
            ).ask()
            
            if create:
                vector_size = mem_cfg.get('embedding', {}).get('dimensions', 1536)
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE
                    )
                )
                logger.info(f"✅ Collection '{collection_name}' 创建成功！")
        
        logger.info("✅ Qdrant 测试完成！")
        
    except ImportError:
        logger.error("❌ 找不到 qdrant_client 库。请先安装: pip install qdrant-client")
    except Exception as e:
        logger.error(f"❌ 测试 Qdrant 时出错: {e}")


def clean_rag():
    """清理 RAG 系统（包括 Qdrant 和 MongoDB）"""
    logger.info("🧹 准备清理 RAG 系统...")
    
    # 询问清理范围
    choices = [
        "仅 Qdrant（所有Collections）",
        "仅 MongoDB（所有Collections）",
        "全部清理（Qdrant + MongoDB）",
        "取消"
    ]
    
    choice = questionary.select(
        "选择清理范围:",
        choices=choices
    ).ask()
    
    if choice == "取消":
        logger.info("❌ 操作已取消。")
        return
    
    # 执行清理
    if choice in ["仅 Qdrant（所有Collections）", "全部清理（Qdrant + MongoDB）"]:
        clean_qdrant()
    
    if choice in ["仅 MongoDB（所有Collections）", "全部清理（Qdrant + MongoDB）"]:
        clean_mongodb()


def clean_mongodb():
    """清理 MongoDB（删除 nora 数据库下所有 collections）"""
    try:
        from pymongo import MongoClient
        import config
        
        logger.info("🔧 正在连接 MongoDB...")
        
        # 加载配置
        cfg = config.get_config()
        mem_cfg = cfg.get("memory", {})
        mongo_cfg = mem_cfg.get("mongo", {})
        
        mongo_uri = mongo_cfg.get("uri", "mongodb://localhost:27017/")
        db_name = "nora"
        # 连接 MongoDB
        client = MongoClient(mongo_uri)
        db = client[db_name]

        # 获取所有 collection
        collection_names = db.list_collection_names()
        if not collection_names:
            logger.info("ℹ️  MongoDB 中没有任何 collection，无需清理。")
            client.close()
            return

        total_docs = 0
        for name in collection_names:
            total_docs += db[name].count_documents({})

        logger.warning("⚠️⚠️⚠️ 高危操作：即将删除 MongoDB 所有 collections（不可恢复）！")
        logger.warning(f"目标数据库: {db_name} | collections: {collection_names} | 总文档数: {total_docs}")

        # 第一次确认
        confirm = questionary.confirm(
            "确认继续删除 MongoDB 所有 collections？",
            default=False
        ).ask()
        
        if not confirm:
            logger.info("❌ 操作已取消。")
            client.close()
            return

        # 第二次确认：输入指定短语
        second_confirm = questionary.text(
            "二次确认：请输入 DELETE ALL 才会执行删除：",
            default=""
        ).ask()
        if second_confirm != "DELETE ALL":
            logger.info("❌ 二次确认未通过，操作已取消。")
            client.close()
            return

        # 删除所有 collections
        dropped = 0
        failed = 0
        for name in collection_names:
            try:
                db.drop_collection(name)
                dropped += 1
                logger.info(f"✅ 已删除 MongoDB collection: {name}")
            except Exception as e:
                failed += 1
                logger.error(f"❌ 删除 MongoDB collection 失败: {name} ({e})")

        logger.info(f"🧹 MongoDB 清理完成：成功 {dropped}，失败 {failed}")
        
        client.close()
        
    except ImportError:
        logger.error("❌ 找不到 pymongo 库。请先安装: pip install pymongo")
    except Exception as e:
        logger.error(f"❌ 清理 MongoDB 时出错: {e}")


def test_rag():
    """测试 RAG 系统完整功能"""
    try:
        logger.info("🧪 测试 RAG 系统...")
        
        # 导入 RAG 引擎
        from memory.rag import RAGEngine
        
        # 初始化
        rag = RAGEngine()
        
        if not rag.enabled:
            logger.error("❌ RAG 系统未启用。请检查配置和依赖。")
            return
        
        logger.info("✅ RAG 引擎初始化成功")
        
        # 测试存储
        test_text = "这是一条测试记忆，用于验证 RAG 系统是否正常工作。"
        test_metadata = {"source": "cli_test", "type": "test"}
        
        logger.info(f"📝 测试存储: {test_text}")
        success = rag.add_memory(test_text, test_metadata)
        
        if success:
            logger.info("✅ 存储成功")
        else:
            logger.error("❌ 存储失败")
            return
        
        # 测试检索
        query = "测试记忆"
        logger.info(f"🔍 测试检索: {query}")
        results = rag.retrieve_memory(query, top_k=3)
        
        if results:
            logger.info(f"✅ 检索成功，找到 {len(results)} 条相关记忆:")
            for i, mem in enumerate(results, 1):
                score = mem.get('score', 0)
                text = mem.get('text', '')
                logger.info(f"   {i}. [相似度: {score:.3f}] {text[:50]}...")
        else:
            logger.warning("⚠️  未找到相关记忆")
        
        logger.info("✅ RAG 系统测试完成！")
        
    except ImportError as e:
        logger.error(f"❌ 导入错误: {e}")
        logger.error("请确保已安装所有依赖: pip install -r requirements.txt")
    except Exception as e:
        logger.error(f"❌ 测试 RAG 时出错: {e}")


def show_status():
    """显示 N.O.R.A. Core 的运行状态"""
    logger.info("📊 N.O.R.A. Core 运行状态")
    logger.info("="*50)

    # 1. 配置文件检查
    try:
        import config
        cfg = config.get_config()
        if not cfg:
            logger.error("❌ 配置文件 (config.yml): 未找到或为空")
            return
            
        logger.info("✅ 配置文件 (config.yml): 已加载")
        
        # 显示关键配置
        provider = cfg.get("llm", {}).get("provider", "N/A")
        smart_model = cfg.get("llm", {}).get("models", {}).get("smart", "N/A")
        logger.info(f"  - LLM Provider: {provider}")
        logger.info(f"  - Smart Model: {smart_model}")
        
    except FileNotFoundError:
        logger.error("❌ 配置文件 (config.yml): 未找到")
        return
    except Exception as e:
        logger.error(f"❌ 配置文件 (config.yml): 加载失败 - {e}")
        return

    # 2. 聊天记录数据库
    try:
        _, db_path = _load_message_history()
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            conn.execute("SELECT 1 FROM messages LIMIT 1")
            conn.close()
            logger.info(f"✅ 聊天记录数据库: 连接正常 ({db_path})")
        else:
            logger.warning(f"⚠️  聊天记录数据库: 未找到 ({db_path})")
    except Exception as e:
        logger.error(f"❌ 聊天记录数据库: 连接失败 - {e}")

    # 3. Qdrant 连接
    try:
        from memory.vector import VectorStore
        vs = VectorStore()
        if vs.client:
            logger.info(f"✅ Qdrant 向量数据库: 连接正常 ({vs.host}:{vs.port})")
        else:
            logger.error("❌ Qdrant 向量数据库: 连接失败")
    except Exception as e:
        logger.error(f"❌ Qdrant 向量数据库: 连接失败 - {e}")

    logger.info("="*50)


# --- 主菜单 ---

def main_menu():
    """显示主菜单并处理用户选择"""
    load_locale()
    
    choices = [
        "🔧 运行配置向导",
        "📊 查看运行状态",
        "🧪 测试 Qdrant 连接",
        "🧪 测试 RAG 系统",
        "💬 聊天记录管理",
        "🧹 清理 RAG 数据",
        "❌ 退出"
    ]
    
    while True:
        print("\n" + "="*50)
        print("N.O.R.A. Core - 管理工具")
        print("="*50)
        
        choice = questionary.select(
            "请选择操作:",
            choices=choices
        ).ask()
        
        if choice is None or choice == "❌ 退出":
            print("👋 再见！")
            break
        
        if choice == "🔧 运行配置向导":
            run_wizard()
        elif choice == "📊 查看运行状态":
            show_status()
        elif choice == "🧪 测试 Qdrant 连接":
            test_qdrant()
        elif choice == "🧪 测试 RAG 系统":
            test_rag()
        elif choice == "💬 聊天记录管理":
            history_menu()
        elif choice == "🧹 清理 RAG 数据":
            clean_rag()


# --- CLI 入口 ---

def main():
    """主 CLI 入口函数"""
    parser = argparse.ArgumentParser(description="N.O.R.A. Core - 命令行管理工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # 'wizard' 命令
    subparsers.add_parser("wizard", help="运行交互式配置向导")

    # 'status' 命令
    subparsers.add_parser("status", help="显示系统运行状态")

    # 'test-rag' 命令
    subparsers.add_parser("test-rag", help="测试 RAG 系统")

    # 'test-qdrant' 命令
    subparsers.add_parser("test-qdrant", help="测试 Qdrant 连接")

    # 'history' 命令
    history_parser = subparsers.add_parser("history", help="聊天记录管理")
    history_subparsers = history_parser.add_subparsers(dest="history_command", help="历史记录操作")
    history_subparsers.add_parser("stats", help="显示统计信息")
    clear_parser = history_subparsers.add_parser("clear", help="清理历史记录")
    clear_parser.add_argument("--all", action="store_true", help="清理所有聊天")
    clear_parser.add_argument("--chat-id", type=str, help="要清理的聊天 ID")
    clear_parser.add_argument("--platform", type=str, default="telegram", help="平台名称")
    clear_parser.add_argument("--include-pinned", action="store_true", help="同时清理已标记的消息")

    args = parser.parse_args()

    # 无配置文件时给出友好引导（wizard 允许直接运行）
    if args.command != "wizard" and not os.path.exists("config.yml"):
        print("⚠️ 未检测到配置文件 config.yml。")
        print("请先使用配置功能创建配置文件：")
        return

    if args.command == "wizard":
        run_wizard()
    elif args.command == "status":
        show_status()
    elif args.command == "test-rag":
        test_rag()
    elif args.command == "test-qdrant":
        test_qdrant()
    elif args.command == "history":
        if args.history_command == "stats":
            show_history_stats()
        elif args.history_command == "clear":
            if args.all:
                clear_history_all(args.include_pinned)
            elif args.chat_id:
                clear_history_chat(args.chat_id, args.platform, args.include_pinned)
            else:
                print("请指定 --all 或 --chat-id")
        else:
            history_menu()
    else:
        main_menu()

if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as e:
        # 兜底：防止遗漏路径触发原始异常堆栈
        if "config.yml" in str(e):
            print("⚠️ 未检测到配置文件 config.yml。")
            print("请先使用配置功能创建配置文件：")
            print("  python cli.py wizard")
        else:
            raise
