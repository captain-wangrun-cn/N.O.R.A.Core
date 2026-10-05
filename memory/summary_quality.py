"""摘要生成的产物质量判据与「失败时不出货」约定。

背景（生产事故）：LLM provider 的失败约定是**返回一段错误文本而不是抛异常**
（历史设计：好让对话链路能把原因说给用户，见 `brain/interface.py` 的 `last_error` 注释）。
但所有把 `chat()` 结果当**产物**用的调用方——压缩写 `summaries`、归档覆盖旧行、段落摘要、
每日总结、上下文槽位压缩——当时都没检查这个信号，于是那段错误说明被当成摘要存进了库里。

后果分三档，取决于调用方写在哪：

- 一级压缩（`_compress_locked`）：脏串进摘要表，**同时**这 10 条消息被标 `is_archived=1`，
  原文从此不再进上下文 —— 永久丢失。
- 归档（`_archive_locked`）：滚动合并会 **DELETE** 被吸收的上一轮归档。脏串不但顶掉内容，
  还把已经压缩好的旧归档一起抹了。
- 段落摘要 / 每日总结 / 上下文槽位：脏串被当成 Nora 的记忆注入后续对话。

两件事故里挖出来的、必须一起处理的事实：

1. **只有 `check_finish_reason_and_log` 的返回带 `ERROR_RESULT_PREFIX`**。各 provider 自己
   的 `_fail()` 调用点（openai 的 `empty_choices` / `responses_api_exception` /
   `unexpected_response_type` / `exception`，gemini 的 `blocked_exception` / `exception`，
   anthropic 的 `exception`）返回的全是「抱歉，处理您的请求时遇到了问题：…」这类没有前缀的
   文本 —— 它们**是给用户看的聊天回复**，所以不能去改那些字符串来统一。
   判别只能靠 `client.last_error`，`is_error_result()` 只是拿不到 client 时的兜底。
2. **输入侧的拦截对重试完全无效**。`prompt_blocked` (HTTP 400) / `PROHIBITED_CONTENT`
   发生在请求进模型之前（`completion_tokens=0`），实测同一份输入 27 次调用 0 次成功；
   输出侧的 `content_filter`（模型生成到一半被掐）才是随机的，重试有意义。
   两者在运行时经 `check_finish_reason_and_log` 后都表现为 `last_error="blocked:<reason>"`，
   **分辨不出来**。所以重试策略只能是「原样重试几次捞输出侧的随机失败，再退到切小」。
"""

from typing import Any, Optional

from brain.interface import BaseLLM

# 重试几次之后再退到分块。输入侧拦截重试再多次也没用，但输出侧的随机失败值得捞；
# 实测过一次「第一轮 8 次全灭、第二轮第 1 次就过」，所以 3 次够了，再多纯浪费。
OUTPUT_SIDE_RETRIES = 3


def looks_like_error(text: Any) -> bool:
    """这段文本是不是 provider 的失败说明（而不是可用产物）。

    认的是 `last_error` 会置位的两类形态：`check_finish_reason_and_log` 带前缀的错误文本，
    以及带「抱歉/处理您的请求时遇到了问题」这类固定措辞的 provider 兜底回复。
    """
    value = str(text or "").strip()
    if not value:
        return False
    if BaseLLM.is_error_result(value):
        return True
    return any(marker in value[:80] for marker in _ERROR_MARKERS)


# provider 自己的兜底文案（没有 ERROR_RESULT_PREFIX，因为同一段话也要直接发给用户）。
_ERROR_MARKERS = (
    "抱歉，处理您的请求时遇到了问题",
    "抱歉，由于内容安全限制",
    "Sorry, I encountered an issue processing your request",
)


def summarize_error_reason(client: Optional[Any], text: Any) -> Optional[str]:
    """摘要调用失败时返回原因串，正常产物返回 None。

    `client.last_error` 是主判据；`looks_like_error` 覆盖拿不到 client 实例的场景
    （例如 provider 在 `chat()` 里重新取了一个 client）。
    """
    if client is not None:
        reason = getattr(client, "last_error", None)
        if reason:
            return str(reason)
    if looks_like_error(text):
        return "error_result_text"
    return None


def ensure_usable_summary(client: Optional[Any], text: Any, label: str) -> str:
    """产物不可用时 raise，可用时原样返回。

    raise 是有意的：三个写入点的 `except` 里都已经有 `conn.rollback()` +
    `_enqueue_retry()`，抛出等于免费接上重试队列。**在写入之前**抛，脏数据就永远不会落库。

    ⚠️ 重试队列对输入侧拦截是无效的（`retry_max_attempts=8` 会白跑 8 次）——那是分块
    （`memory/summary_chunking.py`）负责的那一半，不是这里的。
    """
    reason = summarize_error_reason(client, text)
    if reason:
        raise RuntimeError(f"{label}失败，拒绝写入: {reason}")
    value = str(text or "").strip()
    if not value:
        raise RuntimeError(f"{label}返回空内容，拒绝写入")
    return value
