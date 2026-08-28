"""RTC 通话总结回写测试（P2）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rtc.history_writer import format_transcript, summarize_call, write_call_to_history


class TestFormatTranscript:
    def test_basic(self):
        text = format_transcript([("user", "喂"), ("assistant", "我在")])
        assert "主人：喂" in text
        assert "Nora：我在" in text

    def test_empty(self):
        assert format_transcript([]) == ""


class TestSummarizeCall:
    @pytest.mark.anyio
    async def test_empty_transcript_returns_empty(self):
        assert await summarize_call([]) == ""

    @pytest.mark.anyio
    async def test_summary_from_fast_model(self):
        fake_fast = MagicMock()
        fake_fast.chat = AsyncMock(return_value="他们聊了天气，约好明天带伞。")
        with patch("brain.llm.get_llm_client", return_value=fake_fast):
            summary = await summarize_call([("user", "明天天气"), ("assistant", "有雨")])
        assert summary == "他们聊了天气，约好明天带伞。"
        # 调用参数检查：system 里带"通话记录整理器"
        _, kwargs = fake_fast.chat.call_args
        assert "通话记录整理器" in kwargs["system_prompt"]
        assert "明天天气" in kwargs["user_prompt"]

    @pytest.mark.anyio
    async def test_model_error_text_returns_empty(self):
        fake_fast = MagicMock()
        fake_fast.chat = AsyncMock(return_value="Error: 模型超时")
        with patch("brain.llm.get_llm_client", return_value=fake_fast):
            summary = await summarize_call([("user", "hi")])
        assert summary == ""

    @pytest.mark.anyio
    async def test_long_transcript_truncated(self):
        fake_fast = MagicMock()
        fake_fast.chat = AsyncMock(return_value="ok")
        long_turns = [("user", "啊" * 5000), ("assistant", "嗯" * 5000)]
        with patch("brain.llm.get_llm_client", return_value=fake_fast):
            await summarize_call(long_turns)
        _, kwargs = fake_fast.chat.call_args
        assert len(kwargs["user_prompt"]) < 13000  # 截断生效

    @pytest.mark.anyio
    async def test_llm_exception_returns_empty(self):
        fake_fast = MagicMock()
        fake_fast.chat = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("brain.llm.get_llm_client", return_value=fake_fast):
            assert await summarize_call([("user", "hi")]) == ""


class TestWriteCallToHistory:
    @pytest.mark.anyio
    async def test_no_transcript_skips(self):
        assert await write_call_to_history([], chat_id="123") is False

    @pytest.mark.anyio
    async def test_writes_summary_and_raw(self, tmp_path):
        fake_fast = MagicMock()
        fake_fast.chat = AsyncMock(return_value="通话摘要内容。")
        fake_history = MagicMock()

        # 镜像库在 {data_dir}/memory/message_log.db（走 ContextStore 官方表结构）
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        mirror_db = memory_dir / "message_log.db"
        import sqlite3

        conn = sqlite3.connect(str(mirror_db))
        conn.execute(
            "CREATE TABLE raw_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "message_id INTEGER, platform TEXT, chat_id TEXT, role TEXT,"
            "content_with_timestamp TEXT, raw_content TEXT, timestamp REAL,"
            "metadata TEXT, created_at TEXT, memory_scope_id TEXT, place_scope_id TEXT)"
        )
        conn.commit()
        conn.close()

        fake_ws = MagicMock()
        fake_ws.data_dir = str(tmp_path)

        with patch("brain.llm.get_llm_client", return_value=fake_fast), \
             patch("memory.message_history.MessageHistory", return_value=fake_history), \
             patch("workspace_config.get_workspace_manager", return_value=fake_ws):
            ok = await write_call_to_history(
                [("user", "喂"), ("assistant", "在呢")],
                chat_id="6112866979",
                duration_seconds=120,
                bytes_up=1000, bytes_down=2000,
            )

        assert ok is True
        # 摘要入 message_history（带 [RTC通话] 前缀）
        args = fake_history.add_message.call_args[0]
        assert args[0] == "telegram" and args[1] == "6112866979"
        assert "[RTC通话" in args[3]
        assert "通话摘要内容。" in args[3]
        # 原始转写入镜像库（ContextStore 列名）
        conn = sqlite3.connect(str(mirror_db))
        rows = conn.execute(
            "SELECT content_with_timestamp, raw_content FROM raw_messages"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert "[RTC通话原始转写" in rows[0][0]
        assert "主人：喂" in rows[0][1]

    @pytest.mark.anyio
    async def test_summary_failure_still_writes_raw(self, tmp_path):
        """fast 总结失败：摘要跳过，但原始转写仍落镜像（不丢底账）。"""
        fake_fast = MagicMock()
        fake_fast.chat = AsyncMock(side_effect=RuntimeError("boom"))

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        mirror_db = memory_dir / "message_log.db"
        import sqlite3

        conn = sqlite3.connect(str(mirror_db))
        conn.execute(
            "CREATE TABLE raw_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "message_id INTEGER, platform TEXT, chat_id TEXT, role TEXT,"
            "content_with_timestamp TEXT, raw_content TEXT, timestamp REAL,"
            "metadata TEXT, created_at TEXT, memory_scope_id TEXT, place_scope_id TEXT)"
        )
        conn.commit()
        conn.close()

        fake_ws = MagicMock()
        fake_ws.data_dir = str(tmp_path)

        with patch("brain.llm.get_llm_client", return_value=fake_fast), \
             patch("workspace_config.get_workspace_manager", return_value=fake_ws):
            ok = await write_call_to_history(
                [("user", "喂")], chat_id="123", duration_seconds=60,
            )

        assert ok is False  # 没产出摘要
        conn = sqlite3.connect(str(mirror_db))
        rows = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()
        conn.close()
        assert rows[0] == 1  # 原始转写仍写入了
