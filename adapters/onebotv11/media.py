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
            url = str(data.get("url") or data.get("file") or "")
            local = await self._download_media_url(str(data.get("url") or ""), "image")
            return f"[image: {local or url}]"
        if segment_type == "video":
            url = str(data.get("url") or data.get("file") or "")
            local = await self._download_media_url(str(data.get("url") or ""), "video")
            return f"[video: {local or url}]"
        if segment_type == "record":
            url = str(data.get("url") or data.get("file") or "")
            local = await self._download_media_url(str(data.get("url") or ""), "record")
            return f"[file: {local or url}]"
        if segment_type == "file":
            url = str(data.get("url") or data.get("file") or "")
            local = await self._download_media_url(str(data.get("url") or ""), "file")
            return f"[file: {local or url}]"
        if segment_type == "face":
            return f"[QQ表情:{data.get('id') or ''}]"
        if segment_type == "forward":
            return f"[合并转发:{data.get('id') or ''}]"
        return f"[{segment_type}: {data}]"

    async def segments_to_nora_text(self, segments: list[dict[str, Any]]) -> str:
        parts = [await self._segment_to_nora_text(segment) for segment in segments]
        return "".join(parts).strip()
