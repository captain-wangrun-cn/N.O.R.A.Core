from brain.interface import BaseLLM
from brain.providers.gemini import GeminiProvider
from brain.providers.openai import OpenAIProvider
import config

def get_llm_client(provider_name: str = config.LLM_PROVIDER) -> BaseLLM:
    """
    Factory function to get the configured LLM client.
    This allows for flexible switching between different LLM backends.
    """
    if provider_name == "gemini":
        return GeminiProvider()
    elif provider_name == "openai":
        return OpenAIProvider()
    else:
        raise ValueError(f"Unsupported LLM provider specified in config: '{provider_name}'")

# Default client for easy import
llm_client = get_llm_client()
