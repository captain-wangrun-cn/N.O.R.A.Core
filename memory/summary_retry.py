"""摘要调用的失败约定 —— 重试、降级、以及「哪一半该由谁负责」。

配套 `memory/summary_quality.py`（判据）与 `memory/summary_chunking.py`（分块）。

实测结论（session 722 生产重放）——**内容被拦截时，重试往往毫无用处**：

| 拦截类型 | 层 | 同输入重试 | 证据 |
|---|---|---|---|
| `content_filter` | 输出侧（生成到一半被掐） | ✅ 有效 | 某轮 8 次全灭，下一轮第 1 次就过 |
| `PROHIBITED_CONTENT` / `prompt_blocked` (400) | 输入侧（请求没进模型） | ❌ 无效 | **27 次调用 0 次成功**，`completion_tokens=0` |
| `empty_choices` | 输入侧 | ❌ 无效 | 网关返回 200 但 choices 为空 |

判据是**聚合密度**而不是某条消息：同一份 9 条消息原样送 → 0/4 通过；去掉"触发条" → 仍 5/5 全灭；
每条截到 300 字 → 4/4 通过。所以单条消息看起来都无害，拼起来才构成触发模式。

**两个拦截在运行时分辨不出来**（都经 `check_finish_reason_and_log` →
`last_error = "blocked:<reason>"`），所以只能这么分工：

- **精确重试**（本模块）：捞输出侧的随机失败。真被输入侧拦住就是原子空转，
  这是可接受的代价 —— 3 次调用换一次"本来会永久丢失的记忆"。
- **分块**（`summary_chunking.py`）：输入侧拦截的正解。实测把 7 条硬拦截会话全部救回、零缺口。

⚠️ 别再调用方各写一遍重试循环：`ContextCompressor._call_summary` 曾自带一套 3 次指数退避，
再叠上本模块就是 3×3=9 次调用，对生产上已确认是确定性的输入侧拦截纯属浪费。
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.8


async def call_with_retry(
    call: Callable[[], Awaitable[Tuple[Optional[Any], str]]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    label: str = "摘要",
) -> Tuple[Optional[Any], Optional[str]]:
    """在给定的尝试次数内反复调用，直到拿到可用产物。

    Args:
        call: 协程工厂，返回 `(client, text)`。`client` 用来读 `last_error`
            （主判据，理由见 `memory/summary_quality.py`），拿不到时传 None。
        attempts: 总调用次数，含首次。1 表示不重试。
        base_delay: 首次重试前的等待秒数，之后按 2 的幂递增。
        label: 日志前缀。

    Returns:
        `(client, text)` —— 成功时 `text` 是产物；
        全部失败时 `client` 是最后一次的 client、`text` 是最后一次的**错误文本**
        （调用方必须再走一次 `summary_quality.ensure_usable_summary` 才会 raise，
        这里不直接抛是为了让调用方能先记原因、再决定怎么降级）。
    """
    from memory.summary_quality import summarize_error_reason

    total = max(1, int(attempts))
    client: Optional[Any] = None
    text = ""
    reason: Optional[str] = None

    for attempt in range(1, total + 1):
        client, text = await call()
        reason = summarize_error_reason(client, text)
        if reason is None:
            return client, text

        if attempt >= total:
            break

        # 输入侧拦截重试必然失败，但运行时分辨不出，只能照试。
        # 日志按 warning 记，便于事后统计"哪些调用其实一直是白跑"。
        delay = base_delay * (2 ** (attempt - 1))
        logger.warning(
            "%s调用失败（第 %s/%s 次，原因=%s），%.1fs 后重试",
            label, attempt, total, reason, delay,
        )
        await asyncio.sleep(delay)

    logger.error("%s调用失败（已重试 %s 次，原因=%s）", label, total, reason)
    return client, text
