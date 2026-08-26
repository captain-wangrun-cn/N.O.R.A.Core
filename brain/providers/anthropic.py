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
        self.effort = config.get_llm_effort(model_alias)
        self.effort_budget_tokens = config.get_llm_effort_budget_tokens(model_alias)
        self.temperature = config.get_llm_temperature(model_alias)
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

        base_url = config.get_base_url(provider_name)
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**client_kwargs)

    # 走 output_config.effort 新写法的模型（2026 架构）。旧的
    # thinking={"type":"enabled","budget_tokens":N} 在这些模型上是 **400**，不是降级。
    # 用子串匹配而不是精确列表：模型名带日期后缀（claude-opus-4-7-20260115）且会持续
    # 出新版本，精确列表必然过期，过期的后果是新模型走回旧写法直接报错。
    _EFFORT_NATIVE_MODEL_MARKERS = (
        "opus-4-6", "opus-4.6", "opus-4-7", "opus-4.7", "opus-4-8", "opus-4.8",
        "sonnet-4-6", "sonnet-4.6",
        "sonnet-5", "opus-5", "fable-5", "mythos-5",
    )

    def _supports_native_effort(self) -> bool:
        """该模型是否吃 output_config.effort（而不是 thinking.budget_tokens）。"""
        model = str(getattr(self, "model", "") or "").lower().replace("_", "-")
        return any(marker in model for marker in self._EFFORT_NATIVE_MODEL_MARKERS)

    def _apply_effort(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """把推理强度翻译成 Anthropic 的思考配置。

        Anthropic 2026 年把这块拆成了**两个字段**，别混：
          `thinking`             = 要不要思考 / 什么模式（enabled / adaptive / disabled）
          `output_config.effort` = 思考多少（low/medium/high/xhigh/max）← 顶层兄弟字段
        effort 档位字符串**不在 thinking 里面**。而且 `adaptive` 是模式名不是档位名，
        官方文档明确写了不要拿它当 effort 值传。

        新旧模型走两条路，走错就是 400：
          4.6+ / sonnet-5 / opus-5 / fable-5 → output_config={"effort":...}
          4.5 及更早                          → thinking={"type":"enabled","budget_tokens":N}
        Opus 4.7+ 收到 `{"type":"enabled"}` 会回
        `thinking.type.enabled is not supported for this model`。

        另外两条硬约束：
          - Opus 5 上 `{"type":"disabled"}` 只在 effort <= high 时合法，xhigh/max 会 400；
            fable-5 / mythos-5 连 disabled 都不收（思考关不掉）。所以 none 档在这些模型上
            只降到 low，不发 disabled。
          - `budget_tokens` 必须 < `max_tokens`，且最小 1024。

        用 getattr 取值：effort 是纯可选调优字段，绕过 __init__ 构造的实例
        （测试里的 `object.__new__`）不该因为少这一个属性就整个请求崩掉。
        """
        effort = getattr(self, "effort", None)
        if effort is None:
            return payload

        if self._supports_native_effort():
            return self._apply_native_effort(payload, effort)
        return self._apply_budget_effort(payload, effort)

    def _apply_native_effort(self, payload: Dict[str, Any], effort: str) -> Dict[str, Any]:
        """4.6+ 模型：output_config.effort（+ thinking 模式）。"""
        if effort == config.EFFORT_AUTO:
            # auto = 让模型自己决定思考多少，这正是 adaptive 模式的语义；
            # 不带 effort，省得又把档位钉死。
            payload["thinking"] = {"type": "adaptive"}
            return payload

        if effort == config.EFFORT_NONE:
            # 关思考。但 fable-5 / mythos-5 关不掉（disabled 也 400），退而求其次给最低档。
            model = str(getattr(self, "model", "") or "").lower().replace("_", "-")
            if "fable-5" in model or "mythos-5" in model:
                payload["output_config"] = {"effort": "low"}
                return payload
            payload["thinking"] = {"type": "disabled"}
            return payload

        # minimal 是 OpenAI 的档，Anthropic 最低是 low。
        native = "low" if effort == "minimal" else effort
        payload["output_config"] = {"effort": native}
        return payload

    def _apply_temperature(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """把采样温度写进请求体。

        ⚠️ Anthropic 的硬约束：开启思考（thinking=enabled，或新模型的 output_config.effort）
        时 temperature 只能是 1，传别的值直接 400。所以这里只在**没有激活思考**时才下发
        temperature——思考态下采样温度本就由端点固定，跳过既安全又符合语义。
        （thinking=disabled 或压根没配思考时，temperature 正常下发。）

        用 getattr 取值：绕过 __init__ 的实例少这属性也不该崩。
        """
        temperature = getattr(self, "temperature", None)
        if temperature is None:
            return payload
        thinking = payload.get("thinking")
        reasoning_active = ("output_config" in payload) or (
            isinstance(thinking, dict) and thinking.get("type") == "enabled"
        )
        if not reasoning_active:
            payload["temperature"] = float(temperature)
        return payload

    def _apply_budget_effort(self, payload: Dict[str, Any], effort: str) -> Dict[str, Any]:
        """4.5 及更早：thinking.budget_tokens。"""
        budget = getattr(self, "effort_budget_tokens", None)
        if budget is None:
            return payload

        if budget < 0:
            # auto 档的 -1 是 Gemini 的 dynamic 哨兵，Anthropic 旧模型没有等价物，
            # 塞进 budget_tokens 会报错。旧模型的"自动"就是不传，用端点默认。
            return payload

        max_tokens = int(payload.get("max_tokens") or self.max_output_tokens or 1024)
        if budget == 0:
            payload["thinking"] = {"type": "disabled"}
            return payload

        # 留出至少 512 token 给正文，否则思考吃满预算后没有余量输出答案。
        usable = max(1024, min(int(budget), max_tokens - 512))
        if usable >= max_tokens:
            # max_tokens 太小，塞不进任何合法预算，直接不开思考。
            logger.warning(
                f"[Anthropic/{self.model_alias}] max_tokens={max_tokens} 太小，"
                f"无法容纳 effort={effort} 的思考预算，本次不启用 thinking。"
            )
            return payload
        payload["thinking"] = {"type": "enabled", "budget_tokens": usable}
        return payload

    @staticmethod
    def _is_effort_rejection(error: Exception) -> bool:
        """异常是否由思考配置字段引起（用于摘掉这些字段重试一次）。

        模型代次判断是靠模型名子串猜的，一定会有猜错的时候（自定义网关改名、
        新模型没进列表）。所以除了猜，还得有兜底：报错指向 thinking / effort /
        output_config 时就摘掉这些字段重试，别让一个可选调优字段废掉整个别名。
        """
        msg = str(error).lower()
        field_words = ("thinking", "output_config", "effort", "budget_tokens")
        rejection_words = ("unsupported", "not supported", "invalid", "unknown", "unrecognized")
        return any(f in msg for f in field_words) and any(r in msg for r in rejection_words)

    def _strip_effort_fields(self, payload: Dict[str, Any], error: Exception, context: str) -> bool:
        """就地摘掉 payload 里的思考配置字段。返回 False 表示这个错不该重试。"""
        if not self._is_effort_rejection(error):
            return False
        if "thinking" not in payload and "output_config" not in payload:
            return False
        logger.warning(
            f"[Anthropic {context}/{self.model_alias}] model={self.model} 不接受思考配置"
            f"（thinking={payload.get('thinking')!r} output_config={payload.get('output_config')!r}），"
            f"去掉这些字段重试: {error}"
        )
        payload.pop("thinking", None)
        payload.pop("output_config", None)
        return True

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
        multimodal_videos: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        self._begin_call()
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
        self._apply_effort(payload)
        self._apply_temperature(payload)
        if tools:
            payload["tools"] = self._convert_tools_schema(tools)
        try:
            try:
                response = await self.client.messages.create(**payload)
            except Exception as first_error:
                # 代次判断靠模型名子串猜，猜错就是 400。摘掉思考配置重试一次，
                # 否则一个可选调优字段会让整个别名不可用。
                if not self._strip_effort_fields(payload, first_error, "chat"):
                    raise
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

            # Anthropic 用 stop_reason 而非 finish_reason，语义对齐后走同一套检查。
            err = self.check_finish_reason_and_log(
                getattr(response, "stop_reason", None),
                response,
                "".join(texts),
                context=f"Anthropic chat/{self.model_alias}",
            )
            if err:
                return err

            cleaned = self.strip_think_content("".join(texts))
            if not cleaned.strip():
                logger.error(
                    f"[Anthropic chat/{self.model_alias}] 返回内容为空"
                    f"（stop_reason={getattr(response, 'stop_reason', None)}，usage={self.last_usage}），"
                    f"原始 content: {repr(getattr(response, 'content', None))[:800]}"
                )
                self.last_error = "empty_content"
            return cleaned
        except Exception as e:
            logger.error(f"Anthropic API Error: {e}", exc_info=True)
            return self._fail(
                "Sorry, I encountered an issue processing your request with the Anthropic API.",
                reason=f"exception:{e}",
            )

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
        multimodal_videos: Optional[List[Dict[str, Any]]] = None,
    ):
        self._begin_call()
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
        self._apply_effort(payload)
        self._apply_temperature(payload)
        if tools:
            payload["tools"] = self._convert_tools_schema(tools)

        in_think_block = False
        input_tokens = 0
        output_tokens = 0
        tool_name = ""
        tool_input_json = ""
        # 摘掉思考配置的重试只允许发生在**一个 yield 都还没发出去之前**。
        # 400 是在 __aenter__ 建流那一刻就抛的，所以正常情况下这里必然是 False；
        # 但如果是流中途断的，重试会把已经发给用户的文本再发一遍——宁可放弃重试。
        emitted_any = False
        for attempt in range(2):
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
                                    emitted_any = True
                                    yield {"type": "text", "content": visible_text}
                            elif delta_type == "input_json_delta":
                                tool_input_json += getattr(delta, "partial_json", "") or ""
                        elif event_type == "content_block_stop" and tool_name:
                            try:
                                parsed_args = json.loads(tool_input_json) if tool_input_json else {}
                            except Exception:
                                parsed_args = {}
                            emitted_any = True
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
                return
            except Exception as e:
                if (
                    attempt == 0
                    and not emitted_any
                    and self._strip_effort_fields(payload, e, "chat_stream")
                ):
                    continue
                logger.error(f"Anthropic Stream Error: {e}", exc_info=True)
                self.last_error = f"stream_exception:{e}"
                yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
                return