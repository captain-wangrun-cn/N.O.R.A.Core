from abc import ABC, abstractmethod
from typing import List, Dict

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
                     Example: [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}]

        Returns:
            The generated text response from the LLM.
        """
        pass
