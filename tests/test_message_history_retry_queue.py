'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-27 21:03:02
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import asyncio
from pathlib import Path

import pytest

from memory.message_history import MessageHistory


class _FailingSummarizer:
    async def chat(self, system_prompt, user_prompt, history):
        raise RuntimeError("summary boom")


class _OkSummarizer:
    def __init__(self, text: str = "summary ok"):
        self.text = text

    async def chat(self, system_prompt, user_prompt, history):
        return self.text


@pytest.mark.asyncio
async def test_compression_failure_enters_retry_queue_and_success_clears_it(tmp_path: Path):
    db_path = tmp_path / "history.db"
    mirror_db_path = tmp_path / "mirror.db"
    context_db_path = tmp_path / "context.db"

    history = MessageHistory(
        db_path=str(db_path),
        mirror_db_path=str(mirror_db_path),
        context_db_path=str(context_db_path),
        compress_window=3,
        compress_ratio=2,
        retry_base_delay_seconds=0.01,
        retry_scan_interval_seconds=3600,
    )
    history._summarizer = _FailingSummarizer()  # type: ignore[assignment]

    for i in range(2):
        history.add_message("telegram", "chat-a", "user", f"msg-{i}")

    await history._perform_compression("telegram", "chat-a")

    queue = history.get_retry_queue_status()
    assert len(queue) == 1
    assert queue[0]["task_type"] == "compress"
    assert queue[0]["status"] == "pending"

    history._summarizer = _OkSummarizer("compressed text")  # type: ignore[assignment]
    await history.process_pending_retries()

    queue_after = history.get_retry_queue_status()
    assert queue_after == []

    stats = history.get_statistics("telegram", "chat-a")
    assert stats["level1_summaries"] == 1
    assert stats["archived_messages"] == 2

    await history.stop_retry_worker()


@pytest.mark.asyncio
async def test_context_refresh_failure_enters_retry_queue_and_success_clears_it(tmp_path: Path):
    db_path = tmp_path / "history.db"
    mirror_db_path = tmp_path / "mirror.db"
    context_db_path = tmp_path / "context.db"

    history = MessageHistory(
        db_path=str(db_path),
        mirror_db_path=str(mirror_db_path),
        context_db_path=str(context_db_path),
        retry_base_delay_seconds=0.01,
        retry_scan_interval_seconds=3600,
    )

    history.add_message("telegram", "chat-b", "user", "hello")
    history.close_session("telegram", "chat-b")

    original = history.context_compressor._summarize_single

    async def _boom(*args, **kwargs):
        raise RuntimeError("context refresh boom")

    history.context_compressor._summarize_single = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await history.context_compressor.refresh_context("telegram", "chat-b")

    queue = history.get_retry_queue_status()
    assert len(queue) == 1
    assert queue[0]["task_type"] == "context_refresh"

    history.context_compressor._summarize_single = original  # type: ignore[method-assign]
    history.context_compressor._summarizer = _OkSummarizer("context summary ok")  # type: ignore[assignment]
    await history.process_pending_retries()

    queue_after = history.get_retry_queue_status()
    assert queue_after == []

    segments = history.context_compressor.get_context_messages("telegram", "chat-b")
    assert segments

    await history.stop_retry_worker()
