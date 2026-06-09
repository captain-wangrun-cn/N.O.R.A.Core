from types import SimpleNamespace

from brain.tools import ToolManager


class _Adapter:
    def get_adapter_tools(self):
        return []


def test_tool_manager_security_config_requires_enabled_flag(monkeypatch):
    monkeypatch.setattr(
        "config.get_config",
        lambda: {
            "llm": {"models": {"security": "security-model"}},
            "security": {"enabled": False},
        },
    )
    monkeypatch.setattr("brain.tools.get_workspace_manager", lambda: SimpleNamespace())

    manager = ToolManager(_Adapter())

    assert manager._security_enabled is False


def test_tool_manager_security_config_enables_review_and_whitelist(monkeypatch):
    monkeypatch.setattr(
        "config.get_config",
        lambda: {
            "llm": {"models": {"security": "security-model"}},
            "security": {"enabled": True, "tool_whitelist": ["read_file"]},
        },
    )
    monkeypatch.setattr("brain.tools.get_workspace_manager", lambda: SimpleNamespace())

    manager = ToolManager(_Adapter())

    assert manager._security_enabled is True
    assert manager._security_whitelist == {"read_file"}
