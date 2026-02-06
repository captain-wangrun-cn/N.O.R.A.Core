import warnings
# Suppress the FutureWarning from google-generativeai library at the source
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import google.generativeai as genai
from typing import List, Dict

from brain.interface import BaseLLM
import config

class GeminiProvider(BaseLLM):
    """LLM Provider for Google Gemini models."""

    def __init__(self, model_alias: str = "smart"):
        if not config.get_api_key("gemini"):
            raise ValueError("GEMINI_API_KEY is not set in the config.")
        
        model_name = config.get_model_name(model_alias)
        if not model_name:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

        genai.configure(api_key=config.get_api_key("gemini"))
        self.model = genai.GenerativeModel(
            model_name,
            system_instruction="You are Nora, a helpful assistant." # Base instruction
        )

    async def chat(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]]) -> str:
        # Convert OpenAI-style history to Gemini's format
        gemini_history = []
        for item in history:
            role = "user" if item["role"] == "user" else "model"
            gemini_history.append({"role": role, "parts": [{"text": item["content"]}]})
        
        try:
            # Gemini prefers the system prompt to be part of the model's initialization
            # or combined with the first user message. We'll prepend it for consistency.
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
            
            chat_session = self.model.start_chat(history=gemini_history)
            response = await chat_session.send_message_async(full_prompt)
            return response.text
        except Exception as e:
            # Handle potential content filtering and other API errors
            print(f"Gemini API Error: {e}")
            return "Sorry, I encountered an issue processing your request with the Gemini API."

    async def chat_stream(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]]):
        gemini_history = []
        for item in history:
            role = "user" if item["role"] == "user" else "model"
            gemini_history.append({"role": role, "parts": [{"text": item["content"]}]})

        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
        chat_session = self.model.start_chat(history=gemini_history)
        
        try:
            response_stream = await chat_session.send_message_async(full_prompt, stream=True)
            async for chunk in response_stream:
                # Defensive check: Only yield text if the chunk actually has it.
                try:
                    if chunk.text:
                        yield chunk.text
                except ValueError:
                    # This handles cases where a chunk is valid but has no text part (e.g., safety blocks).
                    pass
        except Exception as e:
            print(f"Gemini API Stream Error: {e}")
            yield "Sorry, an error occurred during streaming."
