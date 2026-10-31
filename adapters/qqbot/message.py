from __future__ import annotations

import re
from typing import Any, Mapping

from adapters.base import AdapterEvent
from adapters.message_controls import format_mention_marker

from .constants import MENTION_ONLY_TEXT

_MENTION = re.compile(r"<@!?([^>]+)>")


def scene_ext(data: Mapping[str, Any]) -> dict[str, str]:
    scene = data.get("message_scene") or {}
    ext = (scene.get("ext") or []) if isinstance(scene, Mapping) else []
    if isinstance(ext, Mapping):
        return {str(key): str(value) for key, value in ext.items()}
    result = {}
    if isinstance(ext, (list, tuple)):
        for item in ext:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                result[key] = value
    return result


def attachment_type(attachment: Mapping[str, Any]) -> str:
    content_type = str(attachment.get("content_type") or "").lower()
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type == "voice" or content_type.startswith("audio/"):
        return "audio"
    return "file"


def render_elements(elements: Any, *, max_depth: int = 4, max_nodes: int = 60, max_chars: int = 8000) -> str:
    """嵌套引用/聊天记录只展开文字和媒体占位，不下载内部附件。"""
    lines: list[str] = []
    nodes = 0
    chars = 0
    truncated = False

    def walk(items: Any, depth: int) -> None:
        nonlocal nodes, chars, truncated
        if not isinstance(items, list):
            return
        if depth > max_depth:
            truncated = True
            return
        for item in items:
            if nodes >= max_nodes or chars >= max_chars:
                truncated = True
                return
            if not isinstance(item, Mapping):
                continue
            nodes += 1
            author = item.get("author") or {}
            name = str(author.get("username") or author.get("member_openid") or author.get("user_openid") or "未知用户")
            content = str(item.get("content") or "").strip()
            for attachment in (item.get("attachments") or [])[:20]:
                if isinstance(attachment, Mapping):
                    content += f" [{attachment_type(attachment)}附件]"
            if content:
                line = f"{name}: {content}"[:max_chars - chars]
                lines.append(line)
                chars += len(line)
            walk(item.get("msg_elements"), depth + 1)

    walk(elements, 1)
    if truncated:
        lines.append("[嵌套消息已达展开预算，其余省略]")
    return "\n".join(lines)


def event_to_context(packet: Mapping[str, Any], *, self_id: str = "", state: Any = None) -> dict[str, Any]:
    data = packet.get("d") or {}
    event_type = str(packet.get("t") or "")
    chat_type = "private" if event_type == "C2C_MESSAGE_CREATE" else "group"
    author = data.get("author") or {}
    user_id = str(author.get("member_openid" if chat_type == "group" else "user_openid") or author.get("id") or "")
    chat_id = str(data.get("group_openid") or "") if chat_type == "group" else user_id
    user_name = str(author.get("username") or "未知用户")
    message_id = str(data.get("id") or "")
    ext = scene_ext(data)
    reply_index = ext.get("ref_msg_idx", "")
    reply = state.reference_for(chat_type, chat_id, reply_index) if state and reply_index else None
    content = str(data.get("content") or "")
    mentions = [item for item in (data.get("mentions") or []) if isinstance(item, Mapping)]
    bot_ids = {self_id} if self_id else set()
    # 全量群事件可由平台 is_you 指明本 bot；群开放 ID 不一定等于 READY id。
    for item in mentions:
        if item.get("is_you") is True or any(str(item.get(key) or "") in bot_ids for key in ("id", "member_openid", "user_openid")):
            bot_ids.update(str(item.get(key) or "") for key in ("id", "member_openid", "user_openid") if item.get(key))
    if event_type == "GROUP_AT_MESSAGE_CREATE" and not bot_ids:
        # READY 之前的回放仍可由 AT 事件识别本 bot；不把所有 bot mentions 当成自己。
        bot_ids.update(str(item.get("member_openid") or item.get("id") or "") for item in mentions if item.get("bot"))
    explicit = event_type == "GROUP_AT_MESSAGE_CREATE" or any(
        item.get("is_you") is True or any(str(item.get(key) or "") in bot_ids for key in ("id", "member_openid", "user_openid"))
        for item in mentions
    ) or any(match.group(1) in bot_ids for match in _MENTION.finditer(content))
    mentioned = list(dict.fromkeys(
        str(item.get("member_openid") or item.get("id") or "")
        for item in mentions
        if item.get("is_you") is not True
        and not any(str(item.get(key) or "") in bot_ids for key in ("id", "member_openid", "user_openid"))
        and str(item.get("member_openid") or item.get("id") or "") not in {"", "all"}
    ))
    for match in _MENTION.finditer(content):
        uid = match.group(1)
        if uid not in bot_ids | {"all"} and uid not in mentioned:
            mentioned.append(uid)
    content = _MENTION.sub(lambda match: "@全体成员" if match.group(1) == "all" else "", content).strip()
    markers = "".join(format_mention_marker(uid) for uid in mentioned)
    nested = render_elements(data.get("msg_elements"))
    if nested:
        content = f"[引用/聊天记录]\n{nested}\n[/引用/聊天记录]\n{content}".strip()
    if not content and explicit and not data.get("attachments"):
        content = MENTION_ONLY_TEXT
    context = AdapterEvent(
        platform="qqbot", chat_id=chat_id, chat_type=chat_type,
        user_id=user_id, user_name=user_name, text=markers + content,
        platform_message_id=message_id or None,
        reply_to_message_id=(reply.message_id if reply else reply_index) or None,
        reply_to_user_id=reply.user_id if reply else None,
        reply_to_user_name=reply.user_name if reply else None,
        mentioned_user_ids=mentioned,
        mentioned_users=[{
            "user_id": str(item.get("member_openid") or item.get("id") or ""),
            "display_name": str(item.get("username") or ""),
        } for item in mentions],
    ).to_context()
    context.update({
        "platform_message_ids": [message_id] if message_id else [],
        "explicit_bot_mention": explicit,
        "reply_to_bot": bool(reply and reply.is_bot),
        "self_id": self_id,
        "timestamp": data.get("timestamp"),
        "group_member_count_status": "官方事件未提供",
        "group_online_count_status": "官方事件未提供",
        "qqbot_has_nested_elements": bool(data.get("msg_elements")),
    })
    if reply and reply.is_bot:
        context["reply_to_bot_message_id"] = reply.message_id
    return context
