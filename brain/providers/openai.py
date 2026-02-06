import openai
from typing import List, Dict

from brain.interface import BaseLLM
import config

class OpenAIProvider(BaseLLM):
    """LLM Provider for OpenAI models."""

    def __init__(self, model_alias: str = "smart"):
        if not config.get_api_key("openai"):
            raise ValueError("OPENAI_API_KEY is not set in the config.")
        
        self.client = openai.AsyncOpenAI(api_key=config.get_api_key("openai"))
        self.model = config.get_model_name(model_alias)
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

    async def chat(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]]) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"OpenAI API Error: {e}")
            return "Sorry, I encountered an issue processing your request with the OpenAI API."

    async def chat_stream(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]]):
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True
            )
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            print(f"OpenAI API Stream Error: {e}")
            yield "Sorry, an error occurred during streaming."
