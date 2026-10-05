"""摘要输入的分块与递归二分 —— 输入侧内容拦截的正解。

为什么必须做这件事（生产事故）：provider 的失败约定是「返回错误文本而不是抛异常」，
压缩链路当时没检查这个信号，于是那段错误说明被当成摘要写进了库，**同时**被压缩的消息被标成
`is_archived=1`，原文从此不再进上下文 —— 永久丢失。

但是"加守卫"只解决了一半：守卫上线后，被拦的消息会从**写脏数据**变成**什么都不写**，
记忆照样丢。真正让它能记住的是这里 —— 把过不去的内容切小到能过去。

实测（session 722 生产重放，全部可复现）：

| 送进去的内容 | 结果 |
|---|---|
| 原始 9 条 | 0/4 通过 |
| 去掉"触发条" | 5/5 全灭（**不是某条消息有毒**） |
| 每条截断到 300 字 | 4/4 通过 |
| 切成 3 条一块 | 通过；个别块被拦，劈半后全部恢复 |

判据是**聚合密度**：单条看都是无害碎片，拼起来才构成触发模式。所以「摘掉某条」没用，
「切小 / 截断 / 中性化」才有用。

⚠️ **有缺口绝不写。** 第一版实现只合并了通过的那些块，产出一份**看起来正常但有缺口**的
摘要 —— 静默损坏比彻底失败更危险，因为它不会被任何人发现。
`chunked_summary()` 在无法填满任一区间时返回 None，调用方必须保留原状（不写、不标记已归档）。

⚠️ 历史时间戳前缀（`[2026-10-05 12:00:00 Sunday] `）是**入库时冻结**的（见 pitfalls 5.17），
分块时必须原样带着走，否则摘要里的时间顺序就没了。
"""

import logging
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 3
# 单条上限。实测 200 / 300 都稳过，取中间值留出余量。
# 注意这是「截断」，不是「中性化」—— 无差别砍，会连事件一起砍掉一部分。
DEFAULT_CHUNK_CHAR_CAP = 250
# 二分下限：一块只剩 1 条还过不去，就是这条本身过不去，再切没有意义。
MIN_CHUNK_SIZE = 1

# 真正干活的 LLM 调用：给定一段对话原文，返回总结文本。
#
# ⚠️ 这个回调**必须自带重试**再对上层报失败。分块层把一次空返回理解为"这段内容过不去"
# 就直接劈半，而输出侧的 content_filter 是随机的——只试一次的话，二分树会被随机失败
# 引着把本来能过的消息一路劈到单条，最后误判"无解"整条放弃。
# 调用方见 `MessageHistory._chunked_fallback`（它内部走 `summary_retry.call_with_retry`）。
SummaryCall = Callable[[str], Awaitable[str]]


def _clip(text: str, cap: int) -> str:
    """按字符上限截断单条消息（保留开头，那通常是真正发生了什么的地方）。"""
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + "…（本条已截断）"


def chunk_messages(messages: Sequence[Any], size: int) -> List[List[Any]]:
    """把消息切成固定大小的块。"""
    step = max(1, int(size))
    return [list(messages[i:i + step]) for i in range(0, len(messages), step)]


async def _summarize_range(
    messages: Sequence[Any],
    call: SummaryCall,
    render: Callable[[str], str],
    chunk_size: int,
    char_cap: int,
    depth: int = 0,
) -> Optional[str]:
    """总结一段消息；被拦就劈成两半分别总结，都成功才返回。

    返回 None 表示**这一段救不回来**，调用方必须整体放弃（不许用部分结果拼一份有缺口的摘要）。
    """
    text = "\n".join(_clip(str(m), char_cap) for m in messages)
    summary = await call(render(text))
    if summary:
        return summary

    if len(messages) <= MIN_CHUNK_SIZE:
        logger.error(
            "摘要分块：单条消息仍被拦截（%s 字符），该区间无解，放弃。"
            "（这一类是输入侧拦截，重试无效，只能靠更激进的中性化处理）",
            len(text),
        )
        return None

    mid = len(messages) // 2
    left = await _summarize_range(messages[:mid], call, render, chunk_size, char_cap, depth + 1)
    if left is None:
        return None
    right = await _summarize_range(messages[mid:], call, render, chunk_size, char_cap, depth + 1)
    if right is None:
        return None
    return f"{left}\n\n{right}"


async def chunked_summary(
    messages: Sequence[Any],
    call: SummaryCall,
    render: Callable[[str], str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    char_cap: int = DEFAULT_CHUNK_CHAR_CAP,
) -> Optional[str]:
    """整段阻塞时退化的分块摘要。全部块都成功才返回，否则 None。

    Args:
        messages: 已带历史时间戳前缀的消息文本序列。
        call: 单次摘要调用，返回空串/None 表示这次被拦。
        render: 把对话原文包成 user_prompt 的函数。
    """
    parts: List[Optional[str]] = []
    for piece in chunk_messages(messages, chunk_size):
        parts.append(await _summarize_range(piece, call, render, chunk_size, char_cap))

    if any(p is None for p in parts):
        logger.error("摘要分块：有区间无法总结，放弃整条摘要（不写有缺口的内容）")
        return None

    joined = "\n\n".join(p for p in parts if p)
    if not joined.strip():
        return None
    return joined
