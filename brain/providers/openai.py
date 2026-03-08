import asyncio
import openai
import json
from typing import Any, List, Dict, Optional
from openai import APIConnectionError, APITimeoutError, RateLimitError, APIStatusError
import logging

from brain.interface import BaseLLM
import config

logger = logging.getLogger(__name__)

class OpenAIProvider(BaseLLM):
    """LLM Provider for OpenAI models."""

    def __init__(self, model_alias: str = "smart", provider_name: Optional[str] = None):
        super().__init__()  # 初始化 BaseLLM
        provider_name = provider_name or config.get_model_provider(model_alias)
        if not config.get_api_key(provider_name):
            raise ValueError("OPENAI_API_KEY is not set in the config.")
        
        self.model_alias = model_alias
        self.provider_name = provider_name
        # Optional custom User-Agent to mimic browser if needed
        ua = config.get_llm_user_agent(provider_name)
        default_headers = {"User-Agent": ua} if ua else None

        self.client = openai.AsyncOpenAI(
            api_key=config.get_api_key(provider_name),
            base_url=config.get_base_url(provider_name),
            default_headers=default_headers
        )
        self.model = config.get_model_name(model_alias)
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

    async def _with_retry(self, fn, max_retries: int = 3, base_delay: float = 1.0):
        """
        对可恢复错误做指数退避重试。
        仅在以下场景重试：连接/超时/限流/5xx。
        """
        attempt = 0
        while True:
            try:
                return await fn()
            except APIStatusError as e:
                status = getattr(e, "status_code", getattr(e, "status", None))
                if status not in ([429] + list(range(500, 600))):
                    raise
                attempt += 1
                if attempt > max_retries:
                    raise
                wait = base_delay * (2 ** (attempt - 1))
                logger.warning(f"OpenAI APIStatusError {status}, retry {attempt}/{max_retries} in {wait}s: {e}")
                await asyncio.sleep(wait)
            except (APIConnectionError, APITimeoutError, RateLimitError) as e:
                attempt += 1
                if attempt > max_retries:
                    raise
                wait = base_delay * (2 ** (attempt - 1))
                logger.warning(f"OpenAI transient error, retry {attempt}/{max_retries} in {wait}s: {e}")
                await asyncio.sleep(wait)

    def _convert_history(self, history: List[Dict[str, str]]) -> List[Dict]:
        """
        将 controller 的结构化 history（含 tool_call/tool_response）转换为 OpenAI 原生格式。
        
        OpenAI function calling 要求的 history 格式：
        1. assistant message with tool_calls field
        2. tool message with tool_call_id
        
        由于 controller 不保存 tool_call_id，我们用 synthetic ID 来对齐。
        """
        converted = []
        tool_call_counter = 0
        
        i = 0
        while i < len(history):
            item = history[i]
            role = item.get("role", "user")
            content = str(item.get("content", ""))
            
            if role == "tool_call":
                tool_name = item.get("tool_name", "unknown")
                tool_args = item.get("tool_args", {})
                tool_call_counter += 1
                synthetic_id = f"call_{tool_call_counter}"
                
                # OpenAI 格式: assistant message with tool_calls
                converted.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": synthetic_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_args, ensure_ascii=False) if isinstance(tool_args, dict) else str(tool_args)
                        }
                    }]
                })
                
                # 如果紧接着的是 tool_response，用同一个 id 关联
                if i + 1 < len(history) and history[i + 1].get("role") == "tool_response":
                    resp_item = history[i + 1]
                    converted.append({
                        "role": "tool",
                        "tool_call_id": synthetic_id,
                        "content": str(resp_item.get("content", ""))
                    })
                    i += 2
                    continue
                    
            elif role == "tool_response":
                # 孤立的 tool_response（没有配对的 tool_call），fallback 为 user
                tool_name = item.get("tool_name", "unknown")
                converted.append({
                    "role": "user",
                    "content": f"[Tool result for {tool_name}]\n{content}"
                })
            elif role in ("user", "assistant", "system"):
                converted.append({"role": role, "content": content})
            else:
                # 未知 role，作为 user
                converted.append({"role": "user", "content": content})
            
            i += 1
        
        return converted

    @staticmethod
    def _convert_tools_schema(tools: List[Dict]) -> List[Dict]:
        """
        将 ToolManager._generate_schema() 输出的 Gemini 格式 schema 
        转换为 OpenAI function calling 格式。
        
        Gemini 格式:
            {"name": "edit_file", "description": "...", "parameters": {"type": "OBJECT", "properties": {...}, "required": [...]}}
        
        OpenAI 格式:
            {"type": "function", "function": {"name": "edit_file", "description": "...", "parameters": {"type": "object", "properties": {...}, "required": [...]}}}
        """
        converted = []
        
        # Gemini → OpenAI 类型映射（大写 → 小写）
        type_map = {
            "OBJECT": "object",
            "STRING": "string",
            "INTEGER": "integer",
            "NUMBER": "number",
            "BOOLEAN": "boolean",
            "ARRAY": "array",
        }
        
        def convert_type(t: str) -> str:
            return type_map.get(t.upper(), t.lower()) if isinstance(t, str) else t
        
        def convert_parameters(params: dict) -> dict:
            """递归转换 parameters 中的类型字段。"""
            result = {}
            for key, value in params.items():
                if key == "type":
                    result[key] = convert_type(value)
                elif key == "properties" and isinstance(value, dict):
                    result[key] = {
                        prop_name: convert_parameters(prop_val) if isinstance(prop_val, dict) else prop_val
                        for prop_name, prop_val in value.items()
                    }
                else:
                    result[key] = value
            return result
        
        for tool in tools:
            # 如果已经是 OpenAI 格式，直接用
            if tool.get("type") == "function" and "function" in tool:
                converted.append(tool)
                continue
            
            # Gemini 格式 → OpenAI 格式
            name = tool.get("name", "")
            description = tool.get("description", "")
            parameters = tool.get("parameters", {})
            
            converted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": convert_parameters(parameters),
                }
            })
        
        return converted

    @staticmethod
    def _build_user_content(user_prompt: str, multimodal_images: Optional[List[Dict[str, Any]]] = None):
        """构建 OpenAI user content：纯文本或多模态 parts。"""
        if not multimodal_images:
            return user_prompt

        parts: List[Dict[str, Any]] = []
        if user_prompt and user_prompt.strip():
            parts.append({"type": "text", "text": user_prompt})

        for image in multimodal_images:
            mime_type = image.get("mime_type") or "image/jpeg"
            b64 = image.get("base64")
            if not b64:
                continue
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64}"
                }
            })

        if not parts:
            return user_prompt
        return parts
    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        非流式调用 OpenAI API（含 function calling 支持）。
        
        注意：chat() 用于 fast_llm 等不需要工具调用的场景，
        大部分工具调用走 chat_stream()。但仍完整实现以备需要。
        """
        filtered_history = self._convert_history(history)
        user_content = self._build_user_content(user_prompt, multimodal_images)
        messages = [
            {"role": "system", "content": system_prompt},
            *filtered_history,
            {"role": "user", "content": user_content}
        ]
        
        # 构建 API 请求参数
        kwargs = {
            "model": self.model,
            "messages": messages,
        }
        
        # 如果有 tools，转换格式并传入
        if tools:
            converted_tools = self._convert_tools_schema(tools)
            if converted_tools:
                kwargs["tools"] = converted_tools
        
        try:
            response = await self._with_retry(lambda: self.client.chat.completions.create(**kwargs))
            
            # 保存 usage 信息
            if response.usage:
                self.last_usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens
                }
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"OpenAI API Error: {e}", exc_info=True)
            return "Sorry, I encountered an issue processing your request with the OpenAI API."

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        流式调用 OpenAI API，完整支持 function calling。
        
        OpenAI 流式返回 tool_calls 的方式：
        - delta.tool_calls 是一个列表，每个元素有 index, id, function.name, function.arguments
        - arguments 会被分成多个 chunk 逐步发送
        - 需要累积同一个 index 的 arguments 直到流结束
        
        yield 的格式与 GeminiProvider 保持一致：
        - {"type": "text", "content": "..."}
        - {"type": "tool_call", "name": "...", "args": {...}}
        - {"type": "usage", "input_tokens": N, "output_tokens": N}
        """
        filtered_history = self._convert_history(history)
        user_content = self._build_user_content(user_prompt, multimodal_images)
        messages = [
            {"role": "system", "content": system_prompt},
            *filtered_history,
            {"role": "user", "content": user_content}
        ]
        
        # 构建 API 请求参数
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        
        # 如果有 tools，转换格式并传入
        if tools:
            converted_tools = self._convert_tools_schema(tools)
            if converted_tools:
                kwargs["tools"] = converted_tools
                logger.info(f"OpenAI chat_stream: passing {len(converted_tools)} tools to API (model={self.model})")
            else:
                logger.warning("OpenAI chat_stream: tools provided but conversion resulted in empty list")
        else:
            logger.debug("OpenAI chat_stream: no tools provided")
        
        # stream_options 不是所有 OpenAI 兼容 API 都支持，尝试传入
        kwargs["stream_options"] = {"include_usage": True}
        
        try:
            stream = await self._with_retry(lambda: self.client.chat.completions.create(**kwargs))
        except Exception as e:
            # 如果 stream_options 导致错误，去掉后重试
            if "stream_options" in str(e).lower() or "unrecognized" in str(e).lower():
                logger.warning(f"stream_options not supported, retrying without it: {e}")
                kwargs.pop("stream_options", None)
                try:
                    stream = await self._with_retry(lambda: self.client.chat.completions.create(**kwargs))
                except Exception as e2:
                    logger.error(f"OpenAI API Stream Error (retry): {e2}", exc_info=True)
                    yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
                    return
            else:
                logger.error(f"OpenAI API Stream Error: {e}", exc_info=True)
                yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
                return
        
        try:
            input_tokens = 0
            output_tokens = 0
            
            # 累积 tool_calls 的状态
            # key: tool_call index, value: {"id": str, "name": str, "arguments": str}
            pending_tool_calls: Dict[int, Dict[str, str]] = {}
            
            async for chunk in stream:
                # 收集 usage（最后一个 chunk 通常包含）
                if hasattr(chunk, 'usage') and chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0
                
                if not chunk.choices:
                    continue
                
                delta = chunk.choices[0].delta
                
                # === 处理文本内容 ===
                if delta.content:
                    yield {"type": "text", "content": delta.content}
                
                # === 处理 tool_calls 分片 ===
                if delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": ""
                            }
                        
                        # 累积各字段（OpenAI 分多个 chunk 发送）
                        if tc_chunk.id:
                            pending_tool_calls[idx]["id"] = tc_chunk.id
                        if tc_chunk.function:
                            if tc_chunk.function.name:
                                pending_tool_calls[idx]["name"] = tc_chunk.function.name
                            if tc_chunk.function.arguments:
                                pending_tool_calls[idx]["arguments"] += tc_chunk.function.arguments
            
            # === 流结束后，yield 所有累积完成的 tool_calls ===
            if pending_tool_calls:
                logger.info(f"OpenAI stream finished: {len(pending_tool_calls)} tool_call(s) accumulated")
            else:
                logger.debug("OpenAI stream finished: no tool_calls received from API")
            
            for idx in sorted(pending_tool_calls.keys()):
                tc = pending_tool_calls[idx]
                tool_name = tc["name"]
                raw_args = tc["arguments"]
                
                # 解析 arguments JSON
                try:
                    tool_args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse tool_call arguments for '{tool_name}': {raw_args[:200]}")
                    tool_args = {"raw": raw_args}
                
                logger.info(f"OpenAI tool_call detected: {tool_name}({tool_args})")
                yield {
                    "type": "tool_call",
                    "name": tool_name,
                    "args": tool_args
                }
            
            # === yield usage ===
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
            logger.error(f"OpenAI API Stream Error: {e}", exc_info=True)
            yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
