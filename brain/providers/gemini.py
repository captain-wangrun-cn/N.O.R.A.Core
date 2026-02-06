import warnings

# Suppress the FutureWarning during import using a context manager
# This is the most aggressive way to silence warnings emitted at import time
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
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

    async def chat(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]], tools: List[Dict] = None) -> str:
        # Convert OpenAI-style history to Gemini's format
        gemini_history = []
        for item in history:
            role = "user" if item["role"] == "user" else "model"
            gemini_history.append({"role": role, "parts": [{"text": item["content"]}]})
        
        try:
            # Re-initialize model with tools if provided (Gemini defines tools at model level or chat level)
            # Efficient way: instantiate a lightweight tool-aware chat session just for this turn?
            # Or pass 'tools' to start_chat (if supported in latest SDK)
            
            # The google.generativeai SDK supports 'tools' in GenerativeModel constructor or start_chat?
            # Documentation says tools are passed to GenerativeModel.
            
            current_model = self.model
            if tools:
                # We need to adapt our generic schema to Gemini's specific tool format if necessary
                # But google.generativeai usually accepts a list of function declarations.
                # Here we assume 'tools' is compatible or we wrap it.
                # Actually, Gemini SDK takes 'tools' as a list of callables OR tool config.
                # Since we have JSON schemas, we might need to construct Tool objects.
                # LIMITATION: The current 'generativeai' python SDK prefers passing actual Python functions 
                # for automatic function calling, OR raw Tool objects.
                # Passing raw JSON schema (OpenAI style) is tricky in the old SDK.
                
                # Workaround: Since we are in 'Phase 3' and want robustness, 
                # let's try to pass the list of tool definitions directly if the SDK supports it.
                # If not, we might need a more complex adapter.
                
                # For now, let's assume we pass the raw tools list and hope the SDK handles the dicts 
                # (Newer versions do support OpenAI-compatible schemas in some contexts).
                # If this fails, we will need to refactor ToolManager to return native Gemini Tool objects.
                
                current_model = genai.GenerativeModel(
                    self.model.model_name,
                    tools=tools, # Pass tools here
                    system_instruction=system_prompt # System prompt is static per model usually
                )
            
            # Note: start_chat doesn't take system_prompt again if model has it.
            # But our previous code prepended it. Let's stick to the previous prompt strategy for now
            # unless we use system_instruction properly.
            
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}" if not tools else user_prompt
            # If we use native tools, system prompt should be in system_instruction (which we did above).
            
            chat_session = current_model.start_chat(history=gemini_history)
            response = await chat_session.send_message_async(full_prompt)
            
            # Check for function calls
            # Gemini SDK (automatic function calling) might execute it automatically if configured?
            # Or it returns a Part with function_call.
            
            # For simplicity in this step, we just return text. 
            # Real function calling loop needs to happen in Controller.
            # But wait, we need to return the function call request to the Controller!
            
            # If response contains function call, we need to return a special structure or parse it.
            # This 'chat' method returns 'str'. We might need to change return type or handle it internally?
            # But the Controller handles the loop.
            
            # Let's inspect response.parts[0].function_call
            if response.parts and response.parts[0].function_call:
                fc = response.parts[0].function_call
                # Return a special string or JSON indicating function call?
                # Or simply return the text representation if any.
                return f"[TOOL_CALL: {fc.name}({fc.args})]" 
            
            return response.text
        except Exception as e:
            # Handle potential content filtering and other API errors
            print(f"Gemini API Error: {e}")
            return "Sorry, I encountered an issue processing your request with the Gemini API."

    async def chat_stream(self, system_prompt: str, user_prompt: str, history: List[Dict[str, str]], tools: List[Dict] = None):
        gemini_history = []
        for item in history:
            role = "user" if item["role"] == "user" else "model"
            # Gemini history must be text-only for now, or adapt tool_response
            # For simplicity, we assume history is text. If we had tool outputs, we'd need more logic.
            gemini_history.append({"role": role, "parts": [{"text": str(item["content"])}]})

        # Configure model with tools if present
        current_model = self.model
        if tools:
            # Pass tools to model. Newer SDKs support OpenAI-style schemas directly or we rely on auto-conversion.
            # We assume tools is a list of schemas.
            current_model = genai.GenerativeModel(
                self.model.model_name,
                tools=tools,
                system_instruction=system_prompt
            )
            full_prompt = user_prompt # System prompt is in instruction
        else:
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        chat_session = current_model.start_chat(history=gemini_history)
        
        try:
            response_stream = await chat_session.send_message_async(full_prompt, stream=True)
            async for chunk in response_stream:
                # Check for function call
                if chunk.parts and chunk.parts[0].function_call:
                    fc = chunk.parts[0].function_call
                    # Yield a structured object for the controller to handle
                    yield {
                        "type": "tool_call",
                        "name": fc.name,
                        "args": dict(fc.args)
                    }
                else:
                    try:
                        if chunk.text:
                            yield {"type": "text", "content": chunk.text}
                    except ValueError:
                        pass
        except Exception as e:
            print(f"Gemini API Stream Error: {e}")
            yield {"type": "text", "content": f"[System Error: {str(e)}]"}
