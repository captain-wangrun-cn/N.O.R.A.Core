import asyncio
import json
from types import SimpleNamespace

from adapters.base import AdapterToolSpec
from adapters.onebotv11.main import MENTION_ONLY_TEXT, OneBotV11Adapter
from adapters.onebotv11.message import (
    is_at_self,
    onebot_message_segments,
    plain_text_from_segments,
    strip_at_self,
)
from adapters.onebotv11.sender import OneBotSenderMixin
from adapters.onebotv11.tools import OneBotV11ToolsMixin
from core.back_brain import BackBrainMixin


class ToolOnlyOneBot(OneBotV11ToolsMixin):
    def __init__(self, enable_napcat_api=False):
        self.current_chat_id = "10001"
        self._chat_types = {"10001": "group"}
        self.enable_napcat_api = enable_napcat_api
        self.calls = []

    async def call_api(self, action, params=None):
        self.calls.append((action, params or {}))
        return {"status": "ok", "retcode": 0, "data": {"action": action, "params": params or {}}}


def _loads(result):
    return json.loads(result)


def test_onebot_message_helpers_parse_cq_and_at_self():
    segments = onebot_message_segments("[CQ:at,qq=123] 你好[CQ:image,file=a.jpg,url=http://x/a.jpg]")

    assert is_at_self(segments, "123") is True
    stripped = strip_at_self(segments, "123")
    assert is_at_self(stripped, "123") is False
    assert "你好" in plain_text_from_segments(stripped)
    assert any(seg["type"] == "image" for seg in segments)


def test_onebot_standard_tools_are_exposed_without_napcat():
    adapter = ToolOnlyOneBot(enable_napcat_api=False)

    tools = adapter.get_adapter_tools()
    names = {tool.name for tool in tools}

    assert "onebotv11_get_group_member_list" in names
    assert "onebotv11_set_group_ban" in names
    assert "onebotv11_napcat_send_poke" not in names
    assert all(isinstance(tool, AdapterToolSpec) for tool in tools)
    assert next(tool for tool in tools if tool.name == "onebotv11_set_group_ban").risk == "high"


def test_onebot_napcat_tools_are_optional():
    adapter = ToolOnlyOneBot(enable_napcat_api=True)

    names = {tool.name for tool in adapter.get_adapter_tools()}

    assert "onebotv11_napcat_send_poke" in names
    assert "onebotv11_napcat_get_ai_characters" in names


def test_onebot_group_tools_use_current_group_and_reply_user_shape():
    adapter = ToolOnlyOneBot()

    result = _loads(asyncio.run(adapter.onebotv11_get_group_member_info("20002")))
    ban = _loads(asyncio.run(adapter.onebotv11_set_group_ban("20002", duration=60, reason="spam")))

    assert result["ok"] is True
    assert ban["ok"] is True
    assert ban["reason"] == "spam"
    assert adapter.calls == [
        (
            "get_group_member_info",
            {"group_id": 10001, "user_id": 20002, "no_cache": False},
        ),
        (
            "set_group_ban",
            {"group_id": 10001, "user_id": 20002, "duration": 60},
        ),
    ]


def test_onebot_tool_error_returns_json():
    adapter = ToolOnlyOneBot()

    result = _loads(asyncio.run(adapter.onebotv11_get_group_member_info("not-a-number")))

    assert result["ok"] is False
    assert "user_id must be numeric" in result["error"]


def test_onebot_napcat_send_poke_prefers_current_group():
    adapter = ToolOnlyOneBot(enable_napcat_api=True)

    result = _loads(asyncio.run(adapter.onebotv11_napcat_send_poke("20002")))

    assert result["ok"] is True
    assert adapter.calls == [("send_poke", {"user_id": 20002, "group_id": 10001})]


class SenderOnlyOneBot(OneBotSenderMixin):
    def __init__(self):
        self.workspace_root = "D:/workspace"
        self.downloads_dir = "D:/workspace/downloads"
        self.data_dir = "D:/workspace/data"
        self._chat_types = {"10001": "group", "20002": "private"}
        self._last_incoming_contexts = {
            "10001": {
                "platform_message_id": "555",
                "user_id": "20002",
                "reply_to_user_id": "30003",
            }
        }
        self.calls = []

    async def call_api(self, action, params=None):
        self.calls.append((action, params or {}))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 42}}


def test_onebot_sender_builds_group_target_and_text_segments():
    adapter = SenderOnlyOneBot()

    ids = asyncio.run(adapter.send_message("10001", "hello", parse_media=False))

    assert ids == ["42"]
    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
        )
    ]


def test_onebot_sender_parses_split_markers_before_sending():
    adapter = SenderOnlyOneBot()

    ids = asyncio.run(adapter.send_message("10001", "hello[SPLIT:0]world", parse_media=False))

    assert ids == ["42", "42"]
    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
        ),
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [{"type": "text", "data": {"text": "world"}}],
            },
        ),
    ]


def test_onebot_sender_parses_reply_and_at_segments():
    adapter = SenderOnlyOneBot()

    ids = asyncio.run(adapter.send_message("10001", "[reply][at:current] 收到啦"))

    assert ids == ["42"]
    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [
                    {"type": "reply", "data": {"id": "555"}},
                    {"type": "at", "data": {"qq": "20002"}},
                    {"type": "text", "data": {"text": " 收到啦"}},
                ],
            },
        )
    ]


def test_onebot_sender_parses_explicit_reply_and_reply_at_target():
    adapter = SenderOnlyOneBot()

    asyncio.run(adapter.send_message("10001", "[reply:666][at:reply] 我看到了"))

    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [
                    {"type": "reply", "data": {"id": "666"}},
                    {"type": "at", "data": {"qq": "30003"}},
                    {"type": "text", "data": {"text": " 我看到了"}},
                ],
            },
        )
    ]


class ContextInjectionProbe(BackBrainMixin):
    def __init__(self):
        async def onebot_tool(user_id: str, group_id: str = "", chat_id: str = ""):
            return "ok"

        self.tool_manager = SimpleNamespace(
            _adapter_tool_specs={
                "onebotv11_set_group_ban": SimpleNamespace(
                    platform="onebotv11",
                    callable=onebot_tool,
                )
            }
        )


def test_current_tool_context_injects_onebot_group_and_reply_user():
    probe = ContextInjectionProbe()

    args = probe._inject_current_tool_context(
        "onebotv11_set_group_ban",
        {},
        runtime_key="onebotv11:10001",
        platform="onebotv11",
        platform_chat_id="10001",
        storage_id="10001",
        context={"chat_type": "group", "reply_to_user_id": "20002"},
    )

    assert args["chat_id"] == "10001"
    assert args["group_id"] == "10001"
    assert args["user_id"] == "20002"


class InitOnlyOneBot(OneBotV11Adapter):
    def __init__(self):
        pass


def test_onebot_metadata_loads_without_connection():
    adapter = InitOnlyOneBot()

    metadata = adapter.load_metadata()

    assert metadata.adapter_id == "onebotv11"
    assert metadata.platform.name == "QQ / OneBot v11"


class GroupContextOneBot(OneBotV11Adapter):
    def __init__(self):
        self._group_context_cache_data = {}
        self.calls = []

    async def call_api(self, action, params=None):
        self.calls.append((action, params or {}))
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "group_name": "测试群",
                "member_count": 12,
                "max_member_count": 200,
            },
        }


def test_onebot_group_context_injects_group_title_and_counts():
    adapter = GroupContextOneBot()
    context = {
        "platform": "onebotv11",
        "chat_id": "10001",
        "user_id": "20002",
        "chat_type": "group",
        "user_name": "群名片",
        "text": "你好",
    }

    enriched = asyncio.run(adapter._enrich_group_context(context))

    assert enriched["user_name"] == "群名片"
    assert enriched["chat_title"] == "测试群"
    assert enriched["group_title"] == "测试群"
    assert enriched["group_display_name"] == "测试群"
    assert enriched["group_member_count"] == 12
    assert enriched["group_member_count_status"] == "ok"
    assert enriched["group_online_count"] is None
    assert enriched["group_online_count_status"] == "unavailable:onebotv11_standard"


class IncomingOnlyOneBot(OneBotV11Adapter):
    def __init__(self):
        self.self_id = "999"
        self.group_message_policy = "at_or_reply"
        self.download_media = False
        self._chat_types = {}
        self._last_incoming_contexts = {}
        self.current_chat_id = None

    async def _enrich_group_context(self, context):
        return context


def test_onebot_group_mention_only_still_reaches_aggregator():
    async def run():
        adapter = IncomingOnlyOneBot()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 10001,
            "user_id": 20002,
            "message_id": 888,
            "sender": {"card": "Alice", "nickname": "Nick"},
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": " "}},
            ],
        }

        await adapter.handle_onebot_event(event)

        assert len(captured) == 1
        chat_id, text, context = captured[0]
        assert chat_id == "10001"
        assert text == MENTION_ONLY_TEXT
        assert context["text"] == MENTION_ONLY_TEXT
        assert context["chat_type"] == "group"
        assert context["user_id"] == "20002"
        assert context["user_name"] == "Alice"
        assert context["platform_message_id"] == "888"

    asyncio.run(run())
