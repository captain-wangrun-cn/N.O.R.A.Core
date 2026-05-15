import json
import logging
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from brain.interface import BaseLLM
import config

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseLLM):
    """LLM Provider for Anthropic models."""

    def __init__(self, model_alias: str = "smart", provider_name: Optional[str] = None):
        super().__init__()
        provider_name = provider_name or config.get_model_provider(model_alias)
        api_key = config.get_api_key(provider_name)
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set in the config.")

        self.model_alias = model_alias
        self.provider_name = provider_name
        self.model = config.get_model_name(model_alias)
        self.max_output_tokens = config.get_llm_max_output_tokens(model_alias) or 1024
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

    self.client = AsyncAnthropic(api_key=api_key)

    @staticmethod
    def _convert_tools_schema(tools: List[Dict]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        type_map = {
            "OBJECT": "object",
            "STRING": "string",
            "INTEGER": "integer",
            "NUMBER": "number",
            "BOOLEAN": "boolean",
            "ARRAY": "array",
        }

        def convert_schema(node: Any) -> Any:
            if isinstance(node, dict):
                out = {}
                for k, v in node.items():
                    if k == "type" and isinstance(v, str):
                        out[k] = type_map.get(v.upper(), v.lower())
                    else:
                        out[k] = convert_schema(v)
                return out
            if isinstance(node, list):
                return [convert_schema(x) for x in node]
            return node

        for tool in tools or []:
            if tool.get("type") == "function" and "function" in tool:
                fn = tool["function"]
                converted.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": convert_schema(fn.get("parameters", {})),
                })
            else:
                converted.append({
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "input_schema": convert_schema(tool.get("parameters", {})),
                })
        return converted

    @staticmethod
    def _convert_history(history: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        pending_tool_result: Optional[Dict[str, Any]] = None
        for item in history:
            role = item.get("role", "user")
            content = str(item.get("content", ""))
            if role == "tool_call":
                tool_name = item.get("tool_name", "unknown")
                tool_args = item.get("tool_args", {})
                converted.append({
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": f"tool_{len(converted)+1}",
                        "name": tool_name,
                        "input": tool_args if isinstance(tool_args, dict) else {},
                    }],
                })
            elif role == "tool_response":
                pending_tool_result = {
                    "type": "tool_result",
                    "tool_use_id": f"tool_{len(converted)}",
                    "content": content,
                }
                converted.append({"role": "user", "content": [pending_tool_result]})
            else:
                converted.append({
                    "role": role if role in ("user", "assistant") else "user",
                    "content": content,
                })
        return converted

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        messages = [
            *self._convert_history(history),
            {"role": "user", "content": user_prompt},
        ]
        payload: Dict[str, Any] = {
            "model": self.model,
            "system": system_prompt,
            "messages": messages,
            "max_tokens": int(self.max_output_tokens),
        }
        if tools:
            payload["tools"] = self._convert_tools_schema(tools)
        try:
            response = await self.client.messages.create(**payload)
            usage = getattr(response, "usage", None)
            if usage:
                self.last_usage = {
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                }
            texts: List[str] = []
            for block in getattr(response, "content", []) or []:
                if getattr(block, "type", "") == "text":
                    texts.append(getattr(block, "text", ""))
            return self.strip_think_content("".join(texts))
        except Exception as e:
            logger.error(f"Anthropic API Error: {e}", exc_info=True)
            return "Sorry, I encountered an issue processing your request with the Anthropic API."

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ):
        messages = [
            *self._convert_history(history),
            {"role": "user", "content": user_prompt},
        ]
        payload: Dict[str, Any] = {
            "model": self.model,
            "system": system_prompt,
            "messages": messages,
            "max_tokens": int(self.max_output_tokens),
        }
        if tools:
            payload["tools"] = self._convert_tools_schema(tools)

        in_think_block = False
        input_tokens = 0
        output_tokens = 0
        tool_name = ""
        tool_input_json = ""
        try:
            async with self.client.messages.stream(**payload) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", "") == "tool_use":
                            tool_name = getattr(block, "name", "")
                            tool_input_json = json.dumps(getattr(block, "input", {}) or {}, ensure_ascii=False)
                    elif event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", "")
                        if delta_type == "text_delta":
                            visible_text, in_think_block = self.strip_stream_think_segment(getattr(delta, "text", "") or "", in_think_block)
                            if visible_text:
                                yield {"type": "text", "content": visible_text}
                        elif delta_type == "input_json_delta":
                            tool_input_json += getattr(delta, "partial_json", "") or ""
                    elif event_type == "content_block_stop" and tool_name:
                        try:
                            parsed_args = json.loads(tool_input_json) if tool_input_json else {}
                        except Exception:
                            parsed_args = {}
                        yield {"type": "tool_call", "name": tool_name, "args": parsed_args}
                        tool_name = ""
                        tool_input_json = ""
                    elif event_type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            input_tokens = getattr(usage, "input_tokens", input_tokens) or input_tokens
                            output_tokens = getattr(usage, "output_tokens", output_tokens) or output_tokens
            if input_tokens or output_tokens:
                self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
                yield {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}
        except Exception as e:
            logger.error(f"Anthropic Stream Error: {e}", exc_info=True)
            yield {"type": "text", "content": "Sorry, an error occurred during streaming."}