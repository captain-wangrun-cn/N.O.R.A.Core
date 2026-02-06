import openai
from typing import List, Dict

from brain.interface import BaseLLM
import config

class OpenAIProvider(BaseLLM):
    """LLM Provider for OpenAI models."""

    def __init__(self, model_name: str = config.MODEL_SMART):
        if not config.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set in the environment.")
        
        self.client = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        self.model = model_name

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

