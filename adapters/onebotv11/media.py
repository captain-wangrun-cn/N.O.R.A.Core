from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession

logger = logging.getLogger(__name__)


class OneBotMediaMixin:
    def _media_filename(self, url_or_file: str, fallback_prefix: str, content_type: str = "") -> str:
        parsed = urlparse(url_or_file or "")
        candidate = os.path.basename(parsed.path or "")
        if candidate and "." in candidate:
            return candidate
        digest = hashlib.sha1(str(url_or_file or fallback_prefix).encode("utf-8")).hexdigest()[:16]
        ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) if content_type else ""
        return f"{fallback_prefix}_{digest}{ext or ''}"

    async def _download_media_url(self, url: str, fallback_prefix: str) -> str | None:
        if not url or not self.download_media:
            return None
        try:
            async with ClientSession() as session:
                async with session.get(url, timeout=30) as response:
                    response.raise_for_status()
                    content = await response.read()
                    filename = self._media_filename(url, fallback_prefix, response.headers.get("content-type", ""))
                    path = Path(self.onebot_data_dir) / filename
                    path.write_bytes(content)
                    return os.path.relpath(path, self.workspace_root).replace("\\", "/")
        except Exception as exc:
            logger.warning("[onebotv11] media download failed url=%s err=%s", url, exc)
            return None

    async def _resolve_media_reference(
        self,
        data: dict[str, Any],
        fallback_prefix: str,
        *,
        resolve_action: str = "",
        resolve_params: dict[str, Any] | None = None,
    ) -> str:
        url = str(data.get("url") or "").strip()
        file_value = str(data.get("file") or data.get("path") or "").strip()
        if url:
            local = await self._download_media_url(url, fallback_prefix)
            return local or url

        if resolve_action and file_value and hasattr(self, "call_api"):
            params = {"file": file_value}
            params.update(resolve_params or {})
            try:
                response = await self.call_api(resolve_action, params)
                resolved_data = response.get("data") or {}
                resolved = str(
                    resolved_data.get("url")
                    or resolved_data.get("file")
                    or resolved_data.get("path")
                    or ""
                ).strip()
                if resolved.startswith(("http://", "https://")):
                    local = await self._download_media_url(resolved, fallback_prefix)
                    return local or resolved
                if resolved:
                    return resolved
            except Exception as exc:
                logger.debug("[onebotv11] %s failed for media file=%s: %s", resolve_action, file_value, exc)

        return file_value

    async def _segment_to_nora_text(self, segment: dict[str, Any]) -> str:
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if segment_type == "text":
            return str(data.get("text") or "")
        if segment_type == "at":
            qq = str(data.get("qq") or "")
            return f"@{qq}" if qq else "@"
        if segment_type == "reply":
            return f"[回复消息ID: {data.get('id') or ''}]"
        if segment_type == "image":
            media = await self._resolve_media_reference(data, "image", resolve_action="get_image")
            return f"[image: {media}]"
        if segment_type == "video":
            media = await self._resolve_media_reference(data, "video", resolve_action="get_file")
            return f"[video: {media}]"
        if segment_type == "record":
            media = await self._resolve_media_reference(
                data,
                "record",
                resolve_action="get_record",
                resolve_params={"out_format": "mp3"},
            )
            return f"[file: {media}]"
        if segment_type == "file":
            media = await self._resolve_media_reference(data, "file", resolve_action="get_file")
            return f"[file: {media}]"
        if segment_type == "face":
            return f"[QQ表情:{data.get('id') or ''}]"
        if segment_type == "forward":
            return f"[合并转发:{data.get('id') or ''}]"
        return f"[{segment_type}: {data}]"

    async def segments_to_nora_text(self, segments: list[dict[str, Any]]) -> str:
        parts = [await self._segment_to_nora_text(segment) for segment in segments]
        return "".join(parts).strip()
