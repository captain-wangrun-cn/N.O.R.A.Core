"""OneBot v11 合并转发（QQ 聊天记录）解析。

设计要点：
- 入站消息里出现 `forward` 段时**直接展开**成结构化文本，模型不需要再调工具。
- 嵌套聊天记录递归展开，受 `max_depth` 限制。
- 记录内部的图片/视频/语音**不下载不解析**，只留 `[图片]` 之类占位。
- 入站路径按节点数与字符数截断；工具路径给更宽的上限，用于取"完整"内容。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# 入站自动展开：控制注入体积，避免一条聊天记录挤爆上下文
INBOUND_MAX_DEPTH = 3
INBOUND_MAX_NODES = 20
INBOUND_MAX_CHARS = 1500

# 工具主动查询：尽量完整，仍留硬上限防御超大记录
FULL_MAX_DEPTH = 5
FULL_MAX_NODES = 200
FULL_MAX_CHARS = 20000

# 单条消息正文上限，防止记录里某条长文本独占预算
NODE_TEXT_MAX_CHARS = 300

_MEDIA_PLACEHOLDERS = {
    "image": "[图片]",
    "mface": "[表情包]",
    "marketface": "[表情包]",
    "video": "[视频]",
    "record": "[语音]",
    "file": "[文件]",
}


def forward_id_from_segment(segment: dict[str, Any]) -> str:
    """从 forward 消息段取合并转发 id。"""
    if str(segment.get("type") or "") != "forward":
        return ""
    data = segment.get("data") or {}
    return str(data.get("id") or data.get("res_id") or data.get("resid") or "").strip()


def inline_nodes_from_segment(segment: dict[str, Any]) -> list[Any]:
    """部分实现（NapCat 等）会把节点直接塞在 forward 段里，能省一次 API 调用。"""
    data = segment.get("data") or {}
    for key in ("content", "messages", "message"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def normalize_forward_payload(payload: Any) -> list[Any]:
    """把 get_forward_msg 的各种返回形态归一成节点列表。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "message", "nodes", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def node_sender_name(node: dict[str, Any]) -> str:
    """取节点发送者显示名，兼容 node 段与扁平 message 记录两种形态。"""
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
    if not sender and isinstance(data.get("sender"), dict):
        sender = data["sender"]
    for candidate in (
        data.get("nickname"),
        data.get("name"),
        sender.get("card"),
        sender.get("nickname"),
        node.get("nickname"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    for candidate in (data.get("user_id"), data.get("uin"), sender.get("user_id"), node.get("user_id")):
        text = str(candidate or "").strip()
        if text:
            return text
    return "未知用户"


def node_segments(node: dict[str, Any]) -> list[Any]:
    """取节点正文消息段。"""
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    for source in (data, node):
        for key in ("content", "message", "messages"):
            value = source.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
            if isinstance(value, str) and value.strip():
                return [{"type": "text", "data": {"text": value}}]
    return []


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


def render_plain_segments(segments: list[Any]) -> str:
    """渲染节点正文，媒体只留占位，不做任何下载或解析。"""
    parts: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            parts.append(segment)
            continue
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if segment_type == "text":
            parts.append(str(data.get("text") or ""))
        elif segment_type == "image":
            sticker_type = data.get("subType", data.get("sub_type", data.get("type", "")))
            parts.append("[表情包]" if str(sticker_type) == "1" else "[图片]")
        elif segment_type in _MEDIA_PLACEHOLDERS:
            parts.append(_MEDIA_PLACEHOLDERS[segment_type])
        elif segment_type == "face":
            parts.append("[QQ表情]")
        elif segment_type == "at":
            qq = str(data.get("qq") or "").strip()
            parts.append(f"@{qq}" if qq else "@")
        elif segment_type == "reply":
            parts.append("[回复]")
        elif segment_type in ("forward", "node"):
            # 嵌套记录由调用方递归处理，这里不重复渲染
            continue
        elif segment_type == "json":
            parts.append("[卡片消息]")
        elif segment_type == "poke":
            parts.append("[戳一戳]")
        else:
            parts.append(f"[{segment_type}]")
    return _truncate("".join(parts), NODE_TEXT_MAX_CHARS)


def nested_forward_from_segments(segments: list[Any]) -> dict[str, Any] | None:
    """找出节点正文里的嵌套聊天记录（forward 段或直接的 node 列表）。"""
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "")
        if segment_type == "forward":
            return {
                "forward_id": forward_id_from_segment(segment),
                "inline_nodes": inline_nodes_from_segment(segment),
            }
        if segment_type == "node":
            return {"forward_id": "", "inline_nodes": [segment]}
    return None


class OneBotForwardMixin:
    """把合并转发展开成结构化文本；依赖宿主提供 `call_api`。"""

    async def _fetch_forward_nodes(self, forward_id: str) -> list[Any]:
        if not forward_id or not hasattr(self, "call_api"):
            return []
        # 不同实现对参数名不统一：标准用 id，部分 NapCat 版本认 message_id
        for params in ({"id": forward_id}, {"message_id": forward_id}):
            try:
                response = await self.call_api("get_forward_msg", params)
            except Exception as exc:
                logger.debug("[onebotv11] get_forward_msg %s failed: %s", params, exc)
                continue
            nodes = normalize_forward_payload(response.get("data"))
            if nodes:
                return nodes
        return []

    async def render_forward_record(
        self,
        *,
        forward_id: str = "",
        inline_nodes: list[Any] | None = None,
        max_depth: int = INBOUND_MAX_DEPTH,
        max_nodes: int = INBOUND_MAX_NODES,
        max_chars: int = INBOUND_MAX_CHARS,
    ) -> str:
        """渲染一条聊天记录。返回带 `[聊天记录]` 包裹的多行文本。"""
        budget = {"nodes": max_nodes, "chars": max_chars, "truncated": False}
        lines = await self._render_forward_level(
            forward_id=forward_id,
            inline_nodes=inline_nodes or [],
            depth=0,
            max_depth=max_depth,
            budget=budget,
        )
        if not lines:
            hint = f" id={forward_id}" if forward_id else ""
            return f"[聊天记录{hint}: 内容为空或无法获取]"

        header = f"[聊天记录 id={forward_id}]" if forward_id else "[聊天记录]"
        body = "\n".join(lines)
        tail = "[/聊天记录]"
        if budget["truncated"]:
            note = "…内容过长已截断"
            if forward_id:
                note += f"，需要完整内容用 onebotv11_get_chat_history(\"{forward_id}\")"
            tail = f"{note}\n{tail}"
        return f"{header}\n{body}\n{tail}"

    async def _render_forward_level(
        self,
        *,
        forward_id: str,
        inline_nodes: list[Any],
        depth: int,
        max_depth: int,
        budget: dict[str, Any],
    ) -> list[str]:
        nodes = inline_nodes or await self._fetch_forward_nodes(forward_id)
        if not nodes:
            return []

        indent = "  " * depth
        lines: list[str] = []
        for node in nodes:
            if budget["nodes"] <= 0 or budget["chars"] <= 0:
                budget["truncated"] = True
                break
            if not isinstance(node, dict):
                continue
            budget["nodes"] -= 1

            segments = node_segments(node)
            name = node_sender_name(node)
            text = render_plain_segments(segments)
            line = f"{indent}{name}: {text}" if text else f"{indent}{name}:"
            budget["chars"] -= len(line)
            lines.append(line)

            nested = nested_forward_from_segments(segments)
            if not nested:
                continue
            if depth + 1 >= max_depth:
                lines.append(f"{indent}  [嵌套聊天记录: 层级过深已省略]")
                continue
            nested_lines = await self._render_forward_level(
                forward_id=nested["forward_id"],
                inline_nodes=nested["inline_nodes"],
                depth=depth + 1,
                max_depth=max_depth,
                budget=budget,
            )
            if nested_lines:
                lines.append(f"{indent}  [聊天记录]")
                lines.extend(nested_lines)
                lines.append(f"{indent}  [/聊天记录]")
            else:
                lines.append(f"{indent}  [嵌套聊天记录: 无法获取]")
        return lines
