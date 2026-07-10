"""Current conversation scene prompt helpers.

These helpers keep the model aware of the exact window it is answering in,
without adding visible prefixes to every outgoing message.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from core.conversation_identity import ConversationIdentity, PRIVATE_CHAT_TYPE


def _canonical_scene_marker(platform: str, chat_type: str, platform_chat_id: str) -> str:
    """构造某场景的规范投递标记 `[platform:X][scene:type:id]`。"""
    platform = _clean(platform).lower()
    chat_type = _clean(chat_type).lower() or PRIVATE_CHAT_TYPE
    chat_id = _clean(platform_chat_id)
    return f"[platform:{platform}][scene:{chat_type}:{chat_id}]"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _platform_label(platform: str) -> str:
    value = _clean(platform).lower()
    if value == "onebotv11":
        return "QQ / OneBot v11"
    if value == "telegram":
        return "Telegram"
    return value or "unknown"


def _chat_label(identity: ConversationIdentity) -> str:
    platform = _clean(identity.platform).lower()
    chat_type = _clean(identity.chat_type).lower() or PRIVATE_CHAT_TYPE
    if platform == "onebotv11":
        if chat_type == "group":
            return "QQ 群聊"
        if chat_type == "private":
            return "QQ 私聊"
        return f"QQ {chat_type}"
    if platform == "telegram":
        if chat_type == "group":
            return "Telegram 群聊"
        if chat_type == "private":
            return "Telegram 私聊"
        return f"Telegram {chat_type}"
    if chat_type == "group":
        return f"{platform or 'unknown'} 群聊"
    if chat_type == "private":
        return f"{platform or 'unknown'} 私聊"
    return f"{platform or 'unknown'} {chat_type}"


def _format_optional_count(value: Any, status: str = "") -> str:
    if value is None or value == "":
        return f"未知（{status}）" if status else "未知"
    return str(value)


def build_current_scene_block(
    identity: ConversationIdentity,
    context: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build an internal prompt block describing the current reply target.

    The block is for model context only. User-visible messages should remain
    natural; this is not a request to prefix every reply.
    """
    context = context or {}
    chat_type = _clean(identity.chat_type).lower() or PRIVATE_CHAT_TYPE
    actor = (
        _clean(identity.actor_display_name)
        or _clean(context.get("user_name"))
        or _clean(identity.actor_user_id)
        or "未知"
    )
    actor_role = "主人" if identity.is_owner else "访客/群友"
    actor_user_id = _clean(identity.actor_user_id) or _clean(context.get("user_id"))
    reply_target = identity.runtime_key or f"{identity.platform}:{identity.platform_chat_id}"

    lines = [
        "【当前会话场景 (Scene / Reply Target)】",
        f"- 当前平台: {_platform_label(identity.platform)}",
        f"- 当前窗口: {_chat_label(identity)}",
        f"- 回复目标: {reply_target}",
        f"- 当前说话人: {actor}（{actor_role}）",
        "- 规则: 本轮所有用户可见回复默认只发回上述回复目标；不要把其它窗口的任务进度当成当前窗口正在讨论的话题。",
        "- 规则: 历史里带 `[来自 platform:chat_id / 昵称]` 的内容来自别处，只能作为背景；提到它时要自然区分场景，不能原样复述标签。",
    ]

    if chat_type == "group":
        if actor_user_id:
            lines.append(f"- 当前说话人平台用户 ID: {actor_user_id}；若明确需要原生 @ 当前说话人，可使用 `[at:{actor_user_id}]`。")
        group_title = _clean(context.get("chat_title") or context.get("group_title") or context.get("group_display_name"))
        if group_title:
            lines.append(f"- 群聊名称: {group_title}")
        member_count = context.get("group_member_count")
        member_status = _clean(context.get("group_member_count_status"))
        if member_count is not None or member_status:
            lines.append(f"- 群成员总数: {_format_optional_count(member_count, member_status)}")
        lines.append("- 群聊边界: 群里可能有其他人在场；不要泄露私聊内容、主人隐私或其它窗口的后台任务细节。")
    else:
        lines.append("- 私聊边界: 这是当前私聊窗口；文件、结果和进度默认发回这里，除非用户明确要求转发到群聊或其它窗口。")

    # 跨场景投递提示：默认无需标记；仅当要发去别处时用规范标记。
    self_marker = _canonical_scene_marker(identity.platform, chat_type, identity.platform_chat_id)
    lines.append(
        f"- 投递默认: 回复默认发回当前场景，无需任何标记。若确需发去别的平台/场景，才在整条消息最前面加 `[platform:X][scene:type:id]`（type 为 group/private）。本场景标记为 {self_marker}。"
    )

    known = _known_scene_lines(identity)
    if known:
        lines.append("- 已知可投递场景（仅这些是确认存在的合法目标，不要编造其它 id）:")
        lines.extend(f"  · {line}" for line in known)

    return "\n".join(lines)


def _known_scene_lines(current: ConversationIdentity) -> list[str]:
    """列出已知活跃场景的规范标记，供模型选择合法投递目标。"""
    try:
        from core.delivery_target_store import load_known_scenes
        scenes = load_known_scenes()
    except Exception:
        return []
    out: list[str] = []
    cur_key = _clean(current.runtime_key)
    for scene in scenes:
        platform = _clean(scene.get("platform"))
        chat_id = _clean(scene.get("platform_chat_id"))
        chat_type = _clean(scene.get("chat_type")) or PRIVATE_CHAT_TYPE
        if not platform or not chat_id:
            continue
        marker = _canonical_scene_marker(platform, chat_type, chat_id)
        tag = "（当前）" if _clean(scene.get("runtime_key")) == cur_key else ""
        out.append(f"{marker}{tag}")
    return out


def should_redact_cross_place_details(
    identity: ConversationIdentity,
    other_runtime_key: str,
) -> bool:
    """Return whether details from another runtime should be hidden here.

    Owner private chats may see cross-platform task details. Group chats and
    visitor conversations should only know that another window is busy.
    """
    other = _clean(other_runtime_key)
    if not other or other == identity.runtime_key:
        return False
    chat_type = _clean(identity.chat_type).lower() or PRIVATE_CHAT_TYPE
    return chat_type != PRIVATE_CHAT_TYPE or not identity.is_owner
