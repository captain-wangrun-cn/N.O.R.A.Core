# N.O.R.A. Core - 交互式配置向导 (i18n YAML 版)
import questionary
import os
import yaml
import google.generativeai as genai
import openai
from typing import Dict, Any

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
    with open(locale_file, 'r') as f:
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

        # Provider
        prov_def = embed_cfg.get('provider', 'siliconflow')
        provider = questionary.text(t('wizard.embed_provider_prompt'), default=prov_def).ask()
        if provider is None: return False

        # Base URL
        url_def = embed_cfg.get('base_url', 'https://api.siliconflow.cn/v1')
        base_url = questionary.text(t('wizard.embed_base_url_prompt'), default=url_def).ask()
        if base_url is None: return False

        # API Key
        key_def = embed_cfg.get('api_key', '')
        api_key = questionary.password(t('wizard.embed_key_prompt'), default=key_def).ask()
        if api_key is None: return False

        # Model
        model_def = embed_cfg.get('model', 'BAAI/bge-m3')
        model = questionary.text(t('wizard.embed_model_prompt'), default=model_def).ask()
        if model is None: return False

        # Save
        if 'memory' not in self.state: self.state['memory'] = {}
        self.state['memory']['embedding'] = {
            'provider': provider,
            'base_url': base_url,
            'api_key': api_key,
            'model': model,
            'dimensions': 1024 # default BGE-M3
        }
        return True

class StepModels(ConfigStep):
    def select_model(self, model_list, role_key):
        role_name = t(f'roles.{role_key}')
        current_models = self.state.get('models', {})
        default_model = current_models.get(role_key) # Get existing value
        
        if not model_list:
            return questionary.text(t('wizard.manual_prompt', role=role_name), default=default_model or "").ask()
        
        choices = model_list + [questionary.Separator(), t('wizard.manual_entry')]
        
        # Ensure default exists in choices for Questionary to select it
        kwargs = {}
        if default_model and default_model in model_list:
            kwargs['default'] = default_model

        selection = questionary.select(
            t('wizard.model_select_prompt', role=role_name),
            choices=choices,
            **kwargs
        ).ask()
        
        if selection == t('wizard.manual_entry'):
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
        models['smart'] = self.select_model(model_list, 'smart')
        models['fast'] = self.select_model(model_list, 'fast')
        models['coder'] = self.select_model(model_list, 'coder')
        self.state['models'] = models
        return True

# --- 主流程 ---
def run_wizard():
    load_locale()
    print(t('wizard.welcome'))

    # 加载现有配置
    config_file = "config.yml"
    state = {}
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            cfg = yaml.safe_load(f) or {}
            state = {
                'telegram_token': cfg.get("telegram", {}).get("bot_token"),
                'provider': cfg.get("llm", {}).get("provider"),
                'gemini_key': cfg.get("llm", {}).get("api_keys", {}).get("gemini"),
                'openai_key': cfg.get("llm", {}).get("api_keys", {}).get("openai"),
                'models': cfg.get("llm", {}).get("models", {}),
                'memory': cfg.get("memory", {})
            }

    steps = [StepTelegram, StepProvider, StepAPIKeys, StepDatabase, StepEmbedding, StepModels]
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
        'telegram': {'bot_token': state.get('telegram_token')},
        'llm': {
            'provider': state.get('provider'),
            'api_keys': {
                'gemini': state.get('gemini_key', ''),
                'openai': state.get('openai_key', '')
            },
            'models': state.get('models', {})
        },
        'memory': state.get('memory', {})
    }

    print(t('wizard.summary_title'))
    print(yaml.dump(final_config, indent=2, allow_unicode=True))

    if questionary.confirm(t('wizard.save_confirm')).ask():
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(final_config, f, indent=2, allow_unicode=True)
        print(t('wizard.success_message', file=config_file))
    else:
        print(t('wizard.aborted_message'))

if __name__ == "__main__":
    run_wizard()
