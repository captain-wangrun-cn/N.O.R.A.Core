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
import re
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 分块结果的拼装方式。
#
# 时间线条目在**行首**，且行内不会出现 `【`（字段名只在行首出现）。
# 所以按行走一遍，就能在不知道块边界在哪的情况下把「同号槽位的碎片」和
# 「被块内模型误当成对话续写的散文」区分开——后者既不是时间线条目、
# 也不属于任何已知字段，直接丢掉。
_LINE_START = re.compile(r"^[\-\*•]\s*")
_FIELD_RE = re.compile(r"【([^】]+)】")
# 允许行首还有 - * • # 等装饰（`## 【时间线】`、`- 【时间线】` 都算）。
_FIELD_LINE_RE = re.compile(r"^[\s\-\*•#>]*【([^】]+)】")

TIMELINE_NAME = "时间线"

MERGE_MODE_SECTIONS = "sections"
MERGE_MODE_CONCAT = "concat"

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


def has_timeline(text: str) -> bool:
    """返回文本是否含【时间线】字段（带装饰也算）。

    为什么结构判据不能只看字符长度：provider 的失败说明与截断原文
    **都长过任何合理的字数阈值**，长度只能拦住空返回，拦不住"内容不是摘要"。
    六字段格式里的【时间线】是模型真做了归纳才会出现的标记，拿它当判据才拦得住。
    """
    return any(m.group(1).strip() == TIMELINE_NAME for m in _FIELD_LINE_RE.finditer(text or ""))


def _merge_concatenated(parts: Sequence[str]) -> str:
    """把块摘要按行分类拼装，丢掉不属于任何已知字段的行。

    两件事在这里完成：

    1. **同号槽位的碎片合并。** 每个块都会独立输出【时间线】【状态】【隐私边界】，
       直接拼接会得到三四份重复段落。所以【时间线】的条目要合到一份里，
       其它字段同名合并、不同名追加——同一个槽位不论切多少块，最终只有一个。
    2. **丢掉"入戏散文"。** 小块露骨内容会让模型以为自己在对话里，以 Nora 的身份
       续写场景描述（实测 231 字）。它不是 provider 报错，守卫拦不住；但它既不是
       时间线条目、也没有字段名，按行分类时自然落进"无归属"里被丢弃。

    ⚠️ 因此**不要切换成人称/口吻聚合**：那会把入戏散文正好归进用户侧聚合里，
    等于亲手把它救回来。
    """
    timeline: List[str] = []
    sections: List[str] = []
    seen_sections = set()
    cur_title: Optional[str] = None
    cur_lines: List[str] = []
    dropped = 0

    def _flush() -> None:
        if cur_title is None:
            return
        body = "\n".join(cur_lines).strip()
        if not body:
            return
        if cur_title in seen_sections:
            return  # 同名非时间线字段以首个为准，不做无标记拼接
        seen_sections.add(cur_title)
        sections.append(f"【{cur_title}】\n{body}")

    for text in parts:
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            m = _FIELD_LINE_RE.match(line)
            if m:
                _flush()
                title = m.group(1).strip()
                cur_title = title
                rest = line[m.end():].strip()
                cur_lines = [rest] if rest else []
                continue
            if cur_title == TIMELINE_NAME:
                stripped = _LINE_START.sub("", line.strip())
                if stripped and not stripped.startswith("（"):  # 吃掉"(无)"这类占位
                    timeline.append(stripped)
                continue
            if cur_title is not None:
                if _LINE_START.match(line.strip()) or not _FIELD_RE.search(line):
                    cur_lines.append(line)
                else:
                    dropped += 1
                continue
            dropped += 1  # 既不属时间线也不属任何字段：入戏散文
    _flush()

    if dropped:
        logger.warning("摘要分块：丢弃 %d 行无归属内容（字段外的散文/续写）", dropped)

    blocks: List[str] = []
    if timeline:
        blocks.append(f"【{TIMELINE_NAME}】\n" + "\n".join(f"- {t}" for t in timeline))
    blocks.extend(sections)
    return "\n\n".join(blocks)


def merge_chunk_summaries(parts: Sequence[str], mode: str = MERGE_MODE_SECTIONS) -> str:
    """按 mode 拼装块摘要：sections 走结构化合并，concat 直接拼接。"""
    kept = [p for p in parts if p and p.strip()]
    if not kept:
        return ""
    if mode == MERGE_MODE_SECTIONS:
        return _merge_concatenated(kept)
    return "\n\n".join(kept)


async def _summarize_range(
    messages: Sequence[Any],
    call: SummaryCall,
    render: Callable[[str], str],
    chunk_size: int,
    char_cap: int,
    depth: int = 0,
    merge_mode: str = MERGE_MODE_CONCAT,
    enforce_timeline: bool = False,
) -> Optional[str]:
    """总结一段消息；被拦就劈成两半分别总结，都成功才返回。

    返回 None 表示**这一段救不回来**，调用方必须整体放弃（不许用部分结果拼一份有缺口的摘要）。
    """
    text = "\n".join(_clip(str(m), char_cap) for m in messages)
    summary = await call(render(text))
    if summary and (not enforce_timeline or has_timeline(summary)):
        return summary

    if len(messages) <= MIN_CHUNK_SIZE:
        logger.error(
            "摘要分块：单条消息仍被拦截（%s 字符），该区间无解，放弃。"
            "（这一类是输入侧拦截，重试无效，只能靠更激进的中性化处理）",
            len(text),
        )
        return None

    mid = len(messages) // 2
    left = await _summarize_range(messages[:mid], call, render, chunk_size, char_cap, depth + 1, merge_mode, enforce_timeline)
    if left is None:
        return None
    right = await _summarize_range(messages[mid:], call, render, chunk_size, char_cap, depth + 1, merge_mode, enforce_timeline)
    if right is None:
        return None
    return merge_chunk_summaries([left, right], merge_mode)


async def chunked_summary(
    messages: Sequence[Any],
    call: SummaryCall,
    render: Callable[[str], str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    char_cap: int = DEFAULT_CHUNK_CHAR_CAP,
    merge_mode: str = MERGE_MODE_CONCAT,
    enforce_timeline: bool = False,
) -> Optional[str]:
    """整段阻塞时退化的分块摘要。全部块都成功才返回，否则 None。

    Args:
        messages: 已带历史时间戳前缀的消息文本序列。
        call: 单次摘要调用，返回空串/None 表示这次被拦。
        render: 把对话原文包成 user_prompt 的函数。
        merge_mode: `MERGE_MODE_SECTIONS` 会把各块的六字段按行分类合并
            （同号槽位只留一份字段，并丢掉字段外的入戏散文）。
            只对**同一个槽位**的输入用——跨槽位合并本来就是拼接，别用。
        enforce_timeline: 要求每块都必须带【时间线】。用于结构已知的
            六字段摘要路径；不满足就当作这一块没总结成功，交给二分劈半。
    """
    parts: List[Optional[str]] = []
    for piece in chunk_messages(messages, chunk_size):
        parts.append(
            await _summarize_range(
                piece, call, render, chunk_size, char_cap,
                merge_mode=merge_mode, enforce_timeline=enforce_timeline,
            )
        )

    if any(p is None for p in parts):
        logger.error("摘要分块：有区间无法总结，放弃整条摘要（不写有缺口的内容）")
        return None

    joined = merge_chunk_summaries([p for p in parts if p], merge_mode)
    if not joined.strip():
        return None
    return joined
