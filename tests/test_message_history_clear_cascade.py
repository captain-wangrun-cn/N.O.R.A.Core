import sqlite3

from memory.message_history import MessageHistory


def _count(db_path, table):
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    value = cursor.fetchone()[0]
    conn.close()
    return value


def test_clear_chat_history_clears_mirror_and_context_segments(tmp_path):
    history_db = tmp_path / "history.db"
    mirror_db = tmp_path / "mirror.db"
    context_db = tmp_path / "context.db"

    history = MessageHistory(
        db_path=str(history_db),
        mirror_db_path=str(mirror_db),
        context_db_path=str(context_db),
    )
    history.add_message("telegram", "chat-a", "user", "hello", "user-a")
    history.message_log.add_message(
        message_id=999,
        platform="telegram",
        chat_id="chat-a",
        role="assistant",
        content_with_timestamp="[now] hi",
        raw_content="hi",
        timestamp=1.0,
    )
    history.context_compressor._persist_segments(
        "telegram",
        "chat-a",
        [
            {
                "slot": 1,
                "segment_type": "raw_recent_segment",
                "role": "system",
                "content": "segment",
                "source_keys": ["1"],
                "source_timestamp": 1.0,
            }
        ],
    )

    assert _count(history_db, "messages") > 0
    assert _count(mirror_db, "raw_messages") > 0
    assert _count(context_db, "context_segments") > 0

    history.clear_chat_history("telegram", "chat-a", keep_pinned=False)

    assert _count(history_db, "messages") == 0
    assert _count(mirror_db, "raw_messages") == 0
    assert _count(context_db, "context_segments") == 0
