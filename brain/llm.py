from brain.interface import BaseLLM
from brain.providers.gemini import GeminiProvider
from brain.providers.openai import OpenAIProvider
import config

def get_llm_client(provider_name: str = None, model_alias: str = "smart") -> BaseLLM:
    """
    Factory function to get the configured LLM client for a specific model alias.
    """
    if provider_name is None:
        provider_name = config.get_llm_provider()
        
    if provider_name == "gemini":
        return GeminiProvider(model_alias=model_alias)
    elif provider_name == "openai":
        # Assuming OpenAIProvider also takes model_alias
        return OpenAIProvider(model_alias=model_alias)
    else:
        raise ValueError(f"Unsupported LLM provider specified in config: '{provider_name}'")

# Default client for easy import (uses the 'smart' model by default)
llm_client = get_llm_client()
