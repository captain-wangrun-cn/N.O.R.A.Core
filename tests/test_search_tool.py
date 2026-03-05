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


def test_search_finds_keyword_with_glob(tmp_path):
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.md"
    file_a.write_text("print('hello copilot search')\n", encoding="utf-8")
    file_b.write_text("hello from markdown\n", encoding="utf-8")

    manager = _build_manager()
    result = manager.search(
        query="copilot",
        path=str(tmp_path),
        include_pattern="**/*.py",
        max_results=10,
    )

    assert "a.py:1:" in result
    assert "b.md" not in result


def test_search_supports_regex(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("Ticket NORA-123 fixed\nTicket NORA-999 pending\n", encoding="utf-8")

    manager = _build_manager()
    result = manager.search(
        query=r"NORA-\d{3}",
        path=str(tmp_path),
        include_pattern="**/*.txt",
        is_regex=True,
        max_results=10,
    )

    assert "sample.txt:1:" in result
    assert "sample.txt:2:" in result
