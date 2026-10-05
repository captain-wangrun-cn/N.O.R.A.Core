"""摘要产物质量判据 + 压缩写入守卫的回归测试。

对应生产事故：provider 失败时返回的是**一段错误文本**（"抱歉，处理您的请求时遇到了问题：…"），
压缩链路当成摘要写进了库，同时把源消息标成 `is_archived=1` —— 原文永久不再进上下文。

这些用例锁住三件事：
1. 各类失败文本都能被识别（含不带 `Error: ` 前缀的 provider 兜底文案）；
2. 被拦时**不写摘要、不标记已归档**，且进重试队列；
3. 正常产物照旧写库，不被守卫误伤。
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.interface import BaseLLM
from memory.message_history import MessageHistory
from memory.summary_quality import (
    ensure_usable_summary,
    looks_like_error,
    summarize_error_reason,
)
from memory.summary_retry import call_with_retry

# provider 自己的兜底文案，**不带** ERROR_RESULT_PREFIX ——
# 同一段话还要直接发给用户，所以不能去改那些字符串。
PROVIDER_FALLBACK = "抱歉，处理您的请求时遇到了问题：400 prompt_blocked"
PREFIXED_ERROR = (
    "Error: 模型因内容安全策略拒绝生成（finish_reason=PROHIBITED_CONTENT）。"
)


class _StubClient:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    @property
    def last_error(self):
        return getattr(self, "_last_error", None)

    @last_error.setter
    def last_error(self, value):
        self._last_error = value

    async def chat(self, system_prompt, user_prompt, history, **kwargs):
        self.calls += 1
        out = self._outputs[min(self.calls - 1, len(self._outputs) - 1)]
        # 模拟 provider 的失败约定：返回错误**文本**的同时置 last_error（不抛异常）。
        if out is None:
            self.last_error = "empty_choices"
            return PROVIDER_FALLBACK
        if out.startswith("Error: "):
            self.last_error = "blocked:PROHIBITED_CONTENT"
            return out
        if out == PROVIDER_FALLBACK:
            self.last_error = "blocked:PROHIBITED_CONTENT"
            return out
        if out == "":
            self.last_error = "empty_content"
            return out
        self.last_error = None
        return out


def _make_history(tmp_path, outputs, monkeypatch):
    hist = MessageHistory(db_path=str(tmp_path / "mh.db"))
    stub = _StubClient(outputs)
    hist._summarizer = stub
    # 不真的起后台 worker；只验证写入行为
    monkeypatch.setattr(hist, "start_retry_worker", lambda: None)
    return hist, stub


def _seed(hist, n=12):
    """灌 n 条未归档消息，够 compress_ratio 触发一次压缩。"""
    conn = sqlite3.connect(str(hist.db_path))
    cur = conn.cursor()
    for i in range(n):
        cur.execute(
            """INSERT INTO messages (platform, chat_id, user_id, role, content, timestamp,
                                    metadata, is_archived, is_pinned, session_id,
                                    memory_scope_id, place_scope_id, actor_display_name)
               VALUES ('telegram', 'c1', 'u1', 'user', ?, ?, NULL, 0, 0, 1,
                       'relationship:owner:default', 'telegram:c1', '主人')""",
            (f"第{i}条内容", 1000.0 + i),
        )
    conn.commit()
    conn.close()


def _counts(hist):
    conn = sqlite3.connect(str(hist.db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM summaries WHERE level = 1")
    summaries = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages WHERE is_archived = 1")
    archived = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM compression_retry_queue WHERE status = 'pending'")
    pending = cur.fetchone()[0]
    conn.close()
    return summaries, archived, pending


# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------

def test_prefixed_error_is_detected():
    assert looks_like_error(PREFIXED_ERROR) is True
    assert BaseLLM.is_error_result(PREFIXED_ERROR) is True


def test_provider_fallback_without_prefix_is_detected():
    """provider 自己的 _fail() 文案没有 Error: 前缀，is_error_result 认不出，
    只能靠措辞匹配 —— 这是本模块存在的理由。"""
    assert BaseLLM.is_error_result(PROVIDER_FALLBACK) is False
    assert looks_like_error(PROVIDER_FALLBACK) is True


def test_normal_summary_is_not_flagged():
    text = "【时间线】\n- 12:00 主人 说了句晚安\n【状态】\n- 待定：明天的安排"
    assert looks_like_error(text) is False
    assert summarize_error_reason(None, text) is None


def test_last_error_wins_over_text():
    """主判据是 client.last_error，不是文本。"""
    client = _StubClient(["看起来正常的文本"])
    client.last_error = "blocked:PROHIBITED_CONTENT"
    assert summarize_error_reason(client, "看起来正常的文本") == "blocked:PROHIBITED_CONTENT"


def test_ensure_usable_summary_raises_on_failure():
    with pytest.raises(RuntimeError):
        ensure_usable_summary(None, PROVIDER_FALLBACK, "压缩摘要")


def test_ensure_usable_summary_raises_on_empty():
    with pytest.raises(RuntimeError):
        ensure_usable_summary(None, "   ", "压缩摘要")


# --------------------------------------------------------------------------
# 重试
# --------------------------------------------------------------------------

def test_retry_recovers_from_stochastic_failure():
    """输出侧 content_filter 是随机的 —— 重试有意义。"""
    client = _StubClient([PROVIDER_FALLBACK, PROVIDER_FALLBACK, "恢复后的摘要"])

    async def _call():
        return client, await client.chat("s", "u", [])

    got_client, text = asyncio.run(call_with_retry(_call, attempts=3, base_delay=0))
    assert text == "恢复后的摘要"
    assert client.calls == 3
    assert got_client is client


def test_retry_exhausts_and_returns_last_error_text():
    # None = 网关返回 200 但 choices 为空：返回兜底文案 + last_error=empty_choices
    client = _StubClient([None])

    async def _call():
        return client, await client.chat("s", "u", [])

    _, text = asyncio.run(call_with_retry(_call, attempts=2, base_delay=0))
    assert text == PROVIDER_FALLBACK
    assert client.calls == 2, "重试次数应等于 attempts"
    assert summarize_error_reason(client, text) == "empty_choices"


# --------------------------------------------------------------------------
# 写入守卫（真库）
# --------------------------------------------------------------------------

def test_compress_writes_nothing_when_blocked(tmp_path, monkeypatch):
    """被拦时：不写摘要、不标已归档、进重试队列。"""
    hist, stub = _make_history(tmp_path, [PROVIDER_FALLBACK], monkeypatch)
    _seed(hist)

    asyncio.run(hist._compress_locked("telegram", "c1"))

    summaries, archived, pending = _counts(hist)
    assert summaries == 0, "被拦时不该写入任何一级摘要"
    assert archived == 0, "被拦时绝不能标记 is_archived —— 那等于永久丢原文"
    assert pending >= 1, "失败应进重试队列"
    assert stub.calls >= 1


def test_compress_writes_normally_when_healthy(tmp_path, monkeypatch):
    """正常产物照旧写库，守卫不误伤。"""
    good = "【时间线】\n- 12:00 主人 说晚安\n【状态】\n- 待定：明天的安排"
    hist, _ = _make_history(tmp_path, [good], monkeypatch)
    _seed(hist)

    asyncio.run(hist._compress_locked("telegram", "c1"))

    summaries, archived, pending = _counts(hist)
    assert summaries == 1
    assert archived == hist.compress_ratio
    conn = sqlite3.connect(str(hist.db_path))
    text = conn.execute("SELECT summary_text FROM summaries WHERE level = 1").fetchone()[0]
    conn.close()
    assert text.startswith("【时间线】")
    assert pending == 0
