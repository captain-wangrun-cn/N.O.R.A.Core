"""OpenRouter provider。

OpenRouter 以 OpenAI 兼容协议服务对话类模型，`chat` / `chat_stream`
直接复用 OpenAIProvider 的实现；其生图端点（POST /api/v1/images）与
OpenAI 原生 images API 不同，这里单独实现。

OpenRouter 的 /api/v1/images 端点（2025+，如 x-ai/grok-imagine 系）：

    POST {base_url}/images
    Authorization: Bearer <api_key>
    {"model": "x-ai/grok-imagine-image-quality", "prompt": "..."}

响应结构与 OpenAI images API 相同：{"data": [{"b64_json": ...}]}。
参考图经 `input_references`（type=image_url）传入，本地参考图以
data URI 内嵌（data:{mime};base64,{b64}），与图片 URL 等价。
"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from brain.providers.openai import OpenAIProvider
import config

logger = logging.getLogger(__name__)

# 常见 b64_json 响应头 → mime。参考图锚定不可用时，用魔数推断 mime 仍可让
# save_generated_image 落成正确的扩展名。
_MIME_BY_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
    b"RIFF": "image/webp",  # RIFF....WEBP
}


class OpenRouterProvider(OpenAIProvider):
    """LLM provider for OpenRouter（OpenAI 兼容协议 + 独立生图端点）。"""

    def __init__(self, model_alias: str = "smart", provider_name: Optional[str] = None):
        # 父类构造会校验 api_key 并初始化 AsyncOpenAI 客户端，chat 链路直接复用。
        super().__init__(model_alias=model_alias, provider_name=provider_name)
        base = config.get_base_url(self.provider_name)
        self.base_url = (base or "https://openrouter.ai/api/v1").rstrip("/")
        self.api_key = config.get_api_key(self.provider_name)

    async def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        文生图：POST {base_url}/images（与 OpenAI images API 同构的响应结构）。

        OpenRouter 该端点经 `input_references`（type=image_url）收参考图。
        本地参考图没有公网 URL，以 data URI 内嵌（data:{mime};base64,...），
        与图片 URL 等价。参考图格式复用 multimodal_images 同构结构
        （mime_type / bytes / base64）。
        """
        if not prompt or not prompt.strip():
            raise ValueError("generate_image 需要非空 prompt")

        url = f"{self.base_url}/images"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
        }

        refs: List[Dict[str, Any]] = []
        for ref in (reference_images or []):
            mime = ref.get("mime_type") or "image/png"
            b64 = ref.get("base64")
            if not b64:
                raw = ref.get("bytes")
                if not raw:
                    continue
                b64 = base64.b64encode(raw).decode("utf-8") if isinstance(raw, bytes) else str(raw)
            refs.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        if refs:
            body["input_references"] = refs

        timeout = aiohttp.ClientTimeout(total=180)
        payload: Optional[Dict[str, Any]] = None
        text = ""
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f"OpenRouter 生图失败 HTTP {resp.status}: {text[:500]}"
                    )
                payload = json.loads(text) if text else None

        if not payload:
            raise RuntimeError("OpenRouter 生图未返回响应")

        data = payload.get("data") or []
        if not data:
            raise RuntimeError(f"OpenRouter 生图未返回 data: {text[:500]}")

        b64 = (data[0] or {}).get("b64_json") or (data[0] or {}).get("b64")
        if not b64:
            raise RuntimeError("OpenRouter 生图返回项没有 b64_json")

        raw = base64.b64decode(b64)
        mime = self._guess_mime(raw)
        return {"bytes": raw, "mime_type": mime}

    @staticmethod
    def _guess_mime(raw: bytes) -> str:
        for magic, mime in _MIME_BY_MAGIC.items():
            if raw.startswith(magic):
                return mime
        return "image/png"
