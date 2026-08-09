"""Civitai provider（仅生图，且仅图生图）。

Civitai 的 orchestration API 与 OpenAI / Gemini / OpenRouter 都不一样：
它不是「一次请求拿一张图」，而是提交一个 workflow 再等它跑完。

    POST {base_url}/v2/consumer/workflows?wait=60
    Authorization: Bearer <api_key>
    {"steps": [{"$type": "imageGen", "input": {...}}]}

响应 200 表示已跑完，202 表示还在跑——此时按 workflow id 轮询
GET {base_url}/v2/consumer/workflows/{id}?wait=60 直到 succeeded。
最终图片在 steps[0].output.images[0].url，是**签名 URL 且会过期**，
所以拿到就立刻下载成 bytes，不往上层传 URL。

**本 provider 只做图生图（operation=editImage），强制要求参考图。**
理由：本项目的形象一致性完全靠 appearance/refs/ 的参考图锚定，
纯文生图（createImage）每次都会画出另一个人。没有参考图直接报错，
不静默降级成文生图——那样只会稳定产出陌生人。

`images` 字段接受 1~3 张（URL / data URL / Base64），本地参考图以
data URI 内嵌。超过 3 张会 400（images maxItems），所以这里截断。

chat / chat_stream 不实现：Civitai 不是对话端点，这个 provider
只应该绑给 `draw` 别名。
"""

import base64
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiohttp

from brain.interface import BaseLLM
import config

logger = logging.getLogger(__name__)

# Civitai editImage 的硬上限，超了直接 400 images maxItems。
_MAX_SOURCE_IMAGES = 3

# 单次 HTTP 请求里让服务端阻塞等待的秒数。跑完就直接 200 返回，
# 没跑完返回 202，再由轮询接手。
_WAIT_SECONDS = 60

# 整体等待上限：SD 系模型排队 + 生成偶尔要几分钟。
_TOTAL_TIMEOUT_SECONDS = 600


class CivitaiProvider(BaseLLM):
    """Civitai orchestration provider（仅 `draw`，仅图生图）。"""

    def __init__(self, model_alias: str = "draw", provider_name: Optional[str] = None):
        super().__init__()
        provider_name = provider_name or config.get_model_provider(model_alias)
        self.provider_name = provider_name
        self.model_alias = model_alias

        self.api_key = config.get_api_key(provider_name)
        if not self.api_key:
            raise ValueError(f"提供商 {provider_name} 缺少 api_key")

        base = config.get_base_url(provider_name)
        self.base_url = (base or "https://orchestration.civitai.com").rstrip("/")

        self.model = config.get_model_name(model_alias)
        if not self.model:
            raise ValueError(f"Model for alias '{model_alias}' not found in config.")

        # engine 决定走哪套后端。不配就按模型串推断：
        # AIR urn（urn:air:sdxl:checkpoint:...）走 sdcpp，其余把模型串本身当 engine
        # （grok / flux2 这类托管模型的 model 就是 "grok" / "klein"）。
        self.engine = config.get_provider_option(provider_name, "engine") or None

    # --- Civitai 不是对话端点，这两个只为满足 BaseLLM 抽象接口 ---

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        raise NotImplementedError(
            "Civitai provider 只支持生图，不能绑给对话类模型别名（smart/fast/...）"
        )

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        history: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        multimodal_images: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict, None]:
        raise NotImplementedError(
            "Civitai provider 只支持生图，不能绑给对话类模型别名（smart/fast/...）"
        )
        yield {}  # pragma: no cover - 让函数保持 async generator 签名

    def _resolve_ecosystem(self) -> Optional[str]:
        """从 AIR urn 取 ecosystem：urn:air:{ecosystem}:checkpoint:civitai:{id}@{ver}。"""
        if not self.model.startswith("urn:air:"):
            return None
        parts = self.model.split(":")
        return parts[2] if len(parts) > 2 else None

    def _build_input(self, prompt: str, images: List[str]) -> Dict[str, Any]:
        ecosystem = self._resolve_ecosystem()
        engine = self.engine or ("sdcpp" if ecosystem else self.model)

        step_input: Dict[str, Any] = {
            "engine": engine,
            "operation": "editImage",
            "prompt": prompt,
            "images": images,
        }
        if ecosystem:
            # AIR 模型要显式带 ecosystem + model；托管模型（grok 等）engine 即身份。
            step_input["ecosystem"] = ecosystem
            step_input["model"] = self.model
        elif not self.engine:
            # 模型串被当作 engine 用时，Civitai 侧仍可能需要 model 区分变体（如 klein）。
            step_input["model"] = self.model

        return step_input

    async def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        图生图（operation=editImage）。必须有参考图。

        参考图格式复用 multimodal_images 同构结构（mime_type / bytes / base64），
        以 data URI 内嵌进 `images`。
        """
        if not prompt or not prompt.strip():
            raise ValueError("generate_image 需要非空 prompt")

        images = self._encode_reference_images(reference_images)
        if not images:
            raise RuntimeError(
                "Civitai provider 只做图生图，本次没有可用参考图。"
                "先用 generate_appearance_reference 生成 appearance/refs/ 下的参考图。"
            )

        body = {"steps": [{"$type": "imageGen", "input": self._build_input(prompt, images)}]}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=_TOTAL_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"{self.base_url}/v2/consumer/workflows?wait={_WAIT_SECONDS}"
            async with session.post(url, headers=headers, json=body) as resp:
                text = await resp.text()
                if resp.status not in (200, 202):
                    raise RuntimeError(
                        f"Civitai 生图提交失败 HTTP {resp.status}: {text[:500]}"
                    )
                payload = await resp.json()

            payload = await self._await_completion(session, headers, payload)
            image_url = self._extract_image_url(payload)
            return await self._download(session, image_url)

    @staticmethod
    def _encode_reference_images(
        reference_images: Optional[List[Dict[str, Any]]],
    ) -> List[str]:
        images: List[str] = []
        for ref in (reference_images or []):
            b64 = ref.get("base64")
            if not b64:
                raw = ref.get("bytes")
                if not raw:
                    continue
                b64 = base64.b64encode(raw).decode("utf-8") if isinstance(raw, bytes) else str(raw)
            mime = ref.get("mime_type") or "image/png"
            images.append(f"data:{mime};base64,{b64}")

        if len(images) > _MAX_SOURCE_IMAGES:
            logger.warning(
                "Civitai editImage 最多接受 %d 张参考图，本次 %d 张，截断多余的。",
                _MAX_SOURCE_IMAGES, len(images),
            )
            images = images[:_MAX_SOURCE_IMAGES]
        return images

    async def _await_completion(
        self,
        session: aiohttp.ClientSession,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """workflow 未完成时按 id 轮询，直到终态。"""
        status = (payload or {}).get("status")
        workflow_id = (payload or {}).get("id")

        while status not in ("succeeded", "failed", "canceled", "expired"):
            if not workflow_id:
                raise RuntimeError(f"Civitai 生图未返回 workflow id，无法轮询: {payload}")
            url = f"{self.base_url}/v2/consumer/workflows/{workflow_id}?wait={_WAIT_SECONDS}"
            async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status not in (200, 202):
                    raise RuntimeError(
                        f"Civitai 轮询失败 HTTP {resp.status}: {text[:500]}"
                    )
                payload = await resp.json()
            status = (payload or {}).get("status")

        if status != "succeeded":
            raise RuntimeError(f"Civitai 生图未成功 status={status}: {str(payload)[:500]}")
        return payload

    @staticmethod
    def _extract_image_url(payload: Dict[str, Any]) -> str:
        for step in (payload or {}).get("steps") or []:
            for image in ((step or {}).get("output") or {}).get("images") or []:
                url = (image or {}).get("url")
                if url:
                    return url
        raise RuntimeError(f"Civitai 生图结果里没有图片 URL: {str(payload)[:500]}")

    @staticmethod
    async def _download(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
        """签名 URL 会过期，拿到就立刻下载成 bytes。"""
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Civitai 图片下载失败 HTTP {resp.status}")
            raw = await resp.read()
        if not raw:
            raise RuntimeError("Civitai 图片下载得到空内容")
        mime = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
        return {"bytes": raw, "mime_type": mime or "image/png"}
