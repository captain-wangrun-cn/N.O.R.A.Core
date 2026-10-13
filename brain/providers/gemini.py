import warnings
import logging

# Suppress the FutureWarning during import using a context manager
# This is the most aggressive way to silence warnings emitted at import time
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import google.generativeai as genai

from typing import List, Dict, Optional, Any
import re
import json
import random
import base64
import urllib.parse

import asyncio
import aiohttp

from brain.interface import BaseLLM
import config

logger = logging.getLogger(__name__)


def _encode_model_for_url(model_name: str) -> str:
    """把模型名整段百分号编码，供 REST 路径占位使用。

    `urllib.parse.quote` 默认 `safe="/"`，会把模型名里的斜杠留在 URL 路径上。自建
    Gemini 网关按**路径段**解析模型名，于是 `vendor/model-variant` 这类带命名空间
    前缀的别名会被截成两段，后端解析出完全不同的供应商，报 `unknown provider for ...`
    ——**看起来像模型名配错，其实是编码漏了**。编码掉斜杠后，网关拿到的是还原后的
    原始别名，能正常路由。

    别名里的非 ASCII 字符（中文、方括号之类）编码与否都能过，统一编码更稳。
    """
    return urllib.parse.quote(str(model_name or ""), safe="")


def _gemini_safety_settings() -> Dict[str, Any]:
    """放宽内容过滤的四个阈值（BLOCK_NONE），构造 SDK/HarmCategory 形式的设置。

    历史坑：`chat()` 的 `if tools:` 分支引用过 `safety_settings`，但该方法内**从未
    定义过**这个名字——一旦带工具调 `chat()` 就是 NameError，被方法尾部那个宽
    `except` 吞掉，对外只表现为一句"处理请求时遇到了问题"。之所以一直没炸，只是
    因为 video 客户端的轮子固定 `no_tools=True`，这条分支从未被走到。
    **别再在方法里手拼这个 dict**，统一走这里；REST 那侧用 `_gemini_rest_safety_settings()`。
    """
    from google.generativeai.types import HarmCategory, HarmBlockThreshold
    return {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }


def _gemini_rest_safety_settings() -> List[Dict[str, str]]:
    """同上，但 REST body 要的是 camelCase 字面量列表。"""
    return [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]


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
        self.effort = config.get_llm_effort(model_alias)
        self.effort_budget_tokens = config.get_llm_effort_budget_tokens(model_alias)
        self.temperature = config.get_llm_temperature(model_alias)
        model_name = config.get_model_name(model_alias)
        if not model_name:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

        self.api_key = config.get_api_key(provider_name)
        self.base_url = config.get_base_url(provider_name) or "https://generativelanguage.googleapis.com"

        genai.configure(api_key=self.api_key)
        # 支持自定义 API endpoint（如代理地址）
        if config.get_base_url(provider_name):
            try:
                from google.api_core import client_options as client_options_lib
                genai.configure(
                    api_key=self.api_key,
                    client_options=client_options_lib.ClientOptions(api_endpoint=self.base_url),
                )
            except Exception:
                logger.warning(f"Gemini base_url 配置失败，使用默认 endpoint: {self.base_url}")

        self.model_name = model_name

        # 配置安全设置 - 放宽内容过滤以允许链接等内容
        self.model = genai.GenerativeModel(
            model_name, safety_settings=_gemini_safety_settings()
        )

    def _uses_thinking_level(self) -> bool:
        """该模型是否走 thinkingLevel 枚举（3.x）而不是 thinkingBudget（2.5）。

        两套系统互斥且不通用：对 3.x 传 thinkingBudget 报错，对 2.5 传 thinkingLevel
        也报错。用"是不是 2.x/1.x"反向判断，而不是枚举 3.x 型号——新模型只会往上出
        （3.7、4.0…），枚举必然过期，而过期的后果是新模型直接 400。
        """
        model = str(getattr(self, "model_name", "") or "").lower()
        legacy_markers = ("gemini-1.", "gemini-1_", "gemini-2.", "gemini-2_")
        return not any(marker in model for marker in legacy_markers)

    def _effort_generation_config(self, camel_case: bool = False) -> Dict[str, Any]:
        """把推理强度翻译成 Gemini 的 thinking 配置片段。

        Gemini 不吃 `reasoning_effort` 字符串，而且**两个代次要的东西不一样**：
          3.x → thinkingLevel 枚举字符串（MINIMAL/LOW/MEDIUM/HIGH）
          2.5 → thinkingBudget 整数（0=关闭，-1=dynamic，正数=预算上限）
        给 3.x 发 budget、给 2.5 发 level，两个方向都是报错。生产上 draw_desc 用的
        gemini-3.6-flash 就属于前者。

        3.x 关不掉思考，所以 none 档只降到 MINIMAL（见 config.EFFORT_THINKING_LEVELS）；
        Gemini 只有 4 档，xhigh/max 一起降到 HIGH。
        auto 档 → 3.x 不传该字段（用模型默认），2.5 用 thinkingBudget=-1（dynamic）。

        两套命名不能合并：Python SDK 的 generation_config 走 snake_case
        (`thinking_config` / `thinking_budget`)，而 `_video_stream_via_rest` 直接拼
        REST body，必须是 camelCase (`thinkingConfig` / `thinkingBudget`)。
        写错的一侧不会报错，只会被 API 静默忽略——档位看起来配了却没生效。

        用 getattr 取值：effort 是纯可选调优字段，绕过 __init__ 构造的实例
        （测试里的 `object.__new__`）不该因为少这一个属性就整个请求崩掉。
        """
        effort = getattr(self, "effort", None)
        if effort is None:
            return {}

        if self._uses_thinking_level():
            level = config.get_effort_thinking_level(effort)
            if not level:
                # auto：3.x 没有"动态预算"这个旋钮，不传就是用模型自己的默认档。
                return {}
            if camel_case:
                return {"thinkingConfig": {"thinkingLevel": level}}
            return {"thinking_config": {"thinking_level": level}}

        budget = getattr(self, "effort_budget_tokens", None)
        if budget is None:
            return {}
        if camel_case:
            return {"thinkingConfig": {"thinkingBudget": int(budget)}}
        return {"thinking_config": {"thinking_budget": int(budget)}}

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
        multimodal_videos: Optional[List[Dict]] = None,
    ) -> str:
        # Convert history to Gemini's format, handling tool_call/tool_response
        self._begin_call()

        # 多模态（尤其是视频）必须走 REST：SDK + 自定义 endpoint + inline_data 是
        # _video_stream_via_rest 当初要绕开的组合，非流式路径漏掉了这一条——带视频时
        # 会一路卡到 SDK 重试超时（600s），而且 chat() 不吐 chunk，日志从 Turn 1 起全静默。
        if multimodal_videos or multimodal_images:
            return await self._media_chat_via_rest(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=history,
                multimodal_images=multimodal_images,
                multimodal_videos=multimodal_videos,
            )

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
                    safety_settings=_gemini_safety_settings(),
                )
            
            # Note: start_chat doesn't take system_prompt again if model has it.
            # But our previous code prepended it. Let's stick to the previous prompt strategy for now
            # unless we use system_instruction properly.
            
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}" if not tools else user_prompt
            # If we use native tools, system prompt should be in system_instruction (which we did above).
            
            chat_session = current_model.start_chat(history=gemini_history)
            # chat() 没有 max_output_tokens 逻辑（历史如此），但 effort 要生效
            # 就必须走 generation_config；为空时不传，保持原行为。
            effort_config = self._effort_generation_config()
            if effort_config:
                response = await chat_session.send_message_async(
                    full_prompt, generation_config=effort_config
                )
            else:
                response = await chat_session.send_message_async(full_prompt)
            
            # 工具调用必须走 last_tool_calls 带回去：chat() 返回 str，这里过去把调用
            # 编码成 "[TOOL_CALL: name(args)]" 字面串，非流式下会被当成正文直接发给
            # 用户（流式路径是 yield {"type": "tool_call"}，两边语义要一致）。
            # 工具的**执行**仍由 controller 的工具循环负责，这里只负责如实上报请求。
            _calls: List[Dict[str, Any]] = []
            for _part in (response.parts or []):
                _fc = getattr(_part, "function_call", None)
                if _fc and getattr(_fc, "name", None):
                    _calls.append({
                        "name": _fc.name,
                        "args": dict(getattr(_fc, "args", None) or {}),
                        "id": None,
                    })
            if _calls:
                self.last_tool_calls = _calls
                # 纯工具调用时响应里没有 text part，直接取 response.text 会抛异常
                # （这正是原代码在此处提前 return 的原因），所以这里要兜住。
                try:
                    return self.strip_think_content(response.text)
                except Exception:
                    return ""

            # SDK 的 response.text 在被安全策略拦截时会抛异常或返回空，
            # 而 finishReason 才说得清是 SAFETY / RECITATION 还是别的。先查它。
            candidate = (getattr(response, "candidates", None) or [None])[0]
            err = self.check_finish_reason_and_log(
                getattr(candidate, "finish_reason", None) if candidate else None,
                getattr(response, "prompt_feedback", None) or response,
                getattr(response, "text", None),
                context=f"Gemini chat/{self.model_alias}",
            )
            if err:
                return err

            cleaned = self.strip_think_content(response.text)
            if not cleaned.strip():
                logger.error(
                    f"[Gemini chat/{self.model_alias}] 返回内容为空"
                    f"（finish_reason={getattr(candidate, 'finish_reason', None) if candidate else None}），"
                    f"promptFeedback={getattr(response, 'prompt_feedback', None)}，"
                    f"candidates={repr(getattr(response, 'candidates', None))[:600]}"
                )
                self.last_error = "empty_content"
            return cleaned
        except Exception as e:
            # Handle potential content filtering and other API errors
            error_msg = str(e)
            logger.error(f"Gemini API Error: {e}", exc_info=True)

            # 检查是否是内容过滤错误
            if "block_reason" in error_msg or "PROHIBITED_CONTENT" in error_msg or "SAFETY" in error_msg:
                return self._fail(
                    "抱歉，由于内容安全限制，我无法直接回复该消息。我理解您的问题，"
                    "但系统检测到可能包含敏感内容（如链接）。您可以尝试重新表述问题，"
                    "或者我可以用其他方式帮助您。",
                    reason=f"blocked_exception:{error_msg}",
                )

            # 对于其他所有错误，返回一个包含具体错误信息的前缀消息
            return self._fail(
                f"抱歉，处理您的请求时遇到了问题：{error_msg}",
                reason=f"exception:{error_msg}",
            )

    @staticmethod
    def _build_user_parts(user_prompt: str, multimodal_images: Optional[List[Dict]] = None, multimodal_videos: Optional[List[Dict]] = None) -> List[Dict]:
        """构建 Gemini message parts（text + inline_data for images/videos）。"""
        if not multimodal_images and not multimodal_videos:
            return [{"text": user_prompt}]

        parts: List[Dict] = []
        if user_prompt and user_prompt.strip():
            parts.append({"text": user_prompt})

        for image in (multimodal_images or []):
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

        for video in (multimodal_videos or []):
            mime_type = video.get("mime_type") or "video/mp4"
            data = video.get("bytes")
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

    def _media_parts(
        self,
        prompt: str,
        multimodal_images: Optional[List[Dict]] = None,
        multimodal_videos: Optional[List[Dict]] = None,
    ) -> List[Dict[str, Any]]:
        """拼多模态 user parts（text + 图片/视频 inlineData）。

        mime 一律取 payload 里的 `mime_type`（调用方 `brain/multimodal.py` 给的就是
        按魔数判出来的真实类型），兜底值只在 payload 缺字段时用。
        """
        parts: List[Dict[str, Any]] = []
        if prompt and prompt.strip():
            parts.append({"text": prompt})

        def _add(items: Optional[List[Dict]], default_mime: str) -> None:
            for item in (items or []):
                b64 = item.get("base64")
                if not b64:
                    raw = item.get("bytes")
                    if not raw:
                        continue
                    b64 = base64.b64encode(raw).decode("utf-8") if isinstance(raw, bytes) else raw
                parts.append({
                    "inlineData": {
                        "mimeType": item.get("mime_type") or default_mime,
                        "data": b64,
                    }
                })

        _add(multimodal_images, "image/jpeg")
        _add(multimodal_videos, "video/mp4")
        return parts

    async def _rest_generate_content(self, body: Dict[str, Any], label: str) -> Dict[str, Any]:
        """POST 一次 REST `:generateContent`，返回解析后的 payload。

        与 `generate_image` 同样的理由走 REST 而不是 SDK。不做重试：调用方（后脑
        工具循环 / 对话轮）自己有重试与降级，这里重试只会把一次慢失败放大成几次。
        """
        url = f"{self.base_url}/v1beta/models/{_encode_model_for_url(self.model_name)}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"{label} HTTP {resp.status}: {text[:500]}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"{label} 响应不是合法 JSON: {text[:300]}") from e

    def _record_rest_usage(self, payload: Dict[str, Any]) -> None:
        usage_meta = payload.get("usageMetadata") or {}
        if usage_meta:
            self.last_usage = {
                "input_tokens": usage_meta.get("promptTokenCount", 0) or 0,
                "output_tokens": (
                    usage_meta.get("candidatesTokenCount", 0)
                    or usage_meta.get("totalTokenCount", 0)
                    or 0
                ),
            }

    async def _media_chat_via_rest(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        multimodal_images: Optional[List[Dict]] = None,
        multimodal_videos: Optional[List[Dict]] = None,
    ) -> str:
        """带媒体的非流式回合：走 REST `:generateContent`。

        body 的拼装顺序与 `_video_stream_via_rest` 保持一致（history → 本轮 user parts，
        systemInstruction / safetySettings / generationConfig 与流式路径同款），
        这样同一个会话在流式与非流式之间切换时，模型看到的东西是一致的。
        """
        contents = self._convert_history_to_rest(history)
        contents.append({
            "role": "user",
            "parts": self._media_parts(user_prompt, multimodal_images, multimodal_videos),
        })
        body: Dict[str, Any] = {"contents": contents}

        if system_prompt and system_prompt.strip():
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        body["safetySettings"] = _gemini_rest_safety_settings()

        generation_config = self._effort_generation_config(camel_case=True)
        if generation_config:
            body["generationConfig"] = generation_config

        try:
            payload = await self._rest_generate_content(body, "Gemini 多模态非流式请求失败")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Gemini 多模态 REST 错误: {error_msg}", exc_info=True)
            if "PROHIBITED_CONTENT" in error_msg or "block_reason" in error_msg or "SAFETY" in error_msg:
                return self._fail(
                    "抱歉，由于内容安全限制，我无法直接回复该消息。",
                    reason=f"blocked_exception:{error_msg}",
                )
            return self._fail(
                f"抱歉，处理您的请求时遇到了问题：{error_msg[:300]}",
                reason=f"exception:{error_msg}",
            )

        self._record_rest_usage(payload)

        # 视频轮的 finishReason 要显式过一遍：SAFETY / RECITATION 在 REST payload 里
        # 只体现为 candidates[0].finishReason，直接取 text 会得到一段空串而不报错。
        candidates = payload.get("candidates") or []
        candidate = candidates[0] if candidates else None
        err = self.check_finish_reason_and_log(
            (candidate or {}).get("finishReason"),
            payload,
            None,
            context=f"Gemini media chat/{self.model_alias}",
        )
        if err:
            return err

        parts = ((candidate or {}).get("content") or {}).get("parts") or []
        text_parts: List[str] = []
        calls: List[Dict[str, Any]] = []
        for part in parts:
            if part.get("thought"):
                continue
            fc = part.get("functionCall")
            if fc and fc.get("name"):
                calls.append({"name": fc["name"], "args": fc.get("args") or {}, "id": None})
            elif part.get("text"):
                text_parts.append(part["text"])

        if calls:
            self.last_tool_calls = calls

        cleaned = self.strip_think_content("".join(text_parts))
        if not cleaned.strip() and not calls:
            logger.error(
                f"[Gemini media chat/{self.model_alias}] 返回内容为空"
                f"（finishReason={(candidate or {}).get('finishReason')}），"
                f"candidates={repr(candidates)[:600]}"
            )
            self.last_error = "empty_content"
        return cleaned

    async def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        文生图（Gemini image-out 模型，如 gemini-2.5-flash-image）。

        走 REST `:generateContent`——与 _video_stream_via_rest 同样的理由：
        SDK + 自定义 endpoint + inline_data 组合不可靠。

        参考图格式与 multimodal_images 一致（mime_type / bytes / base64），
        请求结构与普通多模态输入同构：图片作为 inline_data part 与文本并列。
        """
        if not prompt or not prompt.strip():
            raise ValueError("generate_image 需要非空 prompt")

        model_encoded = _encode_model_for_url(self.model_name)
        url = f"{self.base_url}/v1beta/models/{model_encoded}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        parts: List[Dict[str, Any]] = [{"text": prompt}]
        for ref in (reference_images or []):
            mime_type = ref.get("mime_type") or "image/png"
            b64 = ref.get("base64")
            if not b64:
                raw = ref.get("bytes")
                if not raw:
                    continue
                b64 = base64.b64encode(raw).decode("utf-8") if isinstance(raw, bytes) else raw
            parts.append({"inlineData": {"mimeType": mime_type, "data": b64}})

        body: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "safetySettings": _gemini_rest_safety_settings(),
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }

        timeout = aiohttp.ClientTimeout(total=180)
        payload: Optional[Dict[str, Any]] = None

        # 部分端点/模型不接受 responseModalities，400 时去掉该字段重试一次。
        for attempt in range(2):
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        payload = json.loads(text)
                        break
                    if resp.status == 400 and attempt == 0 and "generationConfig" in body:
                        logger.warning(
                            f"Gemini 生图 400，去掉 responseModalities 重试。响应: {text[:300]}"
                        )
                        body.pop("generationConfig", None)
                        continue
                    raise RuntimeError(
                        f"Gemini 生图失败 HTTP {resp.status}: {text[:500]}"
                    )

        if payload is None:
            raise RuntimeError("Gemini 生图未返回响应")

        usage_meta = payload.get("usageMetadata") or {}
        if usage_meta:
            self.last_usage = {
                "input_tokens": usage_meta.get("promptTokenCount", 0) or 0,
                "output_tokens": (
                    usage_meta.get("candidatesTokenCount", 0)
                    or usage_meta.get("totalTokenCount", 0)
                    or 0
                ),
            }

        candidates = payload.get("candidates") or []
        for candidate in candidates:
            for part in ((candidate.get("content") or {}).get("parts") or []):
                inline = part.get("inlineData") or part.get("inline_data")
                if not inline:
                    continue
                data = inline.get("data")
                if not data:
                    continue
                return {
                    "bytes": base64.b64decode(data),
                    "mime_type": inline.get("mimeType") or inline.get("mime_type") or "image/png",
                }

        # 没有图片：大概率是被安全策略拦了，或者模型只回了文本。
        finish_reason = (candidates[0].get("finishReason") if candidates else "") or ""
        feedback = payload.get("promptFeedback") or {}
        raise RuntimeError(
            f"Gemini 生图未返回图片数据 (finishReason={finish_reason}, promptFeedback={feedback})"
        )

    def _convert_history_to_rest(self, history: List[Dict[str, str]]) -> List[Dict]:
        """将 temp_history 转换为 REST API 格式的 contents 列表。"""
        contents = []
        for item in history:
            role_raw = item.get("role", "user")
            content = str(item.get("content", ""))

            if role_raw == "tool_call":
                tool_name = item.get("tool_name", "unknown")
                tool_args = item.get("tool_args", {})
                contents.append({
                    "role": "model",
                    "parts": [{"functionCall": {"name": tool_name, "args": tool_args}}]
                })
            elif role_raw == "tool_response":
                tool_name = item.get("tool_name", "unknown")
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": tool_name, "response": {"result": content}}}]
                })
            else:
                role = "user" if role_raw == "user" else "model"
                contents.append({"role": role, "parts": [{"text": content}]})
        return contents

    async def _video_stream_via_rest(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        multimodal_images: Optional[List[Dict]] = None,
        multimodal_videos: Optional[List[Dict]] = None,
        generation_config: Optional[Dict] = None,
    ):
        """绕过 SDK，直接用 REST API 流式请求（解决 SDK + 自定义 endpoint + 视频 inline_data 卡死问题）。

        非流式那一侧对应 `_media_chat_via_rest`——两边都必须绕开 SDK，只修一边等于
        没修：`/set_stream` 是 per-chat 的，同一个实例上两条路都会有人走。
        """
        model_encoded = _encode_model_for_url(self.model_name)
        url = f"{self.base_url}/v1beta/models/{model_encoded}:streamGenerateContent?alt=sse"

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        # 构建 contents: history + 当前用户消息
        contents = self._convert_history_to_rest(history)

        # 当前用户消息 parts（图片与视频共用同一套拼装，见 _media_parts）
        user_parts = self._media_parts(user_prompt, multimodal_images, multimodal_videos)

        contents.append({"role": "user", "parts": user_parts})

        body: Dict[str, Any] = {"contents": contents}

        if system_prompt and system_prompt.strip():
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        body["safetySettings"] = _gemini_rest_safety_settings()

        if generation_config:
            body["generationConfig"] = generation_config

        timeout = aiohttp.ClientTimeout(total=300)
        in_think_block = False
        input_tokens = 0
        output_tokens = 0
        max_retries = 3

        for _attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=headers, json=body) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            logger.error(f"Gemini REST API error {resp.status}: {error_text[:200]}")
                            yield {"type": "text", "content": f"抱歉，处理请求时遇到问题：HTTP {resp.status}"}
                            return

                        # 解析 SSE 流
                        buffer = ""
                        async for line in resp.content:
                            decoded = line.decode("utf-8", errors="replace")
                            buffer += decoded

                            while "\n" in buffer:
                                raw_line, buffer = buffer.split("\n", 1)
                                raw_line = raw_line.strip()

                                if not raw_line.startswith("data: "):
                                    continue
                                json_str = raw_line[6:]
                                if not json_str:
                                    continue

                                try:
                                    chunk_data = json.loads(json_str)
                                except json.JSONDecodeError:
                                    continue

                                # 提取 usage
                                usage_meta = chunk_data.get("usageMetadata") or {}
                                if usage_meta.get("promptTokenCount"):
                                    input_tokens = usage_meta["promptTokenCount"]
                                if usage_meta.get("candidatesTokenCount"):
                                    output_tokens = usage_meta["candidatesTokenCount"]

                                candidates = chunk_data.get("candidates") or []
                                for candidate in candidates:
                                    parts = (candidate.get("content") or {}).get("parts") or []
                                    for part in parts:
                                        # thinking 内容跳过
                                        if part.get("thought"):
                                            continue
                                        if "functionCall" in part:
                                            fc = part["functionCall"]
                                            yield {
                                                "type": "tool_call",
                                                "name": fc.get("name", ""),
                                                "args": fc.get("args", {})
                                            }
                                        elif "text" in part:
                                            text = part["text"]
                                            visible_text, in_think_block = self.strip_stream_think_segment(text, in_think_block)
                                            if visible_text:
                                                yield {"type": "text", "content": visible_text}

                if input_tokens or output_tokens:
                    self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
                    yield {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}
                return  # 成功，退出重试循环

            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                is_last = _attempt == max_retries - 1
                if is_last:
                    logger.error(f"Gemini REST video stream error (已重试 {max_retries} 次): {e}", exc_info=True)
                    yield {"type": "text", "content": f"抱歉，视频处理请求超时（已重试 {max_retries} 次）：{str(e)[:100]}"}
                    return
                wait = 2 ** _attempt
                logger.warning(f"Gemini REST video stream 超时/网络错误，{wait}s 后重试 ({_attempt + 1}/{max_retries}): {e}")
                await asyncio.sleep(wait)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Gemini REST video stream error: {e}", exc_info=True)
                if "block_reason" in error_msg or "PROHIBITED_CONTENT" in error_msg or "SAFETY" in error_msg:
                    yield {"type": "text", "content": "抱歉，由于内容安全限制，我无法直接回复该消息。"}
                else:
                    yield {"type": "text", "content": f"抱歉，处理请求时遇到问题：{error_msg[:100]}"}
                return

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict]] = None,
        multimodal_videos: Optional[List[Dict]] = None,
    ):
        # 视频请求绕过 SDK，直接走 REST API（SDK + 自定义 endpoint + 视频 inline_data 会卡死）
        self._begin_call()
        if multimodal_videos:
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}" if not tools else user_prompt
            # REST body 用 camelCase，和下面 SDK 分支的 snake_case 不是一回事。
            gen_config: Dict[str, Any] = {}
            if self.max_output_tokens:
                gen_config["maxOutputTokens"] = int(self.max_output_tokens)
            if self.temperature is not None:
                gen_config["temperature"] = float(self.temperature)
            gen_config.update(self._effort_generation_config(camel_case=True))
            async for chunk in self._video_stream_via_rest(
                system_prompt=system_prompt if tools else "",
                user_prompt=full_prompt,
                history=history,
                multimodal_images=multimodal_images,
                multimodal_videos=multimodal_videos,
                generation_config=gen_config or None,
            ):
                yield chunk
            return

        gemini_history = self._convert_history(history)

        # Configure model with tools if present
        current_model = self.model
        if tools:
            # Pass tools to model. Newer SDKs support OpenAI-style schemas directly or we rely on auto-conversion.
            # We assume tools is a list of schemas.
            current_model = genai.GenerativeModel(
                self.model_name,
                tools=tools,
                system_instruction=system_prompt,
                safety_settings=_gemini_safety_settings(),
            )
            full_prompt = user_prompt # System prompt is in instruction
        else:
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        generation_config: Dict[str, Any] = {}
        if self.max_output_tokens:
            generation_config["max_output_tokens"] = int(self.max_output_tokens)
        if self.temperature is not None:
            generation_config["temperature"] = float(self.temperature)
        # SDK 分支用 snake_case；REST 视频分支在上面用 camelCase。
        generation_config.update(self._effort_generation_config())

        user_parts = self._build_user_parts(full_prompt, multimodal_images, multimodal_videos)
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
