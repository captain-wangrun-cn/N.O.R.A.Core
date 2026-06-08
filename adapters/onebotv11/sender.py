from __future__ import annotations

import logging
import os
import re
import asyncio
from pathlib import Path
from typing import Any

from .constants import MEDIA_TYPES, ONEBOT_MSG_MAX_LENGTH

logger = logging.getLogger(__name__)


_MEDIA_TAG_PATTERN = re.compile(
    r"\[(image|video|audio|voice|file|doc|document):\s*([^\]]+)\]",
    re.IGNORECASE,
)
_SPLIT_MARKER_PATTERN = re.compile(r"\[SPLIT(?::([0-9.]+))?\]", re.IGNORECASE)


class OneBotSenderMixin:
    def _split_text(self, text: str, limit: int = ONEBOT_MSG_MAX_LENGTH) -> list[str]:
        text = str(text or "")
        if len(text) <= limit:
            return [text] if text else []
        chunks: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) <= limit:
                current += line
                continue
            if current:
                chunks.append(current.rstrip())
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        if current:
            chunks.append(current.rstrip())
        return [chunk for chunk in chunks if chunk]

    def _classify_file(self, path: str) -> str:
        clean_path = path.split("?", 1)[0].split("#", 1)[0]
        ext = os.path.splitext(clean_path)[1].lower()
        for media_type, extensions in MEDIA_TYPES.items():
            if ext in extensions:
                return media_type
        return "document"

    def _resolve_local_media_path(self, raw_path: str) -> str | None:
        path = (raw_path or "").strip().strip('"').strip("'")
        if not path:
            return None
        if path.startswith(("http://", "https://", "file://", "base64://")):
            return path
        if os.path.isabs(path):
            return str(Path(path)) if os.path.isfile(path) else None

        normalized = path.replace("\\", "/")
        candidates: list[str] = []
        if normalized.startswith("workspace/"):
            candidates.append(os.path.join(self.workspace_root, normalized[len("workspace/"):]))
        if normalized.startswith("downloads/"):
            candidates.append(os.path.join(self.downloads_dir, normalized[len("downloads/"):]))
        if normalized.startswith("download/"):
            candidates.append(os.path.join(self.downloads_dir, normalized[len("download/"):]))
        if normalized.startswith("data/"):
            candidates.append(os.path.join(self.data_dir, normalized[len("data/"):]))
        if normalized.startswith("./"):
            candidates.append(os.path.join(self.workspace_root, normalized[2:]))
        candidates.append(os.path.join(self.workspace_root, path))
        candidates.append(os.path.join(os.getcwd(), path))

        seen: set[str] = set()
        for candidate in candidates:
            norm = os.path.normpath(candidate)
            if norm in seen:
                continue
            seen.add(norm)
            if os.path.isfile(norm):
                return norm
        return None

    def _file_uri(self, resolved: str) -> str:
        if resolved.startswith(("http://", "https://", "file://", "base64://")):
            return resolved
        return Path(resolved).resolve().as_uri()

    def _media_segment(self, tag_type: str, raw_path: str) -> dict[str, Any] | None:
        resolved = self._resolve_local_media_path(raw_path)
        if not resolved:
            return None
        file_value = self._file_uri(resolved)
        media_type = tag_type.lower()
        if media_type == "image":
            return {"type": "image", "data": {"file": file_value}}
        if media_type == "video":
            return {"type": "video", "data": {"file": file_value}}
        if media_type in {"audio", "voice"}:
            return {"type": "record", "data": {"file": file_value}}
        return {"type": "file", "data": {"file": file_value}}

    def _text_to_onebot_segments(self, text: str, *, parse_media: bool = True) -> list[dict[str, Any]]:
        if not parse_media:
            return [{"type": "text", "data": {"text": str(text or "")}}]

        segments: list[dict[str, Any]] = []
        cursor = 0
        missing: list[str] = []
        for match in _MEDIA_TAG_PATTERN.finditer(text or ""):
            if match.start() > cursor:
                before = text[cursor:match.start()]
                if before:
                    segments.append({"type": "text", "data": {"text": before}})
            segment = self._media_segment(match.group(1), match.group(2))
            if segment:
                segments.append(segment)
            else:
                missing.append(match.group(2).strip())
            cursor = match.end()
        if cursor < len(text or ""):
            rest = text[cursor:]
            if rest:
                segments.append({"type": "text", "data": {"text": rest}})
        if missing:
            segments.append(
                {
                    "type": "text",
                    "data": {
                        "text": "\n\n以下文件未找到，已跳过:\n" + "\n".join(f"- {item}" for item in missing)
                    },
                }
            )
        return segments or [{"type": "text", "data": {"text": text or "(empty message)"}}]

    def _chat_target_params(self, chat_id: str, chat_type: str = "") -> dict[str, Any]:
        chat_id = str(chat_id or "").strip()
        chat_type = str(chat_type or self._chat_types.get(chat_id, "") or "").strip().lower()
        if chat_type == "group" or chat_id.startswith("-"):
            return {"message_type": "group", "group_id": int(chat_id)}
        return {"message_type": "private", "user_id": int(chat_id)}

    async def _send_onebot_message_once(
        self,
        target: dict[str, Any],
        text: str,
        *,
        parse_media: bool,
    ) -> list[str]:
        all_message_ids: list[str] = []
        segments = self._text_to_onebot_segments(text, parse_media=parse_media)
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_text_len = 0
        for segment in segments:
            if segment.get("type") == "text":
                text_value = str((segment.get("data") or {}).get("text") or "")
                for part in self._split_text(text_value):
                    if current and current_text_len + len(part) > ONEBOT_MSG_MAX_LENGTH:
                        chunks.append(current)
                        current = []
                        current_text_len = 0
                    current.append({"type": "text", "data": {"text": part}})
                    current_text_len += len(part)
            else:
                if current:
                    chunks.append(current)
                    current = []
                    current_text_len = 0
                chunks.append([segment])
        if current:
            chunks.append(current)
        if not chunks:
            chunks = [[{"type": "text", "data": {"text": text or "(empty message)"}}]]

        for chunk in chunks:
            params = dict(target)
            params["message"] = chunk
            response = await self.call_api("send_msg", params)
            data = response.get("data") or {}
            message_id = data.get("message_id")
            if message_id is not None:
                all_message_ids.append(str(message_id))
        return all_message_ids

    async def send_message(self, chat_id: str, text: str, **kwargs) -> list[str]:
        parse_media = bool(kwargs.pop("parse_media", True))
        chat_type = str(kwargs.pop("chat_type", "") or "")
        target = self._chat_target_params(chat_id, chat_type=chat_type)
        raw_text = str(text or "")

        if _SPLIT_MARKER_PATTERN.search(raw_text):
            all_message_ids: list[str] = []
            parts = _SPLIT_MARKER_PATTERN.split(raw_text)
            for i in range(0, len(parts), 2):
                part = parts[i].strip()
                if part:
                    msg_ids = await self._send_onebot_message_once(target, part, parse_media=parse_media)
                    all_message_ids.extend(msg_ids)

                if i + 1 < len(parts) and parts[i + 1] is not None:
                    try:
                        delay_val = float(parts[i + 1])
                        if delay_val > 0:
                            await asyncio.sleep(delay_val)
                    except ValueError:
                        pass
            return all_message_ids

        return await self._send_onebot_message_once(target, raw_text, parse_media=parse_media)

    async def start_typing(self, chat_id: str):
        _ = chat_id
        return None

    async def stop_typing(self, chat_id: str):
        _ = chat_id
        return None

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        _ = chat_id, kwargs
        try:
            await self.call_api("delete_msg", {"message_id": int(message_id)})
            return True
        except Exception as exc:
            logger.warning("[onebotv11] delete message failed: %s", exc)
            return False
