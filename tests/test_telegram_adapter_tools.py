import asyncio
import json
from types import SimpleNamespace

from adapters.base import AdapterToolSpec, BaseAdapter
from adapters.telegram.tools import TelegramToolsMixin
from brain.tools import ToolManager
from core.back_brain import BackBrainMixin


class FakeBot:
    def __init__(self):
        self.calls = []

    async def get_chat_member_count(self, chat_id):
        self.calls.append(("get_chat_member_count", chat_id))
        return 42

    async def get_chat_administrators(self, chat_id):
        self.calls.append(("get_chat_administrators", chat_id))
        return [{"user": {"id": 1, "first_name": "Owner"}, "status": "creator"}]

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(("get_chat_member", chat_id, user_id))
        return {"user": {"id": user_id}, "status": "member"}

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None):
        self.calls.append(("restrict_chat_member", chat_id, user_id, permissions, until_date))
        return True

    async def ban_chat_member(self, chat_id, user_id, until_date=None, revoke_messages=None):
        self.calls.append(("ban_chat_member", chat_id, user_id, until_date, revoke_messages))
        return True

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
        self.calls.append(("unban_chat_member", chat_id, user_id, only_if_banned))
        return True

    async def set_chat_administrator_custom_title(self, chat_id, user_id, custom_title):
        self.calls.append(("set_chat_administrator_custom_title", chat_id, user_id, custom_title))
        return True

    async def do_api_request(self, endpoint, api_kwargs=None):
        self.calls.append(("do_api_request", endpoint, api_kwargs))
        return True


class ToolOnlyTelegram(TelegramToolsMixin):
    def __init__(self):
        self.current_chat_id = "-100"
        self.application = SimpleNamespace(bot=FakeBot())


def _loads(result):
    return json.loads(result)


def test_telegram_group_tools_are_exposed():
    adapter = ToolOnlyTelegram()

    tools = adapter.get_adapter_tools()

    names = {tool.name for tool in tools}
    assert "telegram_get_chat_member_count" in names
    assert "telegram_ban_chat_member" in names
    assert all(isinstance(tool, AdapterToolSpec) for tool in tools)
    assert next(tool for tool in tools if tool.name == "telegram_ban_chat_member").risk == "high"


def test_telegram_get_member_count_uses_current_chat():
    adapter = ToolOnlyTelegram()

    result = _loads(asyncio.run(adapter.telegram_get_chat_member_count()))

    assert result["ok"] is True
    assert result["chat_id"] == "-100"
    assert result["result"] == 42
    assert adapter.application.bot.calls == [("get_chat_member_count", "-100")]


def test_telegram_get_chat_member_uses_target_user_id():
    adapter = ToolOnlyTelegram()

    result = _loads(asyncio.run(adapter.telegram_get_chat_member("123", chat_id="-200")))

    assert result["ok"] is True
    assert result["target_user_id"] == "123"
    assert adapter.application.bot.calls == [("get_chat_member", "-200", 123)]


def test_telegram_mute_and_unmute_call_restrict_member():
    adapter = ToolOnlyTelegram()

    mute = _loads(asyncio.run(adapter.telegram_mute_chat_member("123", until_timestamp=999, reason="spam")))
    unmute = _loads(asyncio.run(adapter.telegram_unmute_chat_member("123")))

    assert mute["ok"] is True
    assert mute["reason"] == "spam"
    assert unmute["ok"] is True
    assert adapter.application.bot.calls[0][0] == "restrict_chat_member"
    assert adapter.application.bot.calls[0][4] == 999
    assert adapter.application.bot.calls[1][0] == "restrict_chat_member"


def test_telegram_ban_and_unban_call_bot_methods():
    adapter = ToolOnlyTelegram()

    ban = _loads(asyncio.run(adapter.telegram_ban_chat_member("123", revoke_messages=True)))
    unban = _loads(asyncio.run(adapter.telegram_unban_chat_member("123", only_if_banned=True)))

    assert ban["ok"] is True
    assert unban["ok"] is True
    assert adapter.application.bot.calls == [
        ("ban_chat_member", "-100", 123, None, True),
        ("unban_chat_member", "-100", 123, True),
    ]


def test_telegram_set_titles_and_tags():
    adapter = ToolOnlyTelegram()

    title = _loads(asyncio.run(adapter.telegram_set_chat_administrator_custom_title("123", "helper")))
    tag = _loads(asyncio.run(adapter.telegram_set_chat_member_tag("123", "helper")))

    assert title["ok"] is True
    assert tag["ok"] is True
    assert adapter.application.bot.calls == [
        ("set_chat_administrator_custom_title", "-100", 123, "helper"),
        (
            "do_api_request",
            "setChatMemberTag",
            {"chat_id": "-100", "user_id": 123, "tag": "helper"},
        ),
    ]


def test_telegram_tool_error_returns_json():
    adapter = ToolOnlyTelegram()

    result = _loads(asyncio.run(adapter.telegram_get_chat_member("not-a-number")))

    assert result["ok"] is False
    assert "user_id must be" in result["error"]


class DummyAdapterWithTool(BaseAdapter):
    @property
    def platform_name(self) -> str:
        return "dummy"

    def get_adapter_tools(self):
        async def dummy_adapter_tool(value: str = ""):
            """Dummy adapter tool.

            :param value: Echo value.
            """
            return f"adapter:{value}"

        return [
            AdapterToolSpec(
                name="dummy_adapter_tool",
                callable=dummy_adapter_tool,
                intro="Adapter echo tool.",
                platform="dummy",
            )
        ]

    def run(self, message_handler):
        self._message_handler = message_handler

    async def send_message(self, chat_id: str, text: str, **kwargs) -> list[str]:
        return []

    async def start_typing(self, chat_id: str):
        return None

    async def stop_typing(self, chat_id: str):
        return None


def test_tool_manager_registers_adapter_tools():
    manager = ToolManager(DummyAdapterWithTool())

    schemas = manager.get_tool_schemas()
    intros = manager.get_tool_intros()
    result = asyncio.run(manager.execute("dummy_adapter_tool", {"value": "ok"}))

    assert any(schema["name"] == "dummy_adapter_tool" for schema in schemas)
    assert intros["dummy_adapter_tool"] == "Adapter echo tool."
    assert result == "adapter:ok"


class ContextInjectionProbe(BackBrainMixin):
    def __init__(self):
        self.tool_manager = SimpleNamespace(
            _adapter_tool_specs={"telegram_ban_chat_member": object()}
        )


def test_current_tool_context_injects_telegram_chat_and_reply_user():
    probe = ContextInjectionProbe()

    args = probe._inject_current_tool_context(
        "telegram_ban_chat_member",
        {},
        runtime_key="telegram:-100",
        platform="telegram",
        platform_chat_id="-100",
        storage_id="-100",
        context={"reply_to_user_id": "321"},
    )

    assert args["chat_id"] == "-100"
    assert args["user_id"] == "321"
