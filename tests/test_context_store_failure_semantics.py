"""ContextCompressor 的失败语义与标签往返。

背景（生产事故）：压缩槽位在模型答不上来或输入被拦时，会把**截断到 200 字的原文**
当摘要写进 `context_segments`，再被 `get_context_messages` 注入到后续每一轮对话。
它读起来像记忆，实际把语义砍掉一半——和"报错文案当摘要"是同一类静默降级。

所以这里的约定是：总结不出来就**返回 None、保留旧槽位**，绝不写降级内容。
"""

import asyncio
import sqlite3

import pytest

from memory.context_store import (
    ContextCompressor,
    MessageLog,
    _SegmentText,
    _decorate,
    _unwrap_label,
)

SCOPE = "relationship:owner:default"
TIMELINE = "【时间线】\n- 10:00 主人问了一件事。\n- 10:01 我回答了。\n\n【状态】\n- 已接受：无\n"


class _BlockedSummarizer:
    """provider 的失败约定：返回错误文本 + 置 last_error（不抛异常）。"""

    def __init__(self):
        self.last_error = "empty_choices"

    async def chat(self, system_prompt, user_prompt, history, **kwargs):
        return "抱歉，处理您的请求时遇到了问题：empty_choices"


class _ProseSummarizer:
    """输出"入戏散文"——既不是报错、也不含六字段，长度还很长。"""

    def __init__(self):
        self.last_error = None
        self.calls = 0

    async def chat(self, system_prompt, user_prompt, history, **kwargs):
        self.calls += 1
        return "我凑过去，把下巴搁在你肩上，低声说：" + "这次真的别熬夜了。" * 40


class _GoodSummarizer:
    def __init__(self):
        self.last_error = None

    async def chat(self, system_prompt, user_prompt, history, **kwargs):
        return TIMELINE


def _make(tmp_path, summarizer):
    history_db = tmp_path / "history.db"
    log = MessageLog(db_path=str(tmp_path / "mirror.db"))
    cc = ContextCompressor(
        message_log=log,
        db_path=str(tmp_path / "context.db"),
        history_db_path=str(history_db),
        long_message_threshold=1200,
    )
    cc._summarizer = summarizer
    return cc, history_db


def _seed_messages(history_db, msgs):
    """建 messages / conversation_sessions 两张表并塞入若干条消息。"""
    conn = sqlite3.connect(str(history_db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT, chat_id TEXT, session_id INTEGER, role TEXT,
            content TEXT, timestamp REAL, is_archived INTEGER DEFAULT 0,
            memory_scope_id TEXT
        );
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT, chat_id TEXT, message_count INTEGER,
            started_at REAL, ended_at REAL, summary TEXT, memory_scope_id TEXT
        );
        """
    )
    for role, content, ts, scope in msgs:
        conn.execute(
            "INSERT INTO messages (platform, chat_id, session_id, role, content, timestamp, memory_scope_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("telegram", "chat", None, role, content, ts, scope),
        )
    conn.commit()
    conn.close()


def _slot_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "context.db"))
    rows = conn.execute(
        "SELECT slot, segment_type, content FROM context_segments ORDER BY slot"
    ).fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------
# 标签：库里只存正文，读的时候按 slot 现拼
# --------------------------------------------------------------------------

def test_label_never_persisted_and_roundtrips():
    """入库正文不含标签；读出来才带标签；旧库的标签能被剥掉。"""
    body = "【时间线】\n- 10:00 一段。"
    assert _decorate(3, "raw_recent_segment", body) == f"[最近段#3]\n{body}"
    # 旧库里存的是带标签的版本，再拼一次不能变成两层
    assert _decorate(3, "raw_recent_segment", _unwrap_label(f"[最近段#3]\n{body}")) == f"[最近段#3]\n{body}"
    assert _unwrap_label(f"[压缩段#4] {body}") == body
    assert _unwrap_label(f"[合并摘要段7-10] {body}") == body
    # 正文里出现的方括号不该被误伤
    assert _unwrap_label("[某张图] 细节说明") == "[某张图] 细节说明"


# --------------------------------------------------------------------------
# 失败语义
# --------------------------------------------------------------------------

def test_blocked_segment_is_skipped_not_degraded(tmp_path):
    """被拦时槽位**不写入**——尤其不能写 200 字截断原文。"""
    cc, history_db = _make(tmp_path, _BlockedSummarizer())
    # 12 条消息构成一个已关闭段落；内容够长以便展开
    _seed_messages(history_db, [("user", "内容" * 300, 1000.0 + i, SCOPE) for i in range(12)])
    conn = sqlite3.connect(str(history_db))
    conn.execute(
        "INSERT INTO conversation_sessions (platform, chat_id, message_count, started_at, ended_at, memory_scope_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("telegram", "chat", 12, 1000.0, 1500.0, SCOPE),
    )
    conn.execute("UPDATE messages SET session_id = 1")
    conn.commit()
    conn.close()

    asyncio.run(cc.refresh_context("telegram", "chat", memory_scope_id=SCOPE))

    rows = _slot_rows(tmp_path)
    assert rows == [], "被拦的槽位必须留空，绝不能写截断原文当摘要"


def test_existing_slot_survives_a_failed_refresh(tmp_path):
    """已有摘要的槽位遇到失败时，旧内容必须原样保留（整表覆盖不能删记忆）。"""
    cc, history_db = _make(tmp_path, _GoodSummarizer())
    # 超过 long_message_threshold(1200)，槽 1 才会走压缩路径而不是原文直存
    _seed_messages(history_db, [("user", "内容" * 700, 1000.0, SCOPE)])
    conn = sqlite3.connect(str(history_db))
    conn.execute(
        "INSERT INTO conversation_sessions (platform, chat_id, message_count, started_at, ended_at, memory_scope_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("telegram", "chat", 1, 1000.0, 1500.0, SCOPE),
    )
    conn.execute("UPDATE messages SET session_id = 1")
    conn.commit()
    conn.close()

    asyncio.run(cc.refresh_context("telegram", "chat", memory_scope_id=SCOPE))
    first = _slot_rows(tmp_path)
    assert first, "先跑一次，让槽位里有内容"

    # 内容变了（同段内追加一条新消息 → max(timestamp) 变 → 缓存失效），且这次模型被拦
    conn = sqlite3.connect(str(history_db))
    conn.execute(
        "INSERT INTO messages (platform, chat_id, session_id, role, content, timestamp, memory_scope_id)"
        " VALUES (?, ?, 1, ?, ?, ?, ?)",
        ("telegram", "chat", "user", "新内容", 2000.0, SCOPE),
    )
    conn.commit()
    conn.close()

    cc._summarizer = _BlockedSummarizer()
    asyncio.run(cc.refresh_context("telegram", "chat", memory_scope_id=SCOPE))

    after = _slot_rows(tmp_path)
    assert after, "失败时旧槽位必须保留，不能因为整表覆盖而被删掉"
    assert [r[0] for r in after] == [r[0] for r in first]
    assert after[0][2] == first[0][2], "失败的那一槽内容应原样保留"


def test_prose_output_is_rejected_and_degrades_to_chunks(tmp_path):
    """模型"入戏"写散文（长度够、非报错）也必须被结构判据拦住。"""
    prose = _ProseSummarizer()
    cc, _ = _make(tmp_path, prose)

    async def run():
        return await cc._summarize_single(
            _SegmentText(blocks=["user: 一段", "assistant: 一段"], message_count=2, latest_ts=1.0),
            "system",
            None,
            "session:1:2:1.000",
            min_chars=20,
        )

    result = asyncio.run(run())
    assert result is None, "散文没有【时间线】，不得当成摘要写库"
    assert prose.calls > 1, "结构不合规应触发分块降级（不止一次调用）"


def test_good_summary_passes_and_is_stored_without_label(tmp_path):
    cc, _ = _make(tmp_path, _GoodSummarizer())
    from memory.context_store import _SegmentText

    async def run():
        return await cc._summarize_single(
            _SegmentText(blocks=["user: 一段"], message_count=1, latest_ts=1.0),
            "system",
            None,
            "session:1:1:1.000",
            min_chars=20,
        )

    result = asyncio.run(run())
    assert result is not None and "【时间线】" in result
    assert not result.startswith("[压缩段"), "写库正文不该带标签"


# --------------------------------------------------------------------------
# 缓存键
# --------------------------------------------------------------------------

def test_source_key_uses_latest_message_timestamp(tmp_path):
    """source_key 必须带段内 max(timestamp)：内容变了键就变，没变就命中。"""
    cc, history_db = _make(tmp_path, _GoodSummarizer())
    _seed_messages(
        history_db,
        [("user", "a", 100.0, SCOPE), ("assistant", "b", 300.0, SCOPE)],
    )
    conn = sqlite3.connect(str(history_db))
    conn.execute(
        "INSERT INTO conversation_sessions (platform, chat_id, message_count, started_at, ended_at, memory_scope_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("telegram", "chat", 2, 10.0, 20.0, SCOPE),
    )
    conn.execute("UPDATE messages SET session_id = 1")
    conn.commit()
    conn.close()

    refs = cc._get_recent_segment_refs("telegram", "chat", memory_scope_id=SCOPE)
    closed = [r for r in refs if r["kind"] == "closed"]
    assert len(closed) == 1
    assert "300.000" in closed[0]["source_key"], "键里必须是段内 max(timestamp)，不是 ended_at(20.0)"
