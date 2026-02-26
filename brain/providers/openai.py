import openai
from typing import List, Dict, Optional

from brain.interface import BaseLLM
import config

class OpenAIProvider(BaseLLM):
    """LLM Provider for OpenAI models."""

    def __init__(self, model_alias: str = "smart"):
        super().__init__()  # 初始化 BaseLLM
        if not config.get_api_key("openai"):
            raise ValueError("OPENAI_API_KEY is not set in the config.")
        
        self.model_alias = model_alias
        self.client = openai.AsyncOpenAI(
            api_key=config.get_api_key("openai"),
            base_url=config.get_base_url()
        )
        self.model = config.get_model_name(model_alias)
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

    async def chat(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]], tools: Optional[List[Dict]] = None) -> str:
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
            # 保存 usage 信息
            if response.usage:
                self.last_usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens
                }
            return response.choices[0].message.content or ""
        except Exception as e:
            print(f"OpenAI API Error: {e}")
            return "Sorry, I encountered an issue processing your request with the OpenAI API."

    async def chat_stream(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]], tools: Optional[List[Dict]] = None):
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True}  # 请求 OpenAI 在流中返回 usage
            )
            
            input_tokens = 0
            output_tokens = 0
            
            async for chunk in stream:
                # OpenAI 的 usage 信息在最后一个 chunk 中
                if hasattr(chunk, 'usage') and chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
                
                content = chunk.choices[0].delta.content if chunk.choices else None
                if content:
                    yield {"type": "text", "content": content}
            
            # 保存并返回 usage 信息
            if input_tokens or output_tokens:
                self.last_usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
                yield {
                    "type": "usage",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
                    
        except Exception as e:
            print(f"OpenAI API Stream Error: {e}")
            yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
