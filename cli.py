#!/usr/bin/env python3
# N.O.R.A. Core - 命令行管理工具
import argparse
import questionary
import os
import yaml
import warnings
import logging
import sys

# Suppress warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import google.generativeai as genai
import openai
from typing import Dict, Any

# Configure logger for CLI operations
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

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
    return value.format(**kwargs)

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
        questionary.print(t('wizard.model_fetch_error', error=e), style="bold red").ask()
        return None

def get_openai_models(api_key: str):
    try:
        client = openai.OpenAI(api_key=api_key)
        models = sorted([model.id for model in client.models.list() if "gpt" in model.id.lower()])
        return models
    except Exception as e:
        questionary.print(t('wizard.model_fetch_error', error=e), style="bold red").ask()
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

class StepProvider(ConfigStep):
    def run(self):
        choices = ["gemini", "openai", t('wizard.back_option')]
        default_val = self.state.get("provider", "gemini")
        if default_val not in choices: default_val = "gemini"
        
        self.state['provider'] = questionary.select(
            t('wizard.provider_prompt'),
            choices=choices,
            default=default_val
        ).ask()
        return self.state['provider'] is not None

class StepAPIKeys(ConfigStep):
    def run(self):
        provider = self.state.get('provider')
        if provider == 'gemini':
            self.state['gemini_key'] = questionary.password(
                t('wizard.gemini_key_prompt'),
                default=self.state.get("gemini_key", "")
            ).ask()
            return self.state['gemini_key'] is not None
        elif provider == 'openai':
            self.state['openai_key'] = questionary.password(
                t('wizard.openai_key_prompt'),
                default=self.state.get("openai_key", "")
            ).ask()
            return self.state['openai_key'] is not None
        return False # Should not happen

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

        # Save
        if 'memory' not in self.state: self.state['memory'] = {}
        self.state['memory']['embedding'] = {
            'provider': provider,
            'base_url': base_url,
            'api_key': api_key,
            'model': model,
            'dimensions': 1536 # default for text-embedding-3-small
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
        provider = self.state.get('provider')
        api_key = self.state.get(f"{provider}_key")
        model_list = []

        if provider == 'gemini' and api_key:
            model_list = get_gemini_models(api_key)
        elif provider == 'openai' and api_key:
            model_list = get_openai_models(api_key)

        if model_list is None: # API call failed
            return False 

        models = self.state.get('models', {})
        models['smart'] = self.select_model(model_list, 'smart', provider)
        models['fast'] = self.select_model(model_list, 'fast', provider)
        models['coder'] = self.select_model(model_list, 'coder', provider)
        models['summary'] = self.select_model(model_list, 'summary', provider)  # 添加总结模型
        self.state['models'] = models
        return True


class StepCostTracking(ConfigStep):
    """成本跟踪配置步骤"""
    
    def run(self):
        print("\n📊 成本跟踪配置")
        print("-" * 50)
        print("N.O.R.A. 可以自动跟踪 LLM 调用的 token 使用量和成本。")
        print("内置了 Gemini 和 OpenAI 的官方定价，也支持自定义价格。")
        
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
            "是否需要配置自定义模型价格？\n（如果使用官方 Gemini/OpenAI 模型，通常不需要）",
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
            state = {
                'telegram_token': cfg.get("telegram", {}).get("bot_token"),
                'workspace': cfg.get("workspace", {}),
                'provider': cfg.get("llm", {}).get("provider"),
                'gemini_key': cfg.get("llm", {}).get("api_keys", {}).get("gemini"),
                'openai_key': cfg.get("llm", {}).get("api_keys", {}).get("openai"),
                'models': cfg.get("llm", {}).get("models", {}),
                'memory': cfg.get("memory", {}),
                'tavily': cfg.get("tavily", {}),
                'cost_tracking': cfg.get("cost_tracking", {'enabled': True})
            }

    steps = [StepTelegram, StepWorkspace, StepProvider, StepAPIKeys, StepDatabase, StepEmbedding, StepTavily, StepTimezone, StepModels, StepCostTracking]
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
    final_config = {
        'workspace': state.get('workspace', {'root_path': '~/.nora/workspace'}),
        'telegram': {'bot_token': state.get('telegram_token')},
        'llm': {
            'provider': state.get('provider'),
            'api_keys': {
                'gemini': state.get('gemini_key', ''),
                'openai': state.get('openai_key', '')
            },
            'models': state.get('models', {})
        },
        'memory': state.get('memory', {}),
        'cost_tracking': state.get('cost_tracking', {'enabled': True})
    }
    
    # 添加 Tavily 配置（如果存在）
    if state.get('tavily'):
        final_config['tavily'] = state['tavily']

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
    """清理 Qdrant 向量数据库"""
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
        collection_name = "nora_memory"
        
        # 连接 Qdrant
        client = QdrantClient(host=host, port=port)
        
        # 检查 collection 是否存在
        collections = client.get_collections()
        exists = any(c.name == collection_name for c in collections.collections)
        
        if not exists:
            logger.warning(f"⚠️  Collection '{collection_name}' 不存在，无需清理。")
            return
        
        # 确认删除
        confirm = questionary.confirm(
            f"⚠️  即将删除 Collection '{collection_name}' 及其所有数据。确定继续？",
            default=False
        ).ask()
        
        if not confirm:
            logger.info("❌ 操作已取消。")
            return
        
        # 删除 collection
        client.delete_collection(collection_name=collection_name)
        logger.info(f"✅ Collection '{collection_name}' 已成功删除！")
        
        # 询问是否重新创建
        recreate = questionary.confirm(
            "是否重新创建空的 collection？",
            default=True
        ).ask()
        
        if recreate:
            from qdrant_client.http import models
            vector_size = mem_cfg.get('embedding', {}).get('dimensions', 1536)
            
            client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE
                )
            )
            logger.info(f"✅ Collection '{collection_name}' 已重新创建！")
        
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
        "仅 Qdrant 向量数据库",
        "仅 MongoDB 消息历史",
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
    if choice in ["仅 Qdrant 向量数据库", "全部清理（Qdrant + MongoDB）"]:
        clean_qdrant()
    
    if choice in ["仅 MongoDB 消息历史", "全部清理（Qdrant + MongoDB）"]:
        clean_mongodb()


def clean_mongodb():
    """清理 MongoDB 消息历史"""
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
        collection_name = "message_history"
        
        # 连接 MongoDB
        client = MongoClient(mongo_uri)
        db = client[db_name]
        collection = db[collection_name]
        
        # 获取当前文档数量
        count = collection.count_documents({})
        logger.info(f"📊 当前消息历史数量: {count}")
        
        if count == 0:
            logger.info("ℹ️  消息历史为空，无需清理。")
            return
        
        # 确认删除
        confirm = questionary.confirm(
            f"⚠️  即将删除 {count} 条消息历史记录。确定继续？",
            default=False
        ).ask()
        
        if not confirm:
            logger.info("❌ 操作已取消。")
            return
        
        # 删除所有文档
        result = collection.delete_many({})
        logger.info(f"✅ 已删除 {result.deleted_count} 条消息历史记录！")
        
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


# --- 主菜单 ---

def main_menu():
    """显示主菜单并处理用户选择"""
    load_locale()
    
    choices = [
        "🔧 运行配置向导",
        "🧪 测试 Qdrant 连接",
        "🧪 测试 RAG 系统",
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
        elif choice == "🧪 测试 Qdrant 连接":
            test_qdrant()
        elif choice == "🧪 测试 RAG 系统":
            test_rag()
        elif choice == "🧹 清理 RAG 数据":
            clean_rag()


# --- CLI 入口 ---

def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description="N.O.R.A. Core 管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python cli.py                    # 显示交互式主菜单
  python cli.py --configure        # 直接运行配置向导
  python cli.py --test-qdrant      # 测试 Qdrant 连接
  python cli.py --test-rag         # 测试 RAG 系统
  python cli.py --clean-rag        # 清理 RAG 数据
        """
    )
    
    parser.add_argument('--configure', action='store_true', help='运行配置向导')
    parser.add_argument('--test-qdrant', action='store_true', help='测试 Qdrant 连接')
    parser.add_argument('--test-rag', action='store_true', help='测试 RAG 系统')
    parser.add_argument('--clean-rag', action='store_true', help='清理 RAG 数据')
    
    args = parser.parse_args()
    
    # 如果没有指定任何参数，显示主菜单
    if not any(vars(args).values()):
        main_menu()
        return
    
    # 执行指定的操作
    if args.configure:
        run_wizard()
    elif args.test_qdrant:
        test_qdrant()
    elif args.test_rag:
        test_rag()
    elif args.clean_rag:
        clean_rag()


if __name__ == "__main__":
    main()
