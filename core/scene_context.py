"""Current conversation scene prompt helpers.

These helpers keep the model aware of the exact window it is answering in,
without adding visible prefixes to every outgoing message.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from adapters.base import normalize_mentioned_users
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


def _platform_message_ids(context: Mapping[str, Any]) -> list[str]:
    """按入站顺序整理当前模型轮次对应的平台消息 ID。"""
    values = context.get("platform_message_ids")
    if isinstance(values, (list, tuple)):
        raw_ids = list(values)
    else:
        raw_ids = []
    raw_ids.append(context.get("platform_message_id"))

    seen: set[str] = set()
    message_ids: list[str] = []
    for value in raw_ids:
        message_id = _clean(value)
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        message_ids.append(message_id)
    return message_ids


def _batch_participant_lines(context: Mapping[str, Any]) -> list[str]:
    """列出本批次每位说话人及其可靠 ID，供多人群聊 `[at:]` / `[reply:]` 取值。

    只有最后一条消息的发送者会进入「当前说话人」，多人同时说话时其余人在结构化信息里
    完全不存在，模型只能从正文猜 ID。这里按批次原始 context 逐条汇总，不做任何推断。
    """
    batch = context.get("group_message_batch")
    if not isinstance(batch, (list, tuple)) or len(batch) < 2:
        return []

    order: list[str] = []
    participants: dict[str, dict[str, Any]] = {}
    for item in batch:
        if not isinstance(item, Mapping):
            continue
        user_id = _clean(item.get("user_id"))
        name = _clean(item.get("user_name")) or _clean(item.get("actor_display_name"))
        key = user_id or name
        if not key:
            continue
        if key not in participants:
            order.append(key)
            participants[key] = {"user_id": user_id, "name": name or "未知", "message_ids": []}
        entry = participants[key]
        if not entry["name"] or entry["name"] == "未知":
            entry["name"] = name or entry["name"]
        for message_id in _platform_message_ids(item):
            if message_id not in entry["message_ids"]:
                entry["message_ids"].append(message_id)
        if item.get("explicit_bot_mention"):
            entry["mentioned_bot"] = True
        if item.get("reply_to_bot"):
            entry["replied_bot"] = True

    if len(participants) < 2:
        return []

    lines = [f"- 本批次说话人（共 {len(participants)} 人，每条消息的归属只能按此表和正文行前缀判断）:"]
    for key in order:
        entry = participants[key]
        parts = [entry["name"]]
        parts.append(f"平台用户 ID: {entry['user_id']}" if entry["user_id"] else "平台用户 ID: 未知")
        if entry["message_ids"]:
            parts.append(f"本批次消息 ID: {', '.join(entry['message_ids'])}")
        flags = []
        if entry.get("mentioned_bot"):
            flags.append("@ 了你")
        if entry.get("replied_bot"):
            flags.append("回复了你的消息")
        if flags:
            parts.append("、".join(flags))
        lines.append(f"  · {'；'.join(parts)}")
    lines.append(
        "- 多人归属规则: 回应谁、@ 谁、引用哪条，必须取自上表中该人自己的 ID，不能把某人的话算到另一个人头上，"
        "也不能默认所有内容都出自最后一位说话人。表中没有的人不要 @，禁止从昵称或正文猜测 ID。"
    )
    return lines


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
            lines.append(
                f"- 当前说话人平台用户 ID: {actor_user_id}；若要在多人群聊中直接面向这位已知参与者、自然插话并明确对象，可使用 `[at:{actor_user_id}]`。"
            )
        group_title = _clean(context.get("chat_title") or context.get("group_title") or context.get("group_display_name"))
        if group_title:
            lines.append(f"- 群聊名称: {group_title}")
        member_count = context.get("group_member_count")
        member_status = _clean(context.get("group_member_count_status"))
        if member_count is not None or member_status:
            lines.append(f"- 群成员总数: {_format_optional_count(member_count, member_status)}")
        mentioned_users = normalize_mentioned_users(context.get("mentioned_users"))
        if mentioned_users:
            labels = "、".join(
                f"{user['display_name']}（平台用户 ID: {user['user_id']}）"
                for user in mentioned_users
            )
            lines.append(f"- 当前入站消息中原生 @ 的已知成员: {labels}")
            lines.append(
                "- 提及成员规则: 昵称只用于理解提及对象；如需原生 @，仍必须使用该成员对应的可靠 `[at:USER_ID]`。"
                "禁止从昵称、正文或跨平台历史猜测用户 ID。"
            )
        lines.extend(_batch_participant_lines(context))
        lines.append("- 群聊边界: 群里可能有其他人在场；不要泄露私聊内容、主人隐私或其它窗口的后台任务细节。")
    else:
        lines.append("- 私聊边界: 这是当前私聊窗口；文件、结果和进度默认发回这里，除非用户明确要求转发到群聊或其它窗口。")

    message_ids = _platform_message_ids(context)
    if len(message_ids) == 1:
        lines.append(f"- 当前入站消息 ID: {message_ids[0]}")
    elif message_ids:
        lines.append(f"- 当前聚合入站消息 IDs（按输入顺序）: {', '.join(message_ids)}")

    reply_to_message_id = _clean(context.get("reply_to_message_id"))
    if reply_to_message_id:
        lines.append(f"- 用户当前消息引用的历史消息 ID: {reply_to_message_id}")
    reply_target_message_id = _clean(context.get("reply_target_message_id"))
    if reply_target_message_id:
        lines.append(f"- 应用已选择的出站引用目标 ID: {reply_target_message_id}")

    if message_ids or reply_to_message_id or reply_target_message_id:
        lines.append(
            "- 消息 ID 规则: 回应、纠正或承接某条具体消息，尤其在多人或多分支对话中需要明确目标时，可使用 `[reply:MESSAGE_ID]`；"
            "看到 ID 不代表必须回复或自动引用。只能使用当前目标平台与场景明确提供的可靠 ID，禁止从昵称、正文或历史猜测；禁止猜测、转换或跨平台/跨场景复用。"
        )
        if chat_type == "group":
            lines.append(
                "- 群聊控制标记分寸: 普通群回复、泛泛群体回应和例行寒暄不必引用或 @，不要每条都加。"
                "在 QQ / OneBot v11 群里，若是在直接回应某位群友的某条具体消息，通常优先同时使用 `[reply:MESSAGE_ID]` 和 `[at:USER_ID]`，模拟 QQ 左滑回复时的“回复 + @ 发送人”，让消息分支和接收人都明确。"
                "只有不需要打扰或提醒对方、只是引用其话语作为讨论材料时，才通常只用 `[reply:...]`；其它平台仍按平台协议和实际歧义选择。"
            )

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
