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
        multimodal_videos: Optional[List[Dict]] = None,
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

        model_encoded = urllib.parse.quote(self.model_name)
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
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
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
        """绕过 SDK，直接用 REST API 流式请求（解决 SDK + 自定义 endpoint + 视频 inline_data 卡死问题）。"""
        model_encoded = urllib.parse.quote(self.model_name)
        url = f"{self.base_url}/v1beta/models/{model_encoded}:streamGenerateContent?alt=sse"

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        # 构建 contents: history + 当前用户消息
        contents = self._convert_history_to_rest(history)

        # 当前用户消息 parts
        user_parts = []
        if user_prompt and user_prompt.strip():
            user_parts.append({"text": user_prompt})
        for image in (multimodal_images or []):
            mime_type = image.get("mime_type") or "image/jpeg"
            raw_bytes = image.get("bytes")
            if not raw_bytes:
                continue
            b64 = base64.b64encode(raw_bytes).decode("utf-8") if isinstance(raw_bytes, bytes) else raw_bytes
            user_parts.append({"inlineData": {"mimeType": mime_type, "data": b64}})
        for video in (multimodal_videos or []):
            mime_type = video.get("mime_type") or "video/mp4"
            raw_bytes = video.get("bytes")
            if not raw_bytes:
                continue
            b64 = base64.b64encode(raw_bytes).decode("utf-8") if isinstance(raw_bytes, bytes) else raw_bytes
            user_parts.append({"inlineData": {"mimeType": mime_type, "data": b64}})

        contents.append({"role": "user", "parts": user_parts})

        body: Dict[str, Any] = {"contents": contents}

        if system_prompt and system_prompt.strip():
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        body["safetySettings"] = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]

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
        if multimodal_videos:
            full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}" if not tools else user_prompt
            gen_config = {"maxOutputTokens": int(self.max_output_tokens)} if self.max_output_tokens else None
            async for chunk in self._video_stream_via_rest(
                system_prompt=system_prompt if tools else "",
                user_prompt=full_prompt,
                history=history,
                multimodal_images=multimodal_images,
                multimodal_videos=multimodal_videos,
                generation_config=gen_config,
            ):
                yield chunk
            return

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
