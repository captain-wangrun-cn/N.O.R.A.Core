"""端到端回归：真实 MessageHistory + 生产时间戳格式下，本轮用户消息只能出现一次。

这个场景过去没有覆盖，导致「AI 说用户发了两次」的问题活了好几个月：
add_message 会把内容改写成 "[<时间>] <原文>"，而去重侧按字符串相等比较，
时间戳格式一变（加秒、加星期）就静默失配。
"""

import asyncio
from pathlib import Path

from core.message_dedup import record_persisted_user_message
from core.message_handler import group_message_content
from memory.message_history import MessageHistory

from test_front_brain_prompt import _FrontBrainProbe

LIVE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S %A"  # 生产默认值（含秒与星期）


def _make_history(tmp_path: Path) -> MessageHistory:
    """完全隔离在临时目录的 MessageHistory（不触碰生产库）。"""
    return MessageHistory(
        db_path=str(tmp_path / "history.db"),
        mirror_db_path=str(tmp_path / "mirror.db"),
        context_db_path=str(tmp_path / "context.db"),
        raw_window=80,
        compress_window=9999,
        archive_threshold=99999,
        timestamp_format=LIVE_TIMESTAMP_FORMAT,
    )


def _store(
    history: MessageHistory,
    probe: _FrontBrainProbe,
    context: dict,
    role: str,
    content: str,
) -> int:
    """按生产路径（同一套五层身份）写一条消息，返回行 id。"""
    identity = probe._identity(context)
    return history.add_message(
        platform=identity.platform,
        chat_id=identity.storage_id,
        role=role,
        content=content,
        user_id=context["user_id"] if role == "user" else "assistant",
        memory_scope_id=identity.memory_scope_id,
        place_scope_id=identity.place_scope_id,
    )


def _store_user_message(
    history: MessageHistory, probe: _FrontBrainProbe, context: dict, text: str
) -> int:
    """写入本轮用户消息（含群聊昵称前缀加工），并把返回的行 id 记进 context。"""
    content = group_message_content(
        context, context["user_name"], text, context["chat_type"]
    )
    message_id = _store(history, probe, context, "user", content)
    record_persisted_user_message(context, message_id)
    return message_id


def _run_front_brain(probe: _FrontBrainProbe, context: dict):
    async def _main():
        return await probe._generate_front_chat_response(context)

    return asyncio.run(_main())


def _base_context(chat_type: str = "private") -> dict:
    return {
        "platform": "onebotv11",
        "chat_id": "private-chat" if chat_type == "private" else "10001",
        "user_id": "20002",
        "chat_type": chat_type,
        "user_name": "User",
        "text": "今天天气怎么样",
    }


def test_front_brain_sees_current_message_exactly_once_private(tmp_path):
    history = _make_history(tmp_path)
    context = _base_context("private")

    probe = _FrontBrainProbe(["今天多云。"])
    probe.message_history = history

    _store(history, probe, context, "user", "之前的旧问题")
    _store_user_message(history, probe, context, context["text"])

    _run_front_brain(probe, context)

    sent_history = probe.histories[0]
    occurrences = sum(1 for m in sent_history if context["text"] in str(m["content"]))
    assert occurrences == 0, f"本轮消息不应出现在 history 中（已作为 user_prompt 传入）: {sent_history}"
    assert any("之前的旧问题" in str(m["content"]) for m in sent_history)


def test_front_brain_sees_current_message_exactly_once_group(tmp_path):
    """群聊写入会加 "昵称: " 前缀，字符串比较必然失配，按 id 才命中。"""
    history = _make_history(tmp_path)
    context = _base_context("group")

    probe = _FrontBrainProbe(["今天多云。"])
    probe.message_history = history

    _store_user_message(history, probe, context, context["text"])

    _run_front_brain(probe, context)

    sent_history = probe.histories[0]
    assert not [m for m in sent_history if context["text"] in str(m["content"])], sent_history


def test_front_brain_history_has_no_raw_timestamp_prefix(tmp_path):
    """带秒与星期的时间戳必须被剥掉，不能泄漏进模型可见历史。"""
    history = _make_history(tmp_path)
    context = _base_context("private")

    probe = _FrontBrainProbe(["今天多云。"])
    probe.message_history = history

    _store(history, probe, context, "assistant", "上一轮回答")
    _store_user_message(history, probe, context, context["text"])

    _run_front_brain(probe, context)

    contents = [str(m["content"]) for m in probe.histories[0]]
    assert contents, "历史不应为空"
    assert not [c for c in contents if c.lstrip().startswith("[20")], contents
    assert any(c.strip() == "上一轮回答" for c in contents), contents
