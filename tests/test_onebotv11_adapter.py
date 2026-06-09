import asyncio
import json
from types import SimpleNamespace

from adapters.base import AdapterToolSpec
from adapters.onebotv11.client import OneBotWebSocketClient
from adapters.onebotv11.main import MENTION_ONLY_TEXT, OneBotV11Adapter
from adapters.onebotv11.message import (
    is_at_self,
    onebot_message_segments,
    plain_text_from_segments,
    reply_id_from_event,
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


def test_onebot_reply_id_extraction_supports_raw_and_nested_variants():
    assert reply_id_from_event({"raw_message": "[CQ:reply,id=777][CQ:at,qq=999]"}) == "777"
    assert reply_id_from_event({"source": {"message_id": 778}}) == "778"
    assert reply_id_from_event({"reply_to_message_id": 779}) == "779"


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


def test_onebot_sender_prepends_explicit_reply_to_message_id():
    adapter = SenderOnlyOneBot()

    ids = asyncio.run(
        adapter.send_message(
            "10001",
            "收到啦",
            parse_media=False,
            reply_to_message_id="666",
        )
    )

    assert ids == ["42"]
    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [
                    {"type": "reply", "data": {"id": "666"}},
                    {"type": "text", "data": {"text": "收到啦"}},
                ],
            },
        )
    ]


def test_onebot_sender_bare_reply_prefers_explicit_reply_to_message_id():
    adapter = SenderOnlyOneBot()

    asyncio.run(
        adapter.send_message(
            "10001",
            "[reply] 收到啦",
            reply_to_message_id="666",
        )
    )

    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [
                    {"type": "reply", "data": {"id": "666"}},
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


def test_onebot_sender_parses_face_and_emoji_segments():
    adapter = SenderOnlyOneBot()

    ids = asyncio.run(adapter.send_message("10001", "收到[face:14][emoji:66]"))

    assert ids == ["42"]
    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [
                    {"type": "text", "data": {"text": "收到"}},
                    {"type": "face", "data": {"id": "14"}},
                    {"type": "face", "data": {"id": "66"}},
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
        self.calls = []

    async def _enrich_group_context(self, context):
        return context

    async def call_api(self, action, params=None):
        self.calls.append((action, params or {}))
        if action == "get_msg":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": params["message_id"],
                    "sender": {"user_id": 30003, "card": "Bob", "nickname": "Bobby"},
                    "message": [{"type": "text", "data": {"text": "old message"}}],
                },
            }
        if action == "get_image":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"file": "data/onebotv11/resolved.jpg"},
            }
        return {"status": "ok", "retcode": 0, "data": {}}


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


def test_onebot_group_reply_image_with_mention_includes_replied_media():
    async def run():
        adapter = IncomingOnlyOneBot()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        async def fake_call_api(action, params=None):
            adapter.calls.append((action, params or {}))
            assert action == "get_msg"
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 777,
                    "sender": {"user_id": 30003, "card": "Bob", "nickname": "Bobby"},
                    "message": [
                        {"type": "image", "data": {"file": "old.jpg", "url": "http://example.test/old.jpg"}},
                        {"type": "text", "data": {"text": "旧图说明"}},
                    ],
                },
            }

        adapter.call_api = fake_call_api
        adapter._aggregator = FakeAggregator()
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 10001,
            "user_id": 20002,
            "message_id": 889,
            "sender": {"card": "Alice", "nickname": "Nick"},
            "message": [
                {"type": "reply", "data": {"id": "777"}},
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": " 这张是什么"}},
            ],
        }

        await adapter.handle_onebot_event(event)

        assert len(captured) == 1
        chat_id, text, context = captured[0]
        assert chat_id == "10001"
        assert "[回复内容]" in text
        assert "[image: http://example.test/old.jpg]" in text
        assert "旧图说明" in text
        assert "这张是什么" in text
        assert "回复消息ID" not in text
        assert context["reply_to_message_id"] == "777"
        assert context["reply_to_user_id"] == "30003"
        assert context["reply_to_user_name"] == "Bob"
        assert context["platform_message_ids"] == ["777", "889"]

    asyncio.run(run())


def test_onebot_group_reply_image_when_reply_only_exists_in_raw_message():
    async def run():
        adapter = IncomingOnlyOneBot()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        async def fake_call_api(action, params=None):
            adapter.calls.append((action, params or {}))
            assert action == "get_msg"
            assert params == {"message_id": 777}
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "message_id": 777,
                    "sender": {"user_id": 30003, "card": "Bob"},
                    "message": [{"type": "image", "data": {"url": "http://example.test/raw-reply.jpg"}}],
                },
            }

        adapter.call_api = fake_call_api
        adapter._aggregator = FakeAggregator()
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 10001,
            "user_id": 20002,
            "message_id": 891,
            "raw_message": "[CQ:reply,id=777][CQ:at,qq=999]",
            "sender": {"card": "Alice", "nickname": "Nick"},
            "message": [
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": " "}},
            ],
        }

        await adapter.handle_onebot_event(event)

        assert len(captured) == 1
        text = captured[0][1]
        context = captured[0][2]
        assert text != MENTION_ONLY_TEXT
        assert "[image: http://example.test/raw-reply.jpg]" in text
        assert context["reply_to_message_id"] == "777"
        assert context["platform_message_ids"] == ["777", "891"]

    asyncio.run(run())


def test_onebot_reply_image_file_only_uses_get_image():
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
            "message_id": 890,
            "sender": {"card": "Alice", "nickname": "Nick"},
            "message": [
                {"type": "reply", "data": {"id": "778"}},
                {"type": "at", "data": {"qq": "999"}},
            ],
        }

        async def fake_call_api(action, params=None):
            adapter.calls.append((action, params or {}))
            if action == "get_msg":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "message_id": 778,
                        "sender": {"user_id": 30003, "card": "Bob"},
                        "message": [{"type": "image", "data": {"file": "abc.image"}}],
                    },
                }
            if action == "get_image":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"file": "data/onebotv11/abc.jpg"},
                }
            return {"status": "ok", "retcode": 0, "data": {}}

        adapter.call_api = fake_call_api

        await adapter.handle_onebot_event(event)

        assert "[image: data/onebotv11/abc.jpg]" in captured[0][1]
        assert ("get_image", {"file": "abc.image"}) in adapter.calls

    asyncio.run(run())


def test_onebot_client_dispatches_events_without_blocking_echo_results():
    async def run():
        class FakeWs:
            def __init__(self):
                self.closed = False
                self.sent = []

            async def send_str(self, data):
                self.sent.append(json.loads(data))

        class EventCallsApiAdapter:
            def __init__(self):
                self.client = OneBotWebSocketClient(self)
                self.client.ws = FakeWs()
                self.handled = asyncio.Event()

            async def handle_onebot_event(self, payload):
                response = await self.client.call_api("get_msg", {"message_id": payload["message_id"]}, timeout=1)
                assert response["data"]["message_id"] == payload["message_id"]
                self.handled.set()

        adapter = EventCallsApiAdapter()
        event_payload = {"post_type": "message", "message_id": 123}

        await adapter.client.handle_payload(event_payload)
        await asyncio.sleep(0)

        assert len(adapter.client.event_tasks) == 1
        assert len(adapter.client.ws.sent) == 1
        echo = adapter.client.ws.sent[0]["echo"]

        await adapter.client.handle_payload(
            {
                "status": "ok",
                "retcode": 0,
                "echo": echo,
                "data": {"message_id": 123},
            }
        )
        await asyncio.wait_for(adapter.handled.wait(), timeout=0.2)

    asyncio.run(run())


def test_onebot_client_keeps_same_chat_events_ordered():
    async def run():
        class OrderedAdapter:
            def __init__(self):
                self.client = OneBotWebSocketClient(self)
                self.first_can_finish = asyncio.Event()
                self.handled = []

            async def handle_onebot_event(self, payload):
                if payload["message_id"] == 1:
                    await self.first_can_finish.wait()
                self.handled.append(payload["message_id"])

        adapter = OrderedAdapter()
        await adapter.client.handle_payload(
            {"post_type": "message", "message_type": "group", "group_id": 10001, "message_id": 1}
        )
        await adapter.client.handle_payload(
            {"post_type": "message", "message_type": "group", "group_id": 10001, "message_id": 2}
        )
        await asyncio.sleep(0)

        assert adapter.handled == []
        adapter.first_can_finish.set()
        await asyncio.gather(*adapter.client.event_tasks)
        assert adapter.handled == [1, 2]

    asyncio.run(run())
