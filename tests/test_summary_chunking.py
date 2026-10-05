"""分块降级（切小）的回归测试。

这是「有缺口绝不写」这条约定唯一的执行点：第一版实现只合并了通过的那些块，
产出一份**看起来正常但有缺口**的摘要 —— 静默损坏比彻底失败更危险，
因为它不会被任何人发现。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.summary_chunking import (
    DEFAULT_CHUNK_CHAR_CAP,
    MIN_CHUNK_SIZE,
    chunk_messages,
    chunked_summary,
)

BLOCK = "<<BLOCK>>"


def _render(text: str) -> str:
    return text


def _make_call(blocked_predicate):
    """blocked_predicate(text) -> True 表示这次调用被拦（返回空串）。"""
    calls = []

    async def _call(prompt: str) -> str:
        calls.append(prompt)
        if blocked_predicate(prompt):
            return ""
        return f"总结({prompt[:12]})"

    _call.calls = calls
    return _call


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------

def test_chunk_messages_splits_by_size():
    assert chunk_messages(list("abcdefg"), 3) == [list("abc"), list("def"), list("g")]


def test_chunk_messages_handles_empty_and_oversize():
    assert chunk_messages([], 3) == []
    assert chunk_messages([1], 0) == [[1]]  # size 夹到 1，不会除零或死循环


# --------------------------------------------------------------------------
# 分块摘要
# --------------------------------------------------------------------------

def test_all_chunks_pass_returns_joined_summary():
    call = _make_call(lambda p: False)
    out = asyncio.run(chunked_summary(["a1", "a2", "a3", "b1", "b2"], call, _render, chunk_size=3))
    assert out is not None
    assert out.count("总结(") == 2, "5 条 / 每块 3 条 = 2 块"


def test_blocked_chunk_is_bisected_and_recovered():
    """块被拦时劈半重试 —— 这是救回生产上 7 条硬拦截会话的机制。"""
    # 只有「恰好整块三条一起」时被拦，单独两条/一条都能过。
    def blocked(prompt: str) -> bool:
        return prompt.count("b1") and prompt.count("b2") and prompt.count("b3")

    call = _make_call(blocked)
    out = asyncio.run(chunked_summary(
        ["b1", "b2", "b3"], call, _render, chunk_size=3
    ))
    assert out is not None, "劈半后应能恢复"
    assert len(call.calls) > 1, "应该发生了二分重试"


def test_single_message_still_blocked_returns_none():
    """一条消息自己过不去 = 无解，必须整体放弃，不能返回部分结果。"""
    call = _make_call(lambda p: "毒" in p)
    out = asyncio.run(chunked_summary(["正常1", "毒", "正常2"], call, _render, chunk_size=3))
    assert out is None, "有缺口时绝不能返回拼接结果"


def test_empty_input_returns_none():
    call = _make_call(lambda p: False)
    assert asyncio.run(chunked_summary([], call, _render)) is None


def test_truncation_cap_applied_per_message():
    """实测：每条截到 300 字 = 4/4 通过，不截 = 0/4。上限必须真的生效。"""
    seen = []

    async def _call(prompt: str) -> str:
        seen.append(prompt)
        return "ok"

    long_msg = "字" * 1000
    asyncio.run(chunked_summary([long_msg], _call, _render, chunk_size=3,
                                char_cap=DEFAULT_CHUNK_CHAR_CAP))
    assert len(seen[0]) < 1000
    assert DEFAULT_CHUNK_CHAR_CAP < 1000


def test_min_chunk_size_is_one():
    """只剩 1 条还过不去就放弃 —— 再切没有意义，且能保证递归终止。"""
    assert MIN_CHUNK_SIZE == 1
