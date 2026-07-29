"""本轮用户消息的历史去重（按数据库行 id，而不是按内容字符串）。

背景与历史教训：
    所有大脑的输入都是「历史上下文 + 当前用户消息」，而当前用户消息在生成前
    就已经写进了 messages 表，因此历史里必然包含它一次，必须在拼 history 时
    剔除，否则模型会看到同一句话两遍，进而反馈"你发了两次"。

    过去这一步靠字符串相等判断（拿内存里的原文和历史尾部的 content 比较），
    但写库侧 `add_message` 会**改写**内容：注入时间戳前缀、跨地点来源标签
    `[来自 …]`、表情包描述 `[表情包: …]`；群聊提升批次更是把多条消息重排成
    另一种格式。每次新增一种内容加工，字符串比较就重新失配，去重静默失效。

    所以这里改为按 `add_message` 返回的自增行 id 去重：写库时把 id 记进
    context，下游按 id 排除。内容怎么加工都不影响。字符串比较只保留为
    没有 id 的旧调用方兜底。
"""

from typing import Any, Dict, List, Optional, Set

from core.routing import strip_timestamp_markers

PERSISTED_USER_MESSAGE_IDS_KEY = "_persisted_user_message_ids"


def record_persisted_user_message(context: Optional[Dict[str, Any]], message_id: Any) -> None:
    """把刚写入 messages 表的用户消息行 id 记录到 context，供下游按 id 去重。"""
    if not isinstance(context, dict):
        return
    try:
        value = int(message_id)
    except (TypeError, ValueError):
        return
    if value <= 0:
        return
    ids = context.get(PERSISTED_USER_MESSAGE_IDS_KEY)
    existing = list(ids) if isinstance(ids, (list, tuple)) else []
    if value in existing:
        return
    existing.append(value)
    # always rebind a fresh list: context 常被 dict(...) 浅拷贝传递，
    # 原地 append 会串改上游 context 的同一个列表。
    context[PERSISTED_USER_MESSAGE_IDS_KEY] = existing


def persisted_user_message_ids(context: Optional[Dict[str, Any]]) -> Set[int]:
    """读取 context 中记录的本轮用户消息行 id 集合。"""
    if not isinstance(context, dict):
        return set()
    raw = context.get(PERSISTED_USER_MESSAGE_IDS_KEY)
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    ids: Set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ids.add(value)
    return ids


def clear_persisted_user_messages(context: Optional[Dict[str, Any]]) -> None:
    """清除记录。用于 context.text 已被改写成新指示、原用户消息不再是本轮输入时。"""
    if isinstance(context, dict):
        context.pop(PERSISTED_USER_MESSAGE_IDS_KEY, None)


def merge_persisted_user_messages(
    context: Optional[Dict[str, Any]],
    sources: Any,
) -> None:
    """把若干来源（context / id 可迭代对象）里的用户消息 id 合并进 context。"""
    if not isinstance(context, dict):
        return
    for source in sources or []:
        if isinstance(source, dict):
            values = persisted_user_message_ids(source)
        elif isinstance(source, (list, tuple, set)):
            values = {int(v) for v in source if str(v).lstrip("-").isdigit()}
        else:
            continue
        for value in sorted(values):
            record_persisted_user_message(context, value)


def _message_row_id(msg: Any) -> Optional[int]:
    if not isinstance(msg, dict):
        return None
    try:
        return int(msg.get("message_id"))
    except (TypeError, ValueError):
        return None


def drop_current_user_message(
    db_context: List[Dict[str, Any]],
    context: Optional[Dict[str, Any]],
    fallback_content: str = "",
) -> List[Dict[str, Any]]:
    """从历史上下文中剔除"本轮正在作为当前输入的用户消息"。

    优先按 context 里记录的行 id 精确剔除（可命中任意位置、任意条数，也能覆盖
    群聊批次这种"一次输入对应多条库记录"的情况）；只有在完全没有 id 记录时，
    才退回旧的"尾部内容相等"比较，兼容不经过写库路径的调用方与既有测试。
    """
    if not db_context:
        return list(db_context or [])

    ids = persisted_user_message_ids(context)
    if ids:
        return [
            msg
            for msg in db_context
            if not (
                isinstance(msg, dict)
                and msg.get("role") == "user"
                and _message_row_id(msg) in ids
            )
        ]

    result = list(db_context)
    if fallback_content:
        last = result[-1]
        if isinstance(last, dict) and last.get("role") == "user":
            last_content = strip_timestamp_markers(str(last.get("content", "")))
            if last_content == strip_timestamp_markers(str(fallback_content)):
                result.pop()
    return result
