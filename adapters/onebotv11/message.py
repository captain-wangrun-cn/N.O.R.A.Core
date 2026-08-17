from __future__ import annotations

import html
import re
from typing import Any, Iterable

from adapters.base import AdapterEvent, AdapterMessage, MessageSegment
from adapters.message_controls import format_mention_marker, format_reply_marker


_CQ_PATTERN = re.compile(r"\[CQ:(?P<type>[a-zA-Z0-9_.-]+)(?P<params>(?:,[^\]]*)?)\]")

# 表情包段：QQ 商城表情（mface/marketface），以及 subType/sub_type/type 为 1 的 image。
# 三个字段名都要试——不同实现（NapCat / go-cqhttp / Lagrange）用的键不一样。
_STICKER_SEGMENT_TYPES = {"mface", "marketface"}


def is_sticker_segment(segment: dict[str, Any]) -> bool:
    """该消息段是否是表情包（含 QQ 商城表情）。"""
    segment_type = str(segment.get("type") or "")
    if segment_type in _STICKER_SEGMENT_TYPES:
        return True
    if segment_type != "image":
        return False
    data = segment.get("data") or {}
    sticker_type = data.get("subType", data.get("sub_type", data.get("type", "")))
    return str(sticker_type) == "1"


def is_media_segment(segment: dict[str, Any]) -> bool:
    """该消息段是否是**需要模型真正去看**的媒体（图片/视频/语音/文件）。

    表情包不算：它的真图按设计不进模型（`back_brain.py` 按 is_sticker 过滤）、
    也不进 ImageStore，只走 fast-image 出一句 desc。把它当媒体会让上层给模型
    附上"上方媒体就是被回复的真实内容"的提示，而模型其实拿不到那张图。
    """
    if is_sticker_segment(segment):
        return False
    return str(segment.get("type") or "") in {"image", "video", "record", "file"}


def cq_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def cq_unescape(value: str) -> str:
    return html.unescape(str(value))


def parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    raw = (raw or "").lstrip(",")
    if not raw:
        return params
    for part in raw.split(","):
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key] = cq_unescape(value)
    return params


def parse_cq_message(raw_message: str) -> list[dict[str, Any]]:
    """Parse a OneBot CQ-code string into array message segments."""
    result: list[dict[str, Any]] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(raw_message or ""):
        if match.start() > cursor:
            text = raw_message[cursor:match.start()]
            if text:
                result.append({"type": "text", "data": {"text": cq_unescape(text)}})
        result.append(
            {
                "type": match.group("type"),
                "data": parse_cq_params(match.group("params") or ""),
            }
        )
        cursor = match.end()
    if cursor < len(raw_message or ""):
        text = raw_message[cursor:]
        if text:
            result.append({"type": "text", "data": {"text": cq_unescape(text)}})
    return result or [{"type": "text", "data": {"text": raw_message or ""}}]


def onebot_message_segments(message: Any, raw_message: str = "") -> list[dict[str, Any]]:
    if isinstance(message, list):
        return [seg for seg in message if isinstance(seg, dict)]
    if isinstance(message, dict):
        return [message]
    if isinstance(message, str):
        return parse_cq_message(message)
    return parse_cq_message(raw_message or "")


def reply_id_from_segments(segments: Iterable[dict[str, Any]]) -> str:
    """Return the OneBot reply message id carried by message segments, if any."""
    for segment in segments:
        if str(segment.get("type") or "") != "reply":
            continue
        data = segment.get("data") or {}
        reply_id = str(data.get("id") or data.get("message_id") or "").strip()
        if reply_id:
            return reply_id
    return ""


def reply_id_from_event(event: dict[str, Any], segments: Iterable[dict[str, Any]] | None = None) -> str:
    """Best-effort reply id extraction across OneBot/NapCat event variants."""
    if segments is not None:
        reply_id = reply_id_from_segments(segments)
        if reply_id:
            return reply_id

    for key in (
        "reply_to_message_id",
        "reply_message_id",
        "reply_id",
        "source_message_id",
        "quote_message_id",
    ):
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    for key in ("reply", "source", "quote"):
        nested = event.get(key)
        if not isinstance(nested, dict):
            continue
        for nested_key in ("message_id", "id", "reply_id"):
            value = nested.get(nested_key)
            if value is not None and str(value).strip():
                return str(value).strip()

    raw_message = str(event.get("raw_message") or "")
    if raw_message:
        return reply_id_from_segments(parse_cq_message(raw_message))
    return ""


def native_reference_markers(
    segments: Iterable[dict[str, Any]],
    *,
    self_id: str = "",
    include_mentions: bool = True,
) -> tuple[str, list[str]]:
    """Serialize native reply/@ segments to canonical model-visible markers."""
    parts: list[str] = []
    mentioned_user_ids: list[str] = []
    reply_added = False
    for segment in segments:
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if segment_type == "reply" and not reply_added:
            marker = format_reply_marker(str(data.get("id") or data.get("message_id") or ""))
            if marker:
                parts.append(marker)
                reply_added = True
        elif segment_type == "at" and include_mentions:
            user_id = str(data.get("qq") or "").strip()
            if not user_id or user_id == str(self_id or ""):
                continue
            marker = format_mention_marker(user_id)
            if marker:
                parts.append(marker)
                mentioned_user_ids.append(user_id)
    return "".join(parts), list(dict.fromkeys(mentioned_user_ids))


def plain_text_from_segments(segments: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if segment_type == "text":
            parts.append(str(data.get("text") or ""))
        elif segment_type == "at":
            qq = str(data.get("qq") or "")
            parts.append(f"@{qq}" if qq else "@")
        elif segment_type == "face":
            parts.append(f"[表情:{data.get('id') or ''}]")
        elif segment_type == "reply":
            parts.append(f"[回复:{data.get('id') or ''}]")
        elif segment_type in {"image", "record", "video", "file"}:
            if segment_type == "image" and is_sticker_segment(segment):
                label = "表情包"
            else:
                label = {
                    "image": "图片",
                    "record": "语音",
                    "video": "视频",
                    "file": "文件",
                }.get(segment_type, segment_type)
            parts.append(f"[{label}:{data.get('file') or data.get('url') or ''}]")
        elif segment_type in ("mface", "marketface"):
            parts.append(f"[表情包:{data.get('file') or data.get('url') or ''}]")
        elif segment_type == "forward":
            parts.append(f"[聊天记录:{data.get('id') or ''}]")
        else:
            parts.append(f"[{segment_type}:{data}]")
    return "".join(parts).strip()


def segment_to_onebot(segment: MessageSegment) -> dict[str, Any]:
    if segment.type == "text":
        return {"type": "text", "data": {"text": str(segment.data.get("text") or "")}}
    if segment.type == "image":
        return {"type": "image", "data": {"file": str(segment.data.get("path") or "")}}
    if segment.type == "video":
        return {"type": "video", "data": {"file": str(segment.data.get("path") or "")}}
    if segment.type in {"audio", "voice"}:
        return {"type": "record", "data": {"file": str(segment.data.get("path") or "")}}
    if segment.type in {"file", "doc", "document"}:
        return {"type": "file", "data": {"file": str(segment.data.get("path") or "")}}
    if segment.type == "reply":
        return {"type": "reply", "data": {"id": str(segment.data.get("id") or segment.data.get("text") or "")}}
    return {"type": segment.type, "data": dict(segment.data or {})}


def onebot_message_from_adapter(message: AdapterMessage | MessageSegment | str) -> list[dict[str, Any]]:
    if isinstance(message, AdapterMessage):
        return [segment_to_onebot(segment) for segment in message]
    if isinstance(message, MessageSegment):
        return [segment_to_onebot(message)]
    return [{"type": "text", "data": {"text": str(message or "")}}]


def sender_display_name(sender: dict[str, Any], fallback_user_id: str = "") -> str:
    card = str(sender.get("card") or "").strip()
    nickname = str(sender.get("nickname") or "").strip()
    return card or nickname or str(fallback_user_id or "Unknown")


def is_at_self(segments: Iterable[dict[str, Any]], self_id: str) -> bool:
    if not self_id:
        return False
    for segment in segments:
        if str(segment.get("type") or "") != "at":
            continue
        qq = str((segment.get("data") or {}).get("qq") or "")
        if qq == str(self_id):
            return True
    return False


def strip_at_self(segments: Iterable[dict[str, Any]], self_id: str) -> list[dict[str, Any]]:
    if not self_id:
        return [dict(seg) for seg in segments]
    stripped: list[dict[str, Any]] = []
    for segment in segments:
        if str(segment.get("type") or "") == "at" and str((segment.get("data") or {}).get("qq") or "") == str(self_id):
            continue
        stripped.append(dict(segment))
    return stripped


def remove_empty_text_edges(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    while segments and segments[0].get("type") == "text":
        data = dict(segments[0].get("data") or {})
        text = str(data.get("text") or "").lstrip()
        if text:
            data["text"] = text
            segments[0]["data"] = data
            break
        segments.pop(0)
    while segments and segments[-1].get("type") == "text":
        data = dict(segments[-1].get("data") or {})
        text = str(data.get("text") or "").rstrip()
        if text:
            data["text"] = text
            segments[-1]["data"] = data
            break
        segments.pop()
    return segments


def onebot_event_to_adapter_event(
    event: dict[str, Any],
    *,
    text: str,
    message: AdapterMessage | None = None,
) -> AdapterEvent:
    message_type = str(event.get("message_type") or "private")
    chat_id = str(event.get("group_id") if message_type == "group" else event.get("user_id") or "")
    user_id = str(event.get("user_id") or chat_id)
    sender = event.get("sender") or {}
    return AdapterEvent(
        platform="onebotv11",
        chat_id=chat_id,
        user_id=user_id,
        text=text or (message.to_text() if message else ""),
        chat_type="group" if message_type == "group" else "private",
        user_name=sender_display_name(sender, user_id),
        platform_message_id=str(event.get("message_id")) if event.get("message_id") is not None else None,
        raw=event,
        message=message,
    )
