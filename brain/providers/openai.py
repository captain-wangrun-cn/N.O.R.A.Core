import asyncio
import base64
import io
import openai
import json
import inspect
import re
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
        self.use_responses_api = bool(config.get_provider_option(provider_name, "use_responses_api"))
        self.model = config.get_model_name(model_alias)
        self.max_output_tokens = config.get_llm_max_output_tokens(model_alias)
        self.effort = config.get_llm_effort(model_alias)
        self.temperature = config.get_llm_temperature(model_alias)
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

    def _apply_effort(self, payload: Dict[str, Any], responses_api: bool = False) -> Dict[str, Any]:
        """把推理强度写进请求体（就地修改并返回，便于串在 payload 构造后）。

        ⚠️ 两套 API 的字段形态不一样，写错的一侧是 400 而不是静默忽略：
          Chat Completions → 平铺 `reasoning_effort="high"`
          Responses        → 嵌套 `reasoning={"effort":"high"}`
        平铺字段传给 Responses 会 `400 unsupported_parameter`，反之同理。
        `_filter_supported_kwargs` 只按 SDK 签名过滤顶层参数名，救不了这个——
        `reasoning` 本身是合法顶层参数，它不知道里面该放什么。

        `none` 也是合法值（关闭思考），所以判据是 `is None` 而不是真值——
        `if not self.effort` 会把 none 档吞掉。

        `auto` 是本项目自己的档位，OpenAI 没有这个字符串。它的语义就是"用端点默认"，
        所以翻译成**不传该字段**，直接返回。

        不支持推理的模型收到这个字段可能报 400；chat() / chat_stream() 的异常分支
        会用 `_is_effort_rejection()` 识别并去掉该字段重试一次。

        用 getattr 取值：effort 是纯可选调优字段，绕过 __init__ 构造的实例
        （测试里的 `object.__new__(OpenAIProvider)`）不该因为少这一个属性就整个请求崩掉。
        """
        effort = getattr(self, "effort", None)
        if effort is None or effort == config.EFFORT_AUTO:
            return payload
        if responses_api:
            payload["reasoning"] = {"effort": effort}
        else:
            payload["reasoning_effort"] = effort
        return payload

    def _apply_temperature(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """把采样温度写进请求体（Chat Completions 与 Responses 都是顶层 `temperature`）。

        None 表示不配置（用端点默认，不发字段）。不支持自定义温度的推理模型
        （o 系列 / GPT-5 等）收到该字段会 400——chat() / chat_stream() 的异常分支
        用 `_is_temperature_rejection()` 识别并摘掉该字段重试一次。

        用 getattr 取值：绕过 __init__ 的实例（测试里的 object.__new__）少这属性也不该崩。
        """
        temperature = getattr(self, "temperature", None)
        if temperature is not None:
            payload["temperature"] = float(temperature)
        return payload

    @staticmethod
    def _is_effort_rejection(error: Exception) -> bool:
        """判断异常是否由推理强度字段本身引起（用于去掉它重试一次）。

        各家兼容端点的报错措辞差别很大（unknown / unsupported / unrecognized /
        invalid parameter），所以只要错误里同时出现"推理"和"不认识这个参数"的意思
        就算命中。宁可多重试一次，也不要让一个可选调优字段废掉整个别名。

        也要认「值不被支持」这一类：OpenAI 的档位是 model-dependent 的，老模型收到
        xhigh/max 会回 `Unsupported value: 'xhigh' is not supported ... Supported
        values are: 'low', 'medium', and 'high'`——字段名对、值不对，同样得摘掉重试。
        """
        msg = str(error).lower()
        if "reasoning_effort" in msg or "reasoning.effort" in msg:
            return True
        rejection_words = ("unsupported", "unknown", "unrecognized", "invalid", "not supported")
        if "reasoning" in msg and any(word in msg for word in rejection_words):
            return True
        # 报错里可能只提值不提字段名，兜一层：出现我们发出去的档位名 + 拒绝措辞。
        effort_words = ("xhigh", "max", "minimal", "none")
        return any(word in msg for word in effort_words) and any(
            word in msg for word in rejection_words
        )

    @staticmethod
    def _is_temperature_rejection(error: Exception) -> bool:
        """判断异常是否由 temperature 字段本身引起（用于摘掉它重试一次）。

        推理模型的典型报错：`Unsupported value: 'temperature' does not support 0.55
        with this model. Only the default (1) value is supported.`——只要错误里同时
        提到 temperature 和"不支持/只支持默认值"这类措辞就算命中。
        """
        msg = str(error).lower()
        if "temperature" not in msg:
            return False
        rejection_words = (
            "unsupported", "unknown", "unrecognized", "invalid",
            "not supported", "does not support", "only the default",
        )
        return any(word in msg for word in rejection_words)

    @staticmethod
    def _convert_history_for_responses(history: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for item in history:
            role = item.get("role", "user")
            content = str(item.get("content", ""))
            if role == "tool_call":
                tool_name = item.get("tool_name", "unknown")
                tool_args = item.get("tool_args", {})
                converted.append({
                    "type": "function_call",
                    "name": tool_name,
                    "arguments": json.dumps(tool_args, ensure_ascii=False) if isinstance(tool_args, dict) else str(tool_args),
                    "call_id": f"hist_{len(converted)+1}",
                })
            elif role == "tool_response":
                converted.append({
                    "type": "function_call_output",
                    "call_id": f"hist_{len(converted)}",
                    "output": content,
                })
            else:
                converted.append({
                    "role": role if role in ("user", "assistant", "system") else "user",
                    "content": [{"type": "input_text", "text": content}],
                })
        return converted

    @staticmethod
    def _convert_tools_schema_for_responses(tools: List[Dict]) -> List[Dict[str, Any]]:
        converted = []
        for tool in OpenAIProvider._convert_tools_schema(tools):
            fn = tool.get("function", {})
            converted.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        return converted

    async def _responses_create(self, **kwargs):
        return await self._with_retry(lambda: self.client.responses.create(**kwargs))

    @staticmethod
    def _filter_supported_kwargs(callable_obj, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按目标 callable 的签名过滤 kwargs，兼容不同 OpenAI SDK 版本。"""
        try:
            sig = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return payload

        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
            return payload

        allowed = set(sig.parameters.keys())
        return {key: value for key, value in payload.items() if key in allowed}

    def _extract_response_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if output_text:
            return self.strip_think_content(output_text)
        chunks: List[str] = []
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    chunks.append(text)
        return self.strip_think_content("".join(chunks))

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
    def _build_user_content(user_prompt: str, multimodal_images: Optional[List[Dict[str, Any]]] = None, multimodal_videos: Optional[List[Dict[str, Any]]] = None):
        """构建 OpenAI user content：纯文本或多模态 parts。"""
        if not multimodal_images and not multimodal_videos:
            return user_prompt

        parts: List[Dict[str, Any]] = []
        if user_prompt and user_prompt.strip():
            parts.append({"type": "text", "text": user_prompt})

        for image in (multimodal_images or []):
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

        for video in (multimodal_videos or []):
            mime_type = video.get("mime_type") or "video/mp4"
            b64 = video.get("base64")
            if not b64:
                continue
            parts.append({
                "type": "video_url",
                "video_url": {
                    "url": f"data:{mime_type};base64,{b64}"
                }
            })

        if not parts:
            return user_prompt
        return parts

    _IMAGE_EXT_BY_MIME = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
    }

    async def _images_edit(self, prompt: str, files: List[Any]):
        """
        调 images.edit。多图签名（image=[f1, f2]）在部分 SDK 版本/兼容端点不被接受，
        失败时退回单图（image=f1）——参考图丢一部分也比整个生图失败好。
        """
        try:
            return await self.client.images.edit(model=self.model, image=files, prompt=prompt, n=1)
        except Exception as e:
            if len(files) == 1:
                raise
            logger.warning(
                f"images.edit 多图签名失败（{type(e).__name__}: {e}），退回单张参考图重试。",
                exc_info=True,
            )
            for f in files:
                try:
                    f.seek(0)
                except Exception:
                    pass
            return await self.client.images.edit(model=self.model, image=files[0], prompt=prompt, n=1)

    async def _images_edit_json(
        self,
        prompt: str,
        files: List[Any],
        fallback_form: bool = False,
    ):
        """
        以 application/json 调 images.edit——部分中转站把 /v1/images/edits 实现成
        只收 JSON（参考图以 base64 data URI 内嵌），SDK 的 multipart 会直接 415。

        body 形态经实测确定（wrapi / newapi 系）：参考图是**单个** `image` 对象
        `{"url": "data:image/png;base64,..."}`。其它写法都会被拒，且报错各不相同，
        改这里之前先照 docs 里记的实测结果确认：
          image[N]: "data:..."   → 400 image 不能为空（不认下标写法）
          image: "data:..."      → 422 expected struct ImageUrl（要对象不要裸串）
          image: [{"url": ...}]  → 422 invalid type: map（不吃数组）
        所以多张参考图只能取第一张——形象锚多传也传不进去，这是端点限制不是取舍。

        返回项是 `{"url", "mime_type"}`（不是 b64_json），所以下面两种都要认。
        - fallback_form=True：415 时自动退回 SDK multipart（见 generate_image）
        """
        ref_uri: Optional[str] = None
        for f in files:
            if not hasattr(f, "seek") or not hasattr(f, "read"):
                continue
            try:
                f.seek(0)
                raw_ref = f.read()
                f.seek(0)
            except Exception:
                continue
            if not raw_ref:
                continue
            ref_uri = (
                f"data:{self._guess_mime(raw_ref)};base64,"
                f"{base64.b64encode(raw_ref).decode('utf-8')}"
            )
            break  # 端点只吃单张，多传会 422

        if len(files) > 1:
            logger.warning(
                f"images.edit JSON 直传只支持单张参考图，本次 {len(files)} 张仅使用第一张。"
            )

        body: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            # 要 base64 而不是 url：返回的图常托管在模型厂商自己的 CDN（grok-imagine
            # 回 imgen.x.ai），那些域名在国内机器上多半直连不通，拿到 url 也下不下来。
            # 端点不认这个参数时会照旧回 url，下面的 url 分支仍然兜底。
            "response_format": "b64_json",
        }
        if ref_uri:
            body["image"] = {"url": ref_uri}

        endpoint = f"{config.get_base_url(self.provider_name)}/images/edits"
        headers = {
            "Authorization": f"Bearer {config.get_api_key(self.provider_name)}",
            "Content-Type": "application/json",
        }

        status, raw = await self._post_json_raw(endpoint, headers, body)

        if status == 415 and fallback_form:
            logger.warning("images.edit JSON 直传被拒（HTTP 415），退回 SDK multipart 重试。")
            return await self.client.images.edit(
                model=self.model, image=files, prompt=prompt, n=1
            )
        if status != 200:
            raise RuntimeError(
                f"images.edit JSON 直传失败 HTTP {status}: {raw[:500].decode('utf-8', errors='replace')}"
            )
        text = raw.decode("utf-8", errors="replace")
        payload = json.loads(text) if text.strip() else None
        if not payload:
            raise RuntimeError("images.edit JSON 直传未返回响应")

        data = payload.get("data") or []
        if not data:
            raise RuntimeError(f"images.edit JSON 直传未返回 data: {text[:500]}")

        item = data[0] or {}
        b64 = item.get("b64_json") or item.get("b64")
        if b64:
            raw_bytes = base64.b64decode(b64)
        else:
            img_url = item.get("url")
            if not img_url:
                raise RuntimeError("images.edit JSON 直传返回项既无 b64_json 也无 url")
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(img_url) as r:
                    if r.status != 200:
                        raise RuntimeError(f"下载生成图失败 HTTP {r.status}: {img_url}")
                    raw_bytes = await r.read()

        # 拼成与 SDK 响应同构的对象（.data[].b64_json），generate_image 用同一段解析，
        # 避免两个分支各写一套「取 b64 / 取 url」的逻辑。
        from types import SimpleNamespace

        resp_item = SimpleNamespace(b64_json=base64.b64encode(raw_bytes).decode("utf-8"), url=None)
        return SimpleNamespace(data=[resp_item], usage=None)

    @staticmethod
    def _guess_mime(raw: bytes) -> str:
        """根据文件头推断 mime，推断不出退回 image/png。"""
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
            return "image/gif"
        if raw.startswith(b"RIFF"):
            return "image/webp"
        if raw.startswith(b"BM"):
            return "image/bmp"
        return "image/png"

    @staticmethod
    async def _post_json_raw(
        url: str,
        headers: Dict[str, str],
        body: Dict[str, Any],
    ):
        """
        POST JSON 并返回 (status, body_bytes)，优先 httpx，httpx 不可用或失败时退回
        aiohttp。两个库的响应属性名不同（httpx .status_code / aiohttp .status），
        这里统一归一成 status，避免下游写错属性。
        """
        try:
            import httpx

            timeout = httpx.Timeout(180)
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, headers=headers, json=body)
                return r.status_code, r.read()
        except Exception as e:
            logger.warning(
                f"httpx POST 失败（{type(e).__name__}: {e}），退回 aiohttp。"
            )
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as r:
                    return r.status, await r.read()

    async def _images_generate(self, prompt: str):
        """调 images.generate。dall-e-* 需要显式 response_format 才回 b64；gpt-image-1 不认这个参数。"""
        try:
            return await self.client.images.generate(
                model=self.model, prompt=prompt, n=1, response_format="b64_json"
            )
        except Exception as e:
            logger.warning(
                f"images.generate 带 response_format 失败（{type(e).__name__}: {e}），去掉该参数重试。"
            )
            return await self.client.images.generate(model=self.model, prompt=prompt, n=1)

    # chat 生图返回的图片以 data URI 内嵌在文本里，markdown 图片语法或裸 data URI 都见过。
    _DATA_URI_PATTERN = re.compile(
        r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)"
    )

    @classmethod
    def _extract_data_uri_image(cls, text: str) -> Optional[Dict[str, Any]]:
        """从一段文本里抠出第一个 base64 data URI 图片。抠不到返回 None。"""
        if not text:
            return None
        match = cls._DATA_URI_PATTERN.search(text)
        if not match:
            return None
        mime = match.group(1).lower()
        # markdown 里的 base64 可能被换行折过，且尾部常黏着 `)` 之类的收尾字符。
        b64 = re.sub(r"[^A-Za-z0-9+/=]", "", match.group(2))
        if not b64:
            return None
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            logger.warning(f"chat 生图 data URI 解码失败: {type(e).__name__}: {e}")
            return None
        if not raw:
            return None
        return {"bytes": raw, "mime_type": mime}

    async def _chat_generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        走 /v1/chat/completions + modalities=["text","image"] 生图。

        部分 OpenAI 兼容中转站（把 gemini image 模型挂成 openai 协议）只实现这条路径，
        没有 /v1/images/*。图片以 data URI 内嵌在 message.content 的 markdown 里
        （`![image](data:image/jpeg;base64,...)`），也可能出现在 multi_mod_content /
        images 等厂商自定义字段里，所以解析要把整个 message 兜进来找。

        参考图复用普通多模态输入的 image_url data URI 形式。
        """
        content = self._build_user_content(prompt, multimodal_images=reference_images or None)
        params: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["text", "image"],
        }

        try:
            response = await self.client.chat.completions.create(**params)
        except TypeError as e:
            # 老 SDK 不认 modalities 关键字，退回 extra_body 透传。
            logger.warning(f"chat 生图 modalities 关键字不被 SDK 接受（{e}），改用 extra_body 透传。")
            params.pop("modalities", None)
            params["extra_body"] = {"modalities": ["text", "image"]}
            response = await self.client.chat.completions.create(**params)

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            }

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError(f"chat 生图未返回 choices: {response!r:.800}")

        message = getattr(choices[0], "message", None)
        if message is None:
            raise RuntimeError(f"chat 生图返回项缺少 message: {choices[0]!r:.800}")

        # 生图被安全策略拦时同样是 content=None + 异常 finish_reason，
        # 不查的话只会报"没找到 base64 图片"，指向不了真实原因。
        err = self.check_finish_reason_and_log(
            getattr(choices[0], "finish_reason", None),
            response,
            getattr(message, "content", None),
            context=f"OpenAI chat生图/{self.model_alias}",
        )
        if err:
            raise RuntimeError(err)

        # 优先按结构化字段找；找不到再把整个 message 序列化后正则兜底
        # ——各家中转站塞图片的字段名不统一，硬编码字段列表会漏。
        raw_content = getattr(message, "content", None)
        candidates: List[str] = []
        if isinstance(raw_content, str):
            candidates.append(raw_content)
        elif isinstance(raw_content, list):
            for part in raw_content:
                part_dict = part if isinstance(part, dict) else getattr(part, "__dict__", {})
                candidates.append(json.dumps(part_dict, ensure_ascii=False, default=str))

        try:
            candidates.append(json.dumps(message.model_dump(), ensure_ascii=False, default=str))
        except Exception:
            candidates.append(str(message))

        for text in candidates:
            found = self._extract_data_uri_image(text)
            if found:
                return found

        text_preview = (raw_content if isinstance(raw_content, str) else str(raw_content))[:300]
        raise RuntimeError(
            f"chat 生图未在响应里找到 base64 图片（模型可能只回了文本）: {text_preview}"
        )

    async def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        文生图。两种接口形态由 `llm.draw_api` 配置决定：

        - `images`（默认）：OpenAI 原生 images API。无参考图 → images.generate；
          有参考图 → images.edit。参考图以 file-like 传入，`.name` 的后缀决定端点
          怎么判 mime，必须带上。
        - `chat`：/v1/chat/completions + modalities=["text","image"]，图片以 data URI
          内嵌返回。给只实现了 chat 端点的兼容中转站用。
        """
        if not prompt or not prompt.strip():
            raise ValueError("generate_image 需要非空 prompt")

        if config.get_draw_api() == config.DRAW_API_CHAT:
            return await self._chat_generate_image(prompt, reference_images)

        files: List[Any] = []
        for idx, ref in enumerate(reference_images or []):
            raw = ref.get("bytes")
            if not raw and ref.get("base64"):
                raw = base64.b64decode(ref["base64"])
            if not raw:
                continue
            mime = (ref.get("mime_type") or "image/png").lower()
            buf = io.BytesIO(raw)
            buf.name = f"ref_{idx}{self._IMAGE_EXT_BY_MIME.get(mime, '.png')}"
            files.append(buf)

        if not files:
            resp = await self._images_generate(prompt)
        elif config.get_draw_edit_encoding() == config.DRAW_EDIT_ENCODING_JSON:
            # 中转站把 /v1/images/edits 实现成只收 JSON。SDK 是 multipart，
            # 直接发会 415；先试 JSON 直传，被拒再退回 SDK multipart 保底。
            resp = await self._images_edit_json(prompt, files, fallback_form=True)
        else:
            resp = await self._images_edit(prompt, files)

        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }

        data = getattr(resp, "data", None) or []
        if not data:
            raise RuntimeError(f"OpenAI 生图未返回 data: {resp}")

        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            # mime 按魔数认，不要硬编码 png：同一个 draw 别名后面可能挂回 JPEG 的模型
            # （grok-imagine 就回 JPEG），写死 png 会让落盘扩展名与实际内容不符。
            raw_bytes = base64.b64decode(b64)
            return {"bytes": raw_bytes, "mime_type": self._guess_mime(raw_bytes)}

        url = getattr(item, "url", None)
        if url:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as r:
                    if r.status != 200:
                        raise RuntimeError(f"下载生成图失败 HTTP {r.status}: {url}")
                    raw = await r.read()
            # 魔数优先：CDN 链接常带查询参数或干脆没扩展名，靠后缀猜会错。
            return {"bytes": raw, "mime_type": self._guess_mime(raw)}

        raise RuntimeError("OpenAI 生图返回项既无 b64_json 也无 url")

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
        multimodal_videos: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        非流式调用 OpenAI API（含 function calling 支持）。

        注意：chat() 用于 fast_llm 等不需要工具调用的场景，
        大部分工具调用走 chat_stream()。但仍完整实现以备需要。
        """
        self._begin_call()
        if self.use_responses_api:
            payload: Dict[str, Any] = {
                "model": self.model,
                "instructions": system_prompt,
                "input": [
                    *self._convert_history_for_responses(history),
                    {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
                ],
            }
            if self.max_output_tokens:
                payload["max_output_tokens"] = int(self.max_output_tokens)
            self._apply_effort(payload, responses_api=True)
            self._apply_temperature(payload)
            if tools:
                payload["tools"] = self._convert_tools_schema_for_responses(tools)
            try:
                response = await self._responses_create(**payload)
                usage = getattr(response, "usage", None)
                if usage:
                    self.last_usage = {
                        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                    }
                return self._extract_response_text(response)
            except Exception as e:
                logger.error(f"OpenAI Responses API Error: {e}", exc_info=True)
                return self._fail(
                    "Sorry, I encountered an issue processing your request with the OpenAI API.",
                    reason=f"responses_api_exception:{e}",
                )

        filtered_history = self._convert_history(history)
        user_content = self._build_user_content(user_prompt, multimodal_images, multimodal_videos)
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
        if self.max_output_tokens:
            kwargs["max_tokens"] = int(self.max_output_tokens)
        self._apply_effort(kwargs)
        self._apply_temperature(kwargs)

        # 如果有 tools，转换格式并传入
        if tools:
            converted_tools = self._convert_tools_schema(tools)
            if converted_tools:
                kwargs["tools"] = converted_tools

        try:
            try:
                response = await self._with_retry(lambda: self.client.chat.completions.create(**kwargs))
            except Exception as first_error:
                # 不支持推理/自定义温度的模型会因 reasoning_effort 或 temperature 直接 400。
                # 去掉惹事的可选字段重试一次，否则一个调优字段会让整个别名不可用。
                stripped = []
                if "reasoning_effort" in kwargs and self._is_effort_rejection(first_error):
                    kwargs.pop("reasoning_effort", None)
                    stripped.append("reasoning_effort")
                if "temperature" in kwargs and self._is_temperature_rejection(first_error):
                    kwargs.pop("temperature", None)
                    stripped.append("temperature")
                if not stripped:
                    raise
                logger.warning(
                    f"[OpenAI chat/{self.model_alias}] 端点不接受 {'/'.join(stripped)}，去掉后重试: {first_error}"
                )
                response = await self._with_retry(lambda: self.client.chat.completions.create(**kwargs))

            # 防御：兼容异常返回类型（例如 str）
            if not hasattr(response, "choices"):
                logger.error(f"OpenAI chat() unexpected response type: {type(response)} -> {response}")
                return self._fail(
                    "Sorry, I encountered an issue processing your request with the OpenAI API.",
                    reason=f"unexpected_response_type:{type(response)}",
                )

            # 保存 usage 信息
            if hasattr(response, "usage") and response.usage:
                self.last_usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens
                }
            # 防御：choices 可能为空、message 可能缺失（部分 provider 返回空体或被内容过滤拦截）
            choices = getattr(response, "choices", None) or []
            if not choices:
                logger.error(f"OpenAI chat() empty choices，原始响应: {repr(response)[:800]}")
                return self._fail(
                    "Sorry, I encountered an issue processing your request with the OpenAI API.",
                    reason="empty_choices",
                )
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None) if message else None

            # finish_reason 异常（内容安全拦截 / 非正常终止）时，日志打原始响应并直接返回错误说明。
            # 否则这类失败会退化成"返回空字符串"，上层只能看到"空结果"而不知道真实原因。
            err = self.check_finish_reason_and_log(
                getattr(choices[0], "finish_reason", None),
                response,
                content,
                context=f"OpenAI chat/{self.model_alias}",
            )
            if err:
                return err

            cleaned = self.strip_think_content(content or "")
            if not cleaned.strip():
                # 没有异常 finish_reason 却拿不到内容：推理模型把额度耗在 reasoning 上、
                # 或兼容端点把正文塞进了非标字段，都会走到这里。原始响应是唯一线索。
                logger.error(
                    f"[OpenAI chat/{self.model_alias}] 返回内容为空（finish_reason="
                    f"{getattr(choices[0], 'finish_reason', None)}，usage={self.last_usage}），"
                    f"原始 message: {repr(message)[:800]}"
                )
                self.last_error = "empty_content"
            return cleaned
        except Exception as e:
            logger.error(f"OpenAI API Error: {e}", exc_info=True)
            return self._fail(
                "Sorry, I encountered an issue processing your request with the OpenAI API.",
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
        self._begin_call()
        if self.use_responses_api:
            payload: Dict[str, Any] = {
                "model": self.model,
                "instructions": system_prompt,
                "input": [
                    *self._convert_history_for_responses(history),
                    {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
                ],
                "stream": True,
            }
            if self.max_output_tokens:
                payload["max_output_tokens"] = int(self.max_output_tokens)
            self._apply_effort(payload, responses_api=True)
            self._apply_temperature(payload)
            if tools:
                payload["tools"] = self._convert_tools_schema_for_responses(tools)
            try:
                stream_payload = self._filter_supported_kwargs(self.client.responses.create, payload)
                stream = await self._with_retry(lambda: self.client.responses.create(**stream_payload))
            except Exception as e:
                logger.error(f"OpenAI Responses Stream Error: {e}", exc_info=True)
                yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
                return

            pending_args: Dict[str, str] = {}
            pending_names: Dict[str, str] = {}
            in_think_block = False
            input_tokens = 0
            output_tokens = 0
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    visible_text, in_think_block = self.strip_stream_think_segment(delta, in_think_block)
                    if visible_text:
                        yield {"type": "text", "content": visible_text}
                elif event_type == "response.function_call_arguments.delta":
                    call_id = getattr(event, "call_id", "")
                    pending_args[call_id] = pending_args.get(call_id, "") + (getattr(event, "delta", "") or "")
                elif event_type == "response.function_call_arguments.done":
                    call_id = getattr(event, "call_id", "")
                    name = pending_names.get(call_id) or getattr(event, "name", "")
                    raw_args = pending_args.get(call_id, "") or getattr(event, "arguments", "{}")
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except Exception:
                        parsed_args = {}
                    yield {"type": "tool_call", "name": name, "args": parsed_args}
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if getattr(item, "type", "") == "function_call":
                        call_id = getattr(item, "call_id", "")
                        pending_names[call_id] = getattr(item, "name", "")
                elif event_type == "response.completed":
                    response = getattr(event, "response", None)
                    usage = getattr(response, "usage", None) if response else None
                    if usage:
                        input_tokens = getattr(usage, "input_tokens", 0) or 0
                        output_tokens = getattr(usage, "output_tokens", 0) or 0
            if input_tokens or output_tokens:
                self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
                yield {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens}
            return

        filtered_history = self._convert_history(history)
        user_content = self._build_user_content(user_prompt, multimodal_images, multimodal_videos)
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
        if self.max_output_tokens:
            kwargs["max_tokens"] = int(self.max_output_tokens)
        self._apply_effort(kwargs)
        self._apply_temperature(kwargs)

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

        # 两个可选字段都可能被兼容端点拒掉，且报错措辞高度重叠（都可能只说
        # "unrecognized parameter"）。所以不按错误文本二选一——那样会先摘错字段、
        # 重试再失败一次——而是按"最可能是谁"的顺序逐个摘掉再试。
        stream = None
        last_stream_error: Optional[Exception] = None
        for attempt_desc, strip_key in (
            ("原始请求", None),
            ("去掉 stream_options", "stream_options"),
            ("去掉 reasoning_effort", "reasoning_effort"),
            ("去掉 temperature", "temperature"),
        ):
            if strip_key is not None:
                if strip_key not in kwargs:
                    continue
                # 只在错误确实指向这个字段时才摘；否则摘了也没用，白发一次请求。
                implicated = (
                    strip_key in str(last_stream_error).lower()
                    or "unrecognized" in str(last_stream_error).lower()
                    or "unsupported" in str(last_stream_error).lower()
                    or (strip_key == "reasoning_effort" and self._is_effort_rejection(last_stream_error))
                    or (strip_key == "temperature" and self._is_temperature_rejection(last_stream_error))
                )
                if not implicated:
                    break
                logger.warning(
                    f"[OpenAI chat_stream/{self.model_alias}] 端点不接受 {strip_key}"
                    f"={kwargs.get(strip_key)!r}，{attempt_desc} 后重试: {last_stream_error}"
                )
                kwargs.pop(strip_key, None)
            try:
                stream = await self._with_retry(lambda: self.client.chat.completions.create(**kwargs))
                break
            except Exception as e:
                last_stream_error = e

        if stream is None:
            logger.error(f"OpenAI API Stream Error: {last_stream_error}", exc_info=True)
            self.last_error = f"stream_exception:{last_stream_error}"
            yield {"type": "text", "content": "Sorry, an error occurred during streaming."}
            return
        
        try:
            input_tokens = 0
            output_tokens = 0
            
            # 累积 tool_calls 的状态
            # key: tool_call index, value: {"id": str, "name": str, "arguments": str}
            pending_tool_calls: Dict[int, Dict[str, str]] = {}
            in_think_block = False
            stream_finish_reason: Any = None
            emitted_text_len = 0

            async for chunk in stream:
                # 收集 usage（最后一个 chunk 通常包含）
                if hasattr(chunk, 'usage') and chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0

                if not chunk.choices:
                    continue

                # finish_reason 只在末尾的 chunk 上出现，流结束后才能判断是否被拦。
                if getattr(chunk.choices[0], "finish_reason", None):
                    stream_finish_reason = chunk.choices[0].finish_reason

                delta = chunk.choices[0].delta

                # === 处理文本内容 ===
                if delta.content:
                    visible_text, in_think_block = self.strip_stream_think_segment(delta.content, in_think_block)
                    if visible_text:
                        emitted_text_len += len(visible_text)
                        yield {"type": "text", "content": visible_text}
                
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

            # 流结束后才有完整 finish_reason，迟查总比不查强——至少异常时留下线索。
            err = self.check_finish_reason_and_log(
                stream_finish_reason,
                {"finish_reason": stream_finish_reason, "emitted_text_len": emitted_text_len},
                f"(streamed {emitted_text_len} chars text)",
                context=f"OpenAI stream/{self.model_alias}",
            )
            if err and not emitted_text_len and not pending_tool_calls:
                # 没产出过任何 text 或 tool 就异常终止——相当于空回，得告诉上层。
                # 但如果已经有过正常产出，再抛异常会打断流，不如只记日志。
                logger.error(f"[OpenAI stream/{self.model_alias}] 流未产出任何内容且异常终止：{err}")
                yield {"type": "text", "content": err}
            
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
