from brain.interface import BaseLLM
from brain.providers.gemini import GeminiProvider
from brain.providers.openai import OpenAIProvider
import config
from typing import Optional

def get_llm_client(provider_name: Optional[str] = None, model_alias: str = "smart") -> BaseLLM:
    """
    Factory function to get the configured LLM client for a specific model alias.
    """
    if provider_name is None:
        provider_name = config.get_model_provider(model_alias)
    else:
        # 显式指定 provider 时仍允许按 alias 覆盖
        provider_name = provider_name or config.get_model_provider(model_alias)
        
    provider_type = config.get_provider_type(provider_name)

    if provider_type == "gemini":
        return GeminiProvider(model_alias=model_alias, provider_name=provider_name)
    elif provider_type == "openai":
        # Assuming OpenAIProvider also takes model_alias
        return OpenAIProvider(model_alias=model_alias, provider_name=provider_name)
    else:
        raise ValueError(f"Unsupported LLM provider specified in config: '{provider_name}' ({provider_type})")

# Default client for easy import (uses the 'smart' model by default)
llm_client = get_llm_client()
