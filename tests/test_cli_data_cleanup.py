import sqlite3
from pathlib import Path

import cli


def _create_sqlite(path: Path, table: str):
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute(f"INSERT INTO {table}(value) VALUES ('x')")
    conn.commit()
    conn.close()


def test_get_local_data_paths_uses_current_runtime_paths(tmp_path, monkeypatch):
    history_db = tmp_path / "message_history.db"
    mirror_db = tmp_path / "message_log.db"
    context_db = tmp_path / "context_compression.db"
    cost_db = tmp_path / "cost_tracker.db"

    dummy_history = type(
        "DummyHistory",
        (),
        {
            "message_log": type("DummyMessageLog", (), {"db_path": str(mirror_db)})(),
            "context_compressor": type("DummyContext", (), {"db_path": str(context_db)})(),
        },
    )()

    monkeypatch.setattr(cli, "_load_message_history", lambda: (dummy_history, history_db))

    class DummyCostTracker:
        def __init__(self):
            self.db_path = str(cost_db)

    monkeypatch.setattr(cli, "CostTracker", DummyCostTracker)

    paths = cli.get_local_data_paths()

    assert paths["chat_history"] == history_db
    assert paths["message_log"] == mirror_db
    assert paths["context_compression"] == context_db
    assert paths["cost_tracker"] == cost_db
