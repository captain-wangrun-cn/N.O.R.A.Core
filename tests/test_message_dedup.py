"""本轮用户消息去重（按行 id）。

回归背景：入库时 add_message 会改写内容（时间戳前缀、来源标签、表情包描述，
群聊批次还会整体重排），过去按字符串相等剔除历史里的当前消息，每加一种内容
加工就静默失配一次，模型于是看到同一句话两遍并反馈"你发了两次"。
"""

from core.message_dedup import (
    PERSISTED_USER_MESSAGE_IDS_KEY,
    clear_persisted_user_messages,
    drop_current_user_message,
    merge_persisted_user_messages,
    persisted_user_message_ids,
    record_persisted_user_message,
)


def test_record_ignores_invalid_ids():
    context = {}
    for bad in (None, "abc", 0, -3, ""):
        record_persisted_user_message(context, bad)
    assert persisted_user_message_ids(context) == set()

    record_persisted_user_message(context, "17")
    record_persisted_user_message(context, 17)  # 重复不累加
    record_persisted_user_message(context, 18)
    assert persisted_user_message_ids(context) == {17, 18}


def test_record_does_not_mutate_shallow_copied_parent():
    """context 常被 dict(...) 浅拷贝传递，原地 append 会串改上游的同一个列表。"""
    parent = {}
    record_persisted_user_message(parent, 1)
    child = dict(parent)
    record_persisted_user_message(child, 2)

    assert persisted_user_message_ids(parent) == {1}
    assert persisted_user_message_ids(child) == {1, 2}


def test_clear_and_merge():
    context = {}
    merge_persisted_user_messages(context, [{PERSISTED_USER_MESSAGE_IDS_KEY: [3, 4]}, [5], None])
    assert persisted_user_message_ids(context) == {3, 4, 5}

    clear_persisted_user_messages(context)
    assert persisted_user_message_ids(context) == set()
    assert PERSISTED_USER_MESSAGE_IDS_KEY not in context


def test_drop_by_row_id_regardless_of_content_rewrites():
    """内容被改写（时间戳 + 来源标签 + 昵称前缀）也要精确命中。"""
    db_context = [
        {"role": "system", "content": "[压缩段] 摘要", "message_id": 1},
        {"role": "user", "content": "[2026-07-29 17:11:45 Wednesday] 老问题", "message_id": 2},
        {"role": "assistant", "content": "回答", "message_id": 3},
        {
            "role": "user",
            "content": "[2026-07-29 17:12:03 Wednesday] [来自 telegram / 小明] User: 新的问题",
            "message_id": 4,
        },
    ]
    context = {}
    record_persisted_user_message(context, 4)

    result = drop_current_user_message(db_context, context, "新的问题")

    assert [m.get("message_id") for m in result] == [1, 2, 3]


def test_drop_by_row_id_removes_all_ids_of_a_group_batch():
    """群聊一次输入对应多条库记录，批次的每条都要被剔除（不在尾部也要命中）。"""
    db_context = [
        {"role": "user", "content": "[ts] Alice: 第一句", "message_id": 10},
        {"role": "user", "content": "[ts] Bob: 第二句", "message_id": 11},
        {"role": "assistant", "content": "旧回复", "message_id": 12},
    ]
    context = {}
    merge_persisted_user_messages(context, [[10, 11]])

    result = drop_current_user_message(db_context, context, "Alice: 第一句\nBob: 第二句")

    assert [m.get("message_id") for m in result] == [12]


def test_fallback_tail_compare_when_no_ids_recorded():
    """没有 id 记录的旧调用方仍走尾部内容比较，并且两侧都要剥时间戳。"""
    db_context = [
        {"role": "assistant", "content": "旧回复"},
        {"role": "user", "content": "[2026-07-29 17:11:45 Wednesday] 新的问题"},
    ]

    result = drop_current_user_message(db_context, {}, "新的问题")
    assert [m["content"] for m in result] == ["旧回复"]

    # 内容不同则不动
    keep = drop_current_user_message(db_context, {}, "别的问题")
    assert len(keep) == 2


def test_empty_inputs_are_safe():
    assert drop_current_user_message([], {}, "x") == []
    assert drop_current_user_message(None, None, "") == []
