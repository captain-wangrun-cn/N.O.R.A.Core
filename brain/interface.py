from abc import ABC, abstractmethod
from typing import Any, List, Dict, AsyncGenerator, Optional
import re

class BaseLLM(ABC):
    """Abstract Base Class for all LLM providers."""

    _THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
    _THINK_INLINE_PATTERN = re.compile(r"</?think>", re.IGNORECASE)
    
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

    async def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        文生图。默认不实现——只有具备 image-out 能力的 provider 覆盖它。

        Args:
            prompt: 生图提示词。
            reference_images: (Optional) 参考图列表，格式复用 multimodal_images
                （每项含 mime_type / bytes / base64），用于锚定形象一致性。

        Returns:
            {"bytes": bytes, "mime_type": str}

        Raises:
            NotImplementedError: provider 不支持文生图。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持文生图")

    @classmethod
    def strip_think_content(cls, text: str) -> str:
        """移除完整文本中的 think 标签及其内容。"""
        if not text:
            return ""
        cleaned = cls._THINK_BLOCK_PATTERN.sub("", text)
        cleaned = cls._THINK_INLINE_PATTERN.sub("", cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        return cleaned

    @classmethod
    def strip_stream_think_segment(cls, text: str, in_think_block: bool) -> tuple[str, bool]:
        """按流式片段清理 think 内容，支持跨 chunk 维持状态。"""
        if not text:
            return "", in_think_block

        lower_text = text.lower()
        out: List[str] = []
        i = 0

        while i < len(text):
            if in_think_block:
                close_idx = lower_text.find("</think>", i)
                if close_idx == -1:
                    return "".join(out), True
                i = close_idx + len("</think>")
                in_think_block = False
                continue

            open_idx = lower_text.find("<think>", i)
            close_idx = lower_text.find("</think>", i)

            if open_idx == -1 and close_idx == -1:
                out.append(text[i:])
                break

            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                out.append(text[i:close_idx])
                i = close_idx + len("</think>")
                continue

            if open_idx != -1:
                out.append(text[i:open_idx])
                i = open_idx + len("<think>")
                in_think_block = True
                continue

        return "".join(out), in_think_block
