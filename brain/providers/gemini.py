import warnings
import logging

# Suppress the FutureWarning during import using a context manager
# This is the most aggressive way to silence warnings emitted at import time
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import google.generativeai as genai

from typing import List, Dict, Optional, Any
import re
import random

from brain.interface import BaseLLM
import config

logger = logging.getLogger(__name__)

def _sanitize_links_for_safety(text: str) -> str:
    """
    在链接中插入随机空格以绕过内容安全过滤。
    例如: https://example.com -> https://ex ample.com
    """
    # 匹配 http:// 或 https:// 开头的链接
    url_pattern = r'(https?://[^\s]+)'
    
    def insert_random_space(match):
        url = match.group(1)
        # 如果链接太短，不处理
        if len(url) < 15:
            return url
        
        # 在域名后的路径中随机插入一个空格
        # 避免在协议部分插入
        protocol_end = url.find('://') + 3
        if protocol_end >= len(url) - 5:
            return url
        
        # 在协议之后的随机位置插入空格
        insert_pos = random.randint(protocol_end + 5, len(url) - 2)
        return url[:insert_pos] + ' ' + url[insert_pos:]
    
    return re.sub(url_pattern, insert_random_space, text)

class GeminiProvider(BaseLLM):
    """LLM Provider for Google Gemini models."""

    def __init__(self, model_alias: str = "smart", provider_name: Optional[str] = None):
        super().__init__()  # 初始化 BaseLLM
        provider_name = provider_name or config.get_model_provider(model_alias)
        if not config.get_api_key(provider_name):
            raise ValueError("GEMINI_API_KEY is not set in the config.")
        
        self.model_alias = model_alias
        self.provider_name = provider_name
        self.max_output_tokens = config.get_llm_max_output_tokens(model_alias)
        model_name = config.get_model_name(model_alias)
        if not model_name:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

        genai.configure(api_key=config.get_api_key(provider_name))
        self.model_name = model_name

        # 配置安全设置 - 放宽内容过滤以允许链接等内容
        from google.generativeai.types import HarmCategory, HarmBlockThreshold
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        self.model = genai.GenerativeModel(model_name, safety_settings=safety_settings)

    def _convert_history(self, history: List[Dict[str, str]]) -> list:
        """
        将 controller 的 temp_history（含 tool_call/tool_response）转换为 Gemini 原生格式。
        tool_call → model role + function_call part
        tool_response → user role + function_response part
        普通消息 → text part
        """
        gemini_history = []
        for item in history:
            role_raw = item.get("role", "user")
            content = str(item.get("content", ""))

            if role_raw == "tool_call":
                tool_name = item.get("tool_name", "unknown")
                tool_args = item.get("tool_args", {})
                try:
                    gemini_history.append({
                        "role": "model",
                        "parts": [genai.protos.Part(
                            function_call=genai.protos.FunctionCall(
                                name=tool_name,
                                args=tool_args
                            )
                        )]
                    })
                except Exception:
                    # Fallback: 如果 protos 不可用，用文本格式
                    gemini_history.append({
                        "role": "model",
                        "parts": [{"text": f"[Calling tool: {tool_name}]"}]
                    })
                continue
            elif role_raw == "tool_response":
                tool_name = item.get("tool_name", "unknown")
                try:
                    gemini_history.append({
                        "role": "user",
                        "parts": [genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name=tool_name,
                                response={"result": content}
                            )
                        )]
                    })
                except Exception:
                    # Fallback
                    gemini_history.append({
                        "role": "user",
                        "parts": [{"text": f"[Tool result for {tool_name}]\n{content}"}]
                    })
                continue

            # 普通文本消息
            role = "user" if role_raw == "user" else "model"
            gemini_history.append({"role": role, "parts": [{"text": content}]})
        
        return gemini_history

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict]] = None,
    ) -> str:
        # Convert history to Gemini's format, handling tool_call/tool_response
        gemini_history = self._convert_history(history)
        
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
                    self.model_name,
                    tools=tools, # Pass tools here
                    system_instruction=system_prompt,
                    safety_settings=safety_settings,
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
            
            return self.strip_think_content(response.text)
        except Exception as e:
            # Handle potential content filtering and other API errors
            error_msg = str(e)
            logger.error(f"Gemini API Error: {e}", exc_info=True)
            
            # 检查是否是内容过滤错误
            if "block_reason" in error_msg or "PROHIBITED_CONTENT" in error_msg or "SAFETY" in error_msg:
                return "抱歉，由于内容安全限制，我无法直接回复该消息。我理解您的问题，但系统检测到可能包含敏感内容（如链接）。您可以尝试重新表述问题，或者我可以用其他方式帮助您。"
            
            # 对于其他所有错误，返回一个包含具体错误信息的前缀消息
            return f"抱歉，处理您的请求时遇到了问题：{error_msg}"

    @staticmethod
    def _build_user_parts(user_prompt: str, multimodal_images: Optional[List[Dict]] = None) -> List[Dict]:
        """构建 Gemini message parts（text + inline_data）。"""
        if not multimodal_images:
            return [{"text": user_prompt}]

        parts: List[Dict] = []
        if user_prompt and user_prompt.strip():
            parts.append({"text": user_prompt})

        for image in multimodal_images:
            mime_type = image.get("mime_type") or "image/jpeg"
            data = image.get("bytes")
            if not data:
                continue
            parts.append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": data
                }
            })

        if not parts:
            parts.append({"text": user_prompt})
        return parts

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict]] = None,
    ):
        gemini_history = self._convert_history(history)

        # 配置安全设置
        from google.generativeai.types import HarmCategory, HarmBlockThreshold
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        # Configure model with tools if present
        current_model = self.model
        if tools:
            # Pass tools to model. Newer SDKs support OpenAI-style schemas directly or we rely on auto-conversion.
            # We assume tools is a list of schemas.
            current_model = genai.GenerativeModel(
                self.model_name,
                tools=tools,
                system_instruction=system_prompt,
                safety_settings=safety_settings
            )
            full_prompt = user_prompt # System prompt is in instruction
        else:
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        generation_config: Optional[Any] = None
        if self.max_output_tokens:
            generation_config = {"max_output_tokens": int(self.max_output_tokens)}

        user_parts = self._build_user_parts(full_prompt, multimodal_images)
        chat_session = current_model.start_chat(history=gemini_history)
        
        try:
            if generation_config:
                response_stream = await chat_session.send_message_async(
                    user_parts,
                    stream=True,
                    generation_config=generation_config
                )
            else:
                response_stream = await chat_session.send_message_async(
                    user_parts,
                    stream=True
                )
            
            # 收集 usage 数据
            input_tokens = 0
            output_tokens = 0
            in_think_block = False
            
            async for chunk in response_stream:
                # 尝试从 chunk 中获取 usage 信息（如果可用）
                if hasattr(chunk, 'usage_metadata'):
                    usage = chunk.usage_metadata
                    if hasattr(usage, 'prompt_token_count'):
                        input_tokens = usage.prompt_token_count
                    if hasattr(usage, 'candidates_token_count'):
                        output_tokens = usage.candidates_token_count
                
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
                            visible_text, in_think_block = self.strip_stream_think_segment(chunk.text, in_think_block)
                            if visible_text:
                                yield {"type": "text", "content": visible_text}
                    except ValueError:
                        pass
            
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
            error_msg = str(e)
            logger.error(f"Gemini API Stream Error: {e}", exc_info=True)
            
            # 检查是否是内容过滤错误
            if "block_reason" in error_msg or "PROHIBITED_CONTENT" in error_msg or "SAFETY" in error_msg:
                yield {"type": "text", "content": "抱歉，由于内容安全限制，我无法直接回复该消息。"}
                yield {"type": "text", "content": "我理解您的问题，但系统检测到可能包含敏感内容（如链接）。"}
                yield {"type": "text", "content": "您可以尝试重新表述问题，或者我可以用其他方式帮助您。"}
            else:
                yield {"type": "text", "content": f"抱歉，处理请求时遇到问题：{error_msg[:100]}"}
