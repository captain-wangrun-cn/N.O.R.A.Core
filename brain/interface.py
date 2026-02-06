from abc import ABC, abstractmethod
from typing import List, Dict, AsyncGenerator

class BaseLLM(ABC):
    """Abstract Base Class for all LLM providers."""

    @abstractmethod
    async def chat(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]]) -> str:
        """
        Generates a chat response.

        Args:
            system_prompt: The initial system instruction.
            user_prompt: The latest user message.
            history: A list of previous turns in the conversation.

        Returns:
            The generated text response from the LLM.
        """
        pass

    @abstractmethod
    async def chat_stream(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
        """
        Generates a chat response as an asynchronous stream.

        Args:
            system_prompt: The initial system instruction.
            user_prompt: The latest user message.
            history: A list of previous turns in the conversation.

        Yields:
            A stream of text chunks from the LLM.
        """
        # This is an async generator, so it needs a yield statement.
        # The implementation in the provider will handle the actual yielding.
        yield
