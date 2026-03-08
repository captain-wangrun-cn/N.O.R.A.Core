from abc import ABC, abstractmethod
from typing import Any, List, Dict, AsyncGenerator, Optional

class BaseLLM(ABC):
    """Abstract Base Class for all LLM providers."""
    
    def __init__(self):
        """初始化 LLM，设置 usage 跟踪"""
        self.last_usage: Optional[Dict[str, int]] = None  # {"input_tokens": int, "output_tokens": int}

    @abstractmethod
    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Generates a chat response.

        Args:
            system_prompt: The initial system instruction.
            user_prompt: The latest user message.
            history: A list of previous turns in the conversation.
            tools: (Optional) A list of tool definitions (function declarations).
            multimodal_images: (Optional) 图片输入列表，每项包含 mime_type / bytes / base64 等字段。

        Returns:
            The generated text response from the LLM.
        """
        pass

    @abstractmethod
    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Generates a chat response as an asynchronous stream.

        Args:
            system_prompt: The initial system instruction.
            user_prompt: The latest user message.
            history: A list of previous turns in the conversation.
            tools: (Optional) A list of tool definitions.
            multimodal_images: (Optional) 图片输入列表，每项包含 mime_type / bytes / base64 等字段。

        Yields:
            Dict objects with "type" key ("text" or "tool_call" or "usage").
            Text chunks: {"type": "text", "content": str}
            Tool calls: {"type": "tool_call", "name": str, "args": dict}
            Usage info: {"type": "usage", "input_tokens": int, "output_tokens": int}
        """
        # This is an async generator, so it needs a yield statement.
        # The implementation in the provider will handle the actual yielding.
        if False:
            yield {}
    
    def get_last_usage(self) -> Optional[Dict[str, int]]:
        """获取最近一次调用的 token 使用量"""
        return self.last_usage
