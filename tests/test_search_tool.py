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


def test_workspace_prefix_path_resolution_for_file_tools(tmp_path):
    """验证 workspace/ 前缀会映射到真实 WORKSPACE_ROOT，而不是仓库内相对目录。"""
    import brain.tools as tools_module

    # 临时切换 WORKSPACE_ROOT 到 pytest 的临时目录
    old_root = tools_module.WORKSPACE_ROOT
    tools_module.WORKSPACE_ROOT = str(tmp_path)

    try:
        manager = _build_manager()

        # 1) write_file: workspace/ 前缀
        write_result = manager.write_file("workspace/data/hello.txt", "hello")
        expected_file = tmp_path / "data" / "hello.txt"

        assert expected_file.exists()
        assert expected_file.read_text(encoding="utf-8") == "hello"
        assert "Successfully wrote to" in write_result

        # 2) read_file: workspace/ 前缀
        read_result = manager.read_file("workspace/data/hello.txt")
        assert read_result == "hello"

        # 3) edit_file: workspace/ 前缀
        edit_result = manager.edit_file("workspace/data/hello.txt", "hello", "hello world")
        assert "Successfully edited" in edit_result
        assert expected_file.read_text(encoding="utf-8") == "hello world"
    finally:
        tools_module.WORKSPACE_ROOT = old_root


def test_relative_filename_resolution_uses_workspace_root(tmp_path):
    """验证裸文件名和 ./ 相对路径默认写入 WORKSPACE_ROOT。"""
    import brain.tools as tools_module

    old_root = tools_module.WORKSPACE_ROOT
    tools_module.WORKSPACE_ROOT = str(tmp_path)

    try:
        manager = _build_manager()

        # bare filename
        result1 = manager.write_file("note.txt", "nora")
        expected1 = tmp_path / "note.txt"
        assert expected1.exists()
        assert expected1.read_text(encoding="utf-8") == "nora"
        assert "Successfully wrote to" in result1

        # ./relative path
        result2 = manager.write_file("./logs/run.log", "ok")
        expected2 = tmp_path / "logs" / "run.log"
        assert expected2.exists()
        assert expected2.read_text(encoding="utf-8") == "ok"
        assert "Successfully wrote to" in result2
    finally:
        tools_module.WORKSPACE_ROOT = old_root
