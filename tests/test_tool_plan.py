import asyncio
import json

from brain.tools import ToolManager
from adapters.base import BaseAdapter


class DummyAdapter(BaseAdapter):
    def run(self, message_handler):
        return None

    async def send_message(self, chat_id: str, text: str) -> str:
        return text

    async def start_typing(self, chat_id: str):
        return None

    async def stop_typing(self, chat_id: str):
        return None


def _build_manager() -> ToolManager:
    return ToolManager(DummyAdapter())


def test_execute_tool_plan_stop_on_error_and_report(tmp_path):
    manager = _build_manager()

    ok_file = tmp_path / "ok.txt"
    missing_file = tmp_path / "missing.txt"

    plan = [
        {"name": "write_file", "args": {"path": str(ok_file), "content": "hello"}},
        {"name": "read_file", "args": {"path": str(missing_file)}},
        {"name": "list_dir", "args": {"path": str(tmp_path)}},
    ]

    raw = asyncio.run(
        manager.execute_tool_plan(
            plan_json=json.dumps(plan, ensure_ascii=False),
            stop_on_error=True,
        )
    )
    result = json.loads(raw)

    assert result["summary"]["aborted"] is True
    assert result["summary"]["success"] == 1
    assert result["summary"]["failed"] == 1
    assert result["summary"]["executed_steps"] == 2
    assert result["steps"][0]["status"] == "success"
    assert result["steps"][1]["status"] == "failed"


def test_execute_tool_plan_dedup_successful(tmp_path):
    manager = _build_manager()
    file_path = tmp_path / "same.txt"

    plan = [
        {"name": "write_file", "args": {"path": str(file_path), "content": "A"}},
        {"name": "write_file", "args": {"path": str(file_path), "content": "A"}},
        {"name": "read_file", "args": {"path": str(file_path)}},
    ]

    raw = asyncio.run(
        manager.execute_tool_plan(
            plan_json=json.dumps(plan, ensure_ascii=False),
            stop_on_error=False,
            dedupe_successful=True,
        )
    )
    result = json.loads(raw)

    assert result["summary"]["success"] == 2
    assert result["summary"]["skipped"] == 1
    assert any(step["status"] == "skipped_duplicate_success" for step in result["steps"])
