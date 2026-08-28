from __future__ import annotations

import base64
import logging
import os
import re
import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from adapters.message_controls import parse_message_controls, strip_message_control_markers

from .constants import MEDIA_TYPES, ONEBOT_MSG_MAX_LENGTH

logger = logging.getLogger(__name__)


_MEDIA_TAG_PATTERN = re.compile(
    r"\[(image|video|audio|voice|file|doc|document):\s*([^\]]+)\]",
    re.IGNORECASE,
)
_CONTROL_TAG_PATTERN = re.compile(
    r"\[(reply|at|poke)(?::\s*([^\]]+))?\]",
    re.IGNORECASE,
)
_FACE_TAG_PATTERN = re.compile(
    r"\[(face|emoji)(?::\s*([0-9]+))\]",
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
        if resolved.startswith(("http://", "https://")):
            return resolved
        if bool(getattr(self, "send_media_as_base64", False)):
            return self._media_as_base64_uri(resolved)
        if resolved.startswith(("file://", "base64://")):
            return resolved
        return Path(resolved).resolve().as_uri()

    def _media_as_base64_uri(self, resolved: str) -> str:
        """跨机部署模式：读本地文件转 base64://，避免 NapCat 访问不到本机路径。"""
        if resolved.startswith("base64://"):
            return resolved
        local_path = resolved
        if resolved.startswith("file://"):
            local_path = url2pathname(urlparse(resolved).path)
        try:
            encoded = base64.b64encode(Path(local_path).read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning(
                "[onebotv11] base64 encode failed path=%s err=%s, fallback to file uri",
                local_path,
                exc,
            )
            if not resolved.startswith("file://") and os.path.isfile(local_path):
                return Path(local_path).resolve().as_uri()
            return resolved
        return f"base64://{encoded}"

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

    def _last_incoming_context(self, chat_id: str) -> dict[str, str]:
        contexts = getattr(self, "_last_incoming_contexts", {}) or {}
        return dict(contexts.get(str(chat_id)) or {})

    def _resolve_reply_id(self, raw_value: str, chat_id: str, reply_to_message_id: str = "") -> str:
        value = str(raw_value or "").strip()
        if value:
            return value
        return str(reply_to_message_id or "").strip()

    def _resolve_at_user_id(self, raw_value: str, chat_id: str) -> str:
        value = str(raw_value or "").strip()
        recent = self._last_incoming_context(chat_id)
        if value.lower() in {"", "current", "sender", "user"}:
            return str(recent.get("user_id") or "").strip()
        if value.lower() in {"reply", "replied"}:
            return str(recent.get("reply_to_user_id") or "").strip()
        return value.lstrip("@").strip()

    def _control_segment(
        self,
        tag_type: str,
        raw_value: str,
        chat_id: str,
        reply_to_message_id: str = "",
    ) -> dict[str, Any] | None:
        kind = tag_type.lower()
        if kind == "reply":
            reply_id = self._resolve_reply_id(raw_value, chat_id, reply_to_message_id=reply_to_message_id)
            if reply_id:
                return {"type": "reply", "data": {"id": reply_id}}
            return None
        if kind == "at":
            user_id = self._resolve_at_user_id(raw_value, chat_id)
            if user_id:
                return {"type": "at", "data": {"qq": user_id}}
            return None
        return None

    def _face_segment(self, raw_value: str) -> dict[str, Any] | None:
        value = str(raw_value or "").strip()
        if not value.isdigit():
            return None
        return {"type": "face", "data": {"id": value}}

    def _normalize_message_controls(
        self,
        text: str,
        *,
        chat_id: str,
        reply_to_message_id: str = "",
        allow_mentions: bool,
    ) -> str:
        def replace_legacy(match: re.Match) -> str:
            kind = str(match.group(1) or "").lower()
            raw_value = str(match.group(2) or "")
            if kind == "reply":
                reply_id = self._resolve_reply_id(
                    raw_value,
                    chat_id,
                    reply_to_message_id=reply_to_message_id,
                )
                return f"[reply:{reply_id}]" if reply_id else ""
            if kind == "poke":
                value = raw_value.strip().lstrip("@").strip()
                return f"[poke:{value}]" if value else ""
            user_id = self._resolve_at_user_id(raw_value, chat_id)
            return f"[at:{user_id}]" if allow_mentions and user_id else ""

        normalized = _CONTROL_TAG_PATTERN.sub(replace_legacy, str(text or ""))
        parsed = parse_message_controls(normalized)
        parts: list[str] = []
        for token in parsed.tokens:
            if token.type == "text":
                parts.append(token.value)
            elif token.type == "reply":
                parts.append(f"[reply:{token.value}]")
            elif token.type == "mention" and allow_mentions:
                parts.append(f"[at:{token.value}]")
            elif token.type == "poke":
                parts.append(f"[poke:{token.value}]")
        return "".join(parts)

    @staticmethod
    def _normalize_at_spacing(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for segment in segments:
            if normalized and str(normalized[-1].get("type") or "") == "at":
                if str(segment.get("type") or "") == "text":
                    data = dict(segment.get("data") or {})
                    text = re.sub(r"^[ \t]*", " ", str(data.get("text") or ""))
                    data["text"] = text
                    segment = {**segment, "data": data}
                else:
                    normalized.append({"type": "text", "data": {"text": " "}})
            normalized.append(segment)
        if normalized and str(normalized[-1].get("type") or "") == "at":
            normalized.append({"type": "text", "data": {"text": " "}})
        return normalized

    def _text_to_onebot_segments(
        self,
        text: str,
        *,
        parse_media: bool = True,
        chat_id: str = "",
        reply_to_message_id: str = "",
        allow_mentions: bool = False,
    ) -> list[dict[str, Any]]:
        text = self._normalize_message_controls(
            text,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            allow_mentions=allow_mentions,
        )
        if not parse_media:
            parsed = parse_message_controls(text)
            segments: list[dict[str, Any]] = []
            for token in parsed.tokens:
                if token.type == "text" and token.value:
                    segments.append({"type": "text", "data": {"text": token.value}})
                elif token.type == "reply":
                    segments.append({"type": "reply", "data": {"id": token.value}})
                elif token.type == "mention" and allow_mentions:
                    segments.append({"type": "at", "data": {"qq": token.value}})
            reply_id = parsed.reply_message_id or str(reply_to_message_id or "").strip()
            if reply_id and not any(segment.get("type") == "reply" for segment in segments):
                segments.insert(0, {"type": "reply", "data": {"id": reply_id}})
            return self._normalize_at_spacing(segments)

        segments: list[dict[str, Any]] = []
        cursor = 0
        has_reply_segment = False
        missing: list[str] = []
        marker_pattern = re.compile(
            rf"{_MEDIA_TAG_PATTERN.pattern}|{_CONTROL_TAG_PATTERN.pattern}|{_FACE_TAG_PATTERN.pattern}",
            re.IGNORECASE,
        )
        for match in marker_pattern.finditer(text or ""):
            if match.start() > cursor:
                before = text[cursor:match.start()]
                if before:
                    segments.append({"type": "text", "data": {"text": before}})
            tag_type = str(match.group(1) or match.group(3) or match.group(5) or "")
            tag_value = str(match.group(2) or match.group(4) or match.group(6) or "")
            if tag_type.lower() in {"reply", "at"}:
                segment = self._control_segment(
                    tag_type,
                    tag_value,
                    chat_id,
                    reply_to_message_id=reply_to_message_id,
                )
                if segment:
                    segments.append(segment)
                    if str(segment.get("type") or "") == "reply":
                        has_reply_segment = True
            elif tag_type.lower() in {"face", "emoji"}:
                segment = self._face_segment(tag_value)
                if segment:
                    segments.append(segment)
            else:
                segment = self._media_segment(tag_type, tag_value)
                if segment:
                    segments.append(segment)
                else:
                    missing.append(tag_value.strip())
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
        if not segments:
            return []
        reply_id = str(reply_to_message_id or "").strip()
        if reply_id and not has_reply_segment:
            segments.insert(0, {"type": "reply", "data": {"id": reply_id}})
        return self._normalize_at_spacing(segments)

    def _extract_poke_targets(self, text: str) -> tuple[str, list[str]]:
        parsed = parse_message_controls(text)
        visible: list[str] = []
        targets: list[str] = []
        seen: set[str] = set()
        for token in parsed.tokens:
            if token.type == "poke":
                if token.value not in seen:
                    seen.add(token.value)
                    targets.append(token.value)
                continue
            if token.type == "text":
                visible.append(token.value)
            elif token.type == "reply":
                visible.append(f"[reply:{token.value}]")
            elif token.type == "mention":
                visible.append(f"[at:{token.value}]")
        return "".join(visible), targets

    async def _send_poke_targets(self, target: dict[str, Any], user_ids: list[str]) -> None:
        if not user_ids or not bool(getattr(self, "enable_napcat_api", False)):
            return
        for user_id in user_ids:
            if not str(user_id).isdigit():
                logger.debug("[onebotv11] ignore non-numeric poke target: %s", user_id)
                continue
            params: dict[str, Any] = {"user_id": int(user_id)}
            if str(target.get("message_type") or "") == "group":
                params["group_id"] = int(target["group_id"])
            try:
                await self.call_api("send_poke", params)
            except Exception as exc:
                logger.warning("[onebotv11] send poke failed for %s: %s", user_id, exc)

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
        reply_to_message_id: str = "",
    ) -> list[str]:
        all_message_ids: list[str] = []
        target_chat_id = str(target.get("group_id") or target.get("user_id") or "")
        allow_mentions = str(target.get("message_type") or "") == "group"
        normalized_text = self._normalize_message_controls(
            text,
            chat_id=target_chat_id,
            reply_to_message_id=reply_to_message_id,
            allow_mentions=allow_mentions,
        )
        visible_text, poke_targets = self._extract_poke_targets(normalized_text)
        await self._send_poke_targets(target, poke_targets)
        segments = self._text_to_onebot_segments(
            visible_text,
            parse_media=parse_media,
            chat_id=target_chat_id,
            reply_to_message_id=reply_to_message_id,
            allow_mentions=allow_mentions,
        )
        if not segments:
            return all_message_ids
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_text_len = 0
        for segment in segments:
            segment_type = str(segment.get("type") or "")
            if segment_type == "text":
                text_value = str((segment.get("data") or {}).get("text") or "")
                for part in self._split_text(text_value):
                    if current and current_text_len + len(part) > ONEBOT_MSG_MAX_LENGTH:
                        chunks.append(current)
                        current = []
                        current_text_len = 0
                    current.append({"type": "text", "data": {"text": part}})
                    current_text_len += len(part)
            elif segment_type in {"reply", "at", "face"}:
                current.append(segment)
            else:
                control_or_spacing = [
                    seg
                    for seg in current
                    if str(seg.get("type") or "") in {"reply", "at"}
                    or (
                        str(seg.get("type") or "") == "text"
                        and not str((seg.get("data") or {}).get("text") or "").strip()
                    )
                ]
                has_visible_text = any(
                    str(seg.get("type") or "") == "text"
                    and bool(str((seg.get("data") or {}).get("text") or "").strip())
                    for seg in current
                )
                if current and has_visible_text:
                    chunks.append(current)
                    current = []
                    current_text_len = 0
                if control_or_spacing and not has_visible_text:
                    current = control_or_spacing + [segment]
                    chunks.append(current)
                    current = []
                    current_text_len = 0
                else:
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
        reply_to_message_id = str(kwargs.pop("reply_to_message_id", "") or "").strip()
        target = self._chat_target_params(chat_id, chat_type=chat_type)
        raw_text = str(text or "")

        if _SPLIT_MARKER_PATTERN.search(raw_text):
            all_message_ids: list[str] = []
            parts = _SPLIT_MARKER_PATTERN.split(raw_text)
            fallback_reply_id = reply_to_message_id
            for i in range(0, len(parts), 2):
                part = parts[i].strip()
                if part:
                    msg_ids = await self._send_onebot_message_once(
                        target,
                        part,
                        parse_media=parse_media,
                        reply_to_message_id=fallback_reply_id,
                    )
                    all_message_ids.extend(msg_ids)
                    fallback_reply_id = ""

                if i + 1 < len(parts) and parts[i + 1] is not None:
                    try:
                        delay_val = float(parts[i + 1])
                        if delay_val > 0:
                            await asyncio.sleep(delay_val)
                    except ValueError:
                        pass
            return all_message_ids

        return await self._send_onebot_message_once(
            target,
            raw_text,
            parse_media=parse_media,
            reply_to_message_id=reply_to_message_id,
        )

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
