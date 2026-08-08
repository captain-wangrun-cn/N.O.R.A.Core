"""OpenRouter provider。

OpenRouter 以 OpenAI 兼容协议服务对话类模型，`chat` / `chat_stream`
直接复用 OpenAIProvider 的实现；其生图端点（POST /api/v1/images）与
OpenAI 原生 images API 不同，这里单独实现。

OpenRouter 的 /api/v1/images 端点（2025+，如 x-ai/grok-imagine 系）：

    POST {base_url}/images
    Authorization: Bearer <api_key>
    {"model": "x-ai/grok-imagine-image-quality", "prompt": "..."}

响应结构与 OpenAI images API 相同：{"data": [{"b64_json": ...}]}。
该端点目前不收参考图/编辑图，reference_images 非空时降级为纯文本生图。
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

        OpenRouter 该端点暂不收参考图；reference_images 非空时只记 warning
        降级为纯文本生图（上游 appearance_gen 已容忍无参考图——本来就没带图时
        只是形象一致性下降，不会报错）。
        """
        if not prompt or not prompt.strip():
            raise ValueError("generate_image 需要非空 prompt")

        if reference_images:
            logger.warning(
                "[OpenRouter generate_image] 该端点不收参考图，本次降级为纯文本生图"
                "（形象一致性可能下降，建议先用 generate_appearance_reference 生成参考图）"
            )

        url = f"{self.base_url}/images"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "prompt": prompt,
        }

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
