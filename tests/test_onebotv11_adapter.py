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
    assert "onebotv11_send_private_msg" in names
    assert "onebotv11_send_group_msg" in names
    assert "onebotv11_set_group_ban" in names
    assert "onebotv11_napcat_send_poke" not in names
    assert all(isinstance(tool, AdapterToolSpec) for tool in tools)
    assert next(tool for tool in tools if tool.name == "onebotv11_send_private_msg").risk == "medium"
    assert next(tool for tool in tools if tool.name == "onebotv11_send_private_msg").requires_owner_confirmation is True
    assert next(tool for tool in tools if tool.name == "onebotv11_send_group_msg").requires_owner_confirmation is True
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


def test_onebot_send_message_tools_call_standard_actions():
    adapter = ToolOnlyOneBot()

    private = _loads(asyncio.run(adapter.onebotv11_send_private_msg("20002", "你好", auto_escape=True)))
    group = _loads(asyncio.run(adapter.onebotv11_send_group_msg("大家好")))

    assert private["ok"] is True
    assert private["target_user_id"] == "20002"
    assert group["ok"] is True
    assert group["group_id"] == "10001"
    assert adapter.calls == [
        (
            "send_private_msg",
            {"user_id": 20002, "message": "你好", "auto_escape": True},
        ),
        (
            "send_group_msg",
            {"group_id": 10001, "message": "大家好", "auto_escape": False},
        ),
    ]


def test_onebot_send_message_tools_reject_empty_content():
    adapter = ToolOnlyOneBot()

    private = _loads(asyncio.run(adapter.onebotv11_send_private_msg("20002", "  ")))
    group = _loads(asyncio.run(adapter.onebotv11_send_group_msg("")))

    assert private["ok"] is False
    assert group["ok"] is False
    assert "message is required" in private["error"]
    assert "message is required" in group["error"]
    assert adapter.calls == []


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
        self.enable_napcat_api = True
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


def test_onebot_sender_bare_reply_does_not_use_recent_message():
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


def test_onebot_sender_canonical_at_uses_native_segment_without_numeric_text():
    adapter = SenderOnlyOneBot()

    asyncio.run(adapter.send_message("10001", "[at:20002]看这里", chat_type="group"))

    segments = adapter.calls[0][1]["message"]
    assert segments == [
        {"type": "at", "data": {"qq": "20002"}},
        {"type": "text", "data": {"text": " 看这里"}},
    ]
    assert all(
        "@20002" not in str(segment.get("data", {}).get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    )


def test_onebot_sender_at_spacing_before_media_and_at_end():
    adapter = SenderOnlyOneBot()
    adapter._resolve_local_media_path = lambda path: "https://example.test/a.jpg"

    asyncio.run(adapter.send_message("10001", "[at:20002][image:a.jpg]"))
    asyncio.run(adapter.send_message("10001", "[at:20002]"))

    assert adapter.calls[0][1]["message"][:3] == [
        {"type": "at", "data": {"qq": "20002"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "image", "data": {"file": "https://example.test/a.jpg"}},
    ]
    assert adapter.calls[1][1]["message"] == [
        {"type": "at", "data": {"qq": "20002"}},
        {"type": "text", "data": {"text": " "}},
    ]


def test_onebot_sender_poke_marker_side_effect_and_marker_only():
    adapter = SenderOnlyOneBot()

    ids = asyncio.run(adapter.send_message("10001", "[poke:20002][poke:20002]你好"))
    marker_only_ids = asyncio.run(adapter.send_message("20002", "[poke:30003]", chat_type="private"))

    assert ids == ["42"]
    assert marker_only_ids == []
    assert adapter.calls == [
        ("send_poke", {"user_id": 20002, "group_id": 10001}),
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [{"type": "text", "data": {"text": "你好"}}],
            },
        ),
        ("send_poke", {"user_id": 30003}),
    ]


def test_onebot_sender_poke_disabled_is_stripped_but_text_sends():
    adapter = SenderOnlyOneBot()
    adapter.enable_napcat_api = False

    asyncio.run(adapter.send_message("10001", "[poke:20002]继续"))

    assert adapter.calls == [
        (
            "send_msg",
            {
                "message_type": "group",
                "group_id": 10001,
                "message": [{"type": "text", "data": {"text": "继续"}}],
            },
        )
    ]


def test_onebot_sender_strips_at_control_in_private_chat():
    adapter = SenderOnlyOneBot()

    asyncio.run(adapter.send_message("10001", "[at:20002] 私聊正文", chat_type="private"))

    assert adapter.calls[0][1]["message"] == [
        {"type": "text", "data": {"text": " 私聊正文"}},
    ]


def test_onebot_sender_split_fallback_reply_only_applies_to_first_part():
    adapter = SenderOnlyOneBot()

    asyncio.run(
        adapter.send_message(
            "10001",
            "第一段[SPLIT:0]第二段",
            reply_to_message_id="666",
        )
    )

    assert adapter.calls[0][1]["message"][0] == {"type": "reply", "data": {"id": "666"}}
    assert all(
        segment.get("type") != "reply"
        for segment in adapter.calls[1][1]["message"]
    )


def test_onebot_sender_split_explicit_reply_is_local_to_its_part():
    adapter = SenderOnlyOneBot()

    asyncio.run(adapter.send_message("10001", "[reply:666]第一段[SPLIT:0][at:20002]第二段"))

    assert adapter.calls[0][1]["message"][0] == {"type": "reply", "data": {"id": "666"}}
    assert adapter.calls[1][1]["message"][0] == {"type": "at", "data": {"qq": "20002"}}
    assert all(
        segment.get("type") != "reply"
        for segment in adapter.calls[1][1]["message"]
    )


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
        if action == "get_group_member_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": params["user_id"], "card": "Bob", "nickname": "Bobby"},
            }
        if action == "get_image":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"file": "data/onebotv11/resolved.jpg"},
            }
        return {"status": "ok", "retcode": 0, "data": {}}


def test_onebot_image_sticker_type_variants_and_marketface():
    async def run():
        adapter = IncomingOnlyOneBot()

        for field in ("subType", "sub_type", "type"):
            marker = await adapter._segment_to_nora_text(
                {"type": "image", "data": {"file": "sticker.webp", field: 1}}
            )
            assert marker == "[sticker: data/onebotv11/resolved.jpg]"

        normal = await adapter._segment_to_nora_text(
            {"type": "image", "data": {"file": "photo.jpg", "type": 0}}
        )
        marketface = await adapter._segment_to_nora_text(
            {"type": "marketface", "data": {"file": "market.webp"}}
        )
        assert normal == "[image: data/onebotv11/resolved.jpg]"
        assert marketface == "[sticker: data/onebotv11/resolved.jpg]"

    asyncio.run(run())


def test_plain_text_from_segments_labels_sticker_variants():
    assert plain_text_from_segments([
        {"type": "image", "data": {"file": "a.webp", "type": 1}},
        {"type": "mface", "data": {"file": "b.webp"}},
    ]) == "[表情包:a.webp][表情包:b.webp]"


# --- 表情包不算"需要模型去看的媒体" --------------------------------------
# 表情包会被下载并产出 [sticker: path]，但真图按设计不进模型（back_brain 按
# is_sticker 过滤）、也不进 ImageStore。所以引用表情包时不能附
# HISTORICAL_REPLY_MEDIA_NOTE ——那句话告诉模型"上方媒体就是被回复的真实内容"，
# 而模型其实拿不到那张图，等于邀请它幻觉。

def test_marketface_is_sticker_not_media():
    from adapters.onebotv11.message import is_media_segment, is_sticker_segment

    for segment_type in ("mface", "marketface"):
        segment = {"type": segment_type, "data": {"file": "market.webp"}}
        assert is_sticker_segment(segment) is True, segment_type
        assert is_media_segment(segment) is False, segment_type


def test_image_subtype_1_is_sticker_not_media():
    """三个字段名都要认——不同实现（NapCat / go-cqhttp / Lagrange）用的键不一样。"""
    from adapters.onebotv11.message import is_media_segment, is_sticker_segment

    for field in ("subType", "sub_type", "type"):
        segment = {"type": "image", "data": {"file": "s.webp", field: 1}}
        assert is_sticker_segment(segment) is True, field
        assert is_media_segment(segment) is False, field


def test_normal_image_is_media():
    from adapters.onebotv11.message import is_media_segment, is_sticker_segment

    segment = {"type": "image", "data": {"file": "photo.jpg", "type": 0}}
    assert is_sticker_segment(segment) is False
    assert is_media_segment(segment) is True


def test_real_media_segments_are_media():
    from adapters.onebotv11.message import is_media_segment

    for segment_type in ("video", "record", "file"):
        assert is_media_segment({"type": segment_type, "data": {"file": "x"}}) is True, segment_type


def test_non_media_segments_are_not_media():
    from adapters.onebotv11.message import is_media_segment

    for segment_type in ("text", "at", "face", "reply"):
        assert is_media_segment({"type": segment_type, "data": {}}) is False, segment_type


def test_reply_to_marketface_does_not_flag_media():
    """引用商城表情：不置 reply_to_contains_media、不附媒体备注。

    置了的话被引用消息 ID 会被补进 platform_message_ids，但表情包没入 ImageStore，
    view_media 查不到——补了也是空，还多给模型一句会诱发幻觉的提示。
    """
    async def run():
        adapter = IncomingOnlyOneBot()
        event = {
            "_reply_message_data": {
                "message": [{"type": "marketface", "data": {"file": "market.webp"}}],
            }
        }
        await adapter._enrich_reply_text(event)
        assert "[sticker:" in event["reply_to_text"]
        assert not event.get("reply_to_contains_media")

    asyncio.run(run())


def test_reply_to_real_image_still_flags_media():
    """真图必须仍然置位——这条是上面那条的对照，防止一刀切改过头。"""
    async def run():
        adapter = IncomingOnlyOneBot()
        event = {
            "_reply_message_data": {
                "message": [{"type": "image", "data": {"file": "photo.jpg", "type": 0}}],
            }
        }
        await adapter._enrich_reply_text(event)
        assert event.get("reply_to_contains_media") is True

    asyncio.run(run())


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
        assert context["explicit_bot_mention"] is True
        assert context["reply_to_bot"] is False

    asyncio.run(run())


def test_onebot_target_group_poke_reaches_normal_dispatch():
    async def run():
        adapter = IncomingOnlyOneBot()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        await adapter.handle_onebot_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 10001,
                "user_id": 20002,
                "target_id": 999,
            }
        )

        assert len(captured) == 1
        chat_id, text, context = captured[0]
        assert chat_id == "10001"
        assert text == "Bob 戳了 Nora 一下"
        assert context["chat_type"] == "group"
        assert context["user_id"] == "20002"
        assert context["user_name"] == "Bob"
        assert context["explicit_bot_mention"] is True
        assert context["event_type"] == "poke"
        assert context["poke_to_bot"] is True
        assert "platform_message_id" not in context

    asyncio.run(run())


def test_onebot_non_target_and_self_pokes_are_ignored():
    async def run():
        adapter = IncomingOnlyOneBot()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        await adapter.handle_onebot_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 10001,
                "user_id": 20002,
                "target_id": 30003,
            }
        )
        await adapter.handle_onebot_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": 999,
                "target_id": 999,
            }
        )
        assert captured == []

    asyncio.run(run())


def test_onebot_online_group_poke_uses_group_listener():
    async def run():
        adapter = IncomingOnlyOneBot()
        aggregated = []
        submitted = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                aggregated.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        adapter._group_presence_for = lambda runtime_key: "online"

        async def submit_group_message(context):
            submitted.append(context)
            return True

        adapter._group_message_handler = submit_group_message
        await adapter.handle_onebot_event(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 10001,
                "user_id": 20002,
                "target_id": 999,
            }
        )

        assert aggregated == []
        assert len(submitted) == 1
        assert submitted[0]["poke_to_bot"] is True
        assert submitted[0]["explicit_bot_mention"] is True

    asyncio.run(run())


def test_onebot_online_group_message_bypasses_aggregator():
    async def run():
        adapter = IncomingOnlyOneBot()
        aggregated = []
        submitted = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                aggregated.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        adapter._group_presence_for = lambda runtime_key: "online"

        async def submit_group_message(context):
            submitted.append(context)
            return True

        adapter._group_message_handler = submit_group_message
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 10001,
            "user_id": 20002,
            "message_id": 890,
            "sender": {"card": "Alice", "nickname": "Nick"},
            "message": [
                {"type": "at", "data": {"qq": "30003"}},
                {"type": "text", "data": {"text": "ordinary group message"}},
            ],
        }

        await adapter.handle_onebot_event(event)

        assert aggregated == []
        assert len(submitted) == 1
        assert submitted[0]["text"] == "[at:30003]ordinary group message"
        assert submitted[0]["mentioned_users"] == [{"user_id": "30003", "display_name": "Bob"}]
        assert submitted[0]["explicit_bot_mention"] is False
        assert submitted[0]["reply_to_bot"] is False

    asyncio.run(run())


def test_onebot_semi_online_reply_to_bot_uses_aggregator():
    async def run():
        adapter = IncomingOnlyOneBot()
        aggregated = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                aggregated.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        adapter._group_presence_for = lambda runtime_key: "semi_online"
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 10001,
            "user_id": 20002,
            "message_id": 891,
            "sender": {"card": "Alice", "nickname": "Nick"},
            "message": [
                {"type": "reply", "data": {"id": "777"}},
                {"type": "text", "data": {"text": "reply to Nora"}},
            ],
        }

        async def fake_enrich(event_data, segments, include_text=True):
            event_data["reply_to_message_id"] = "777"
            event_data["reply_to_user_id"] = "999"
            event_data["reply_to_user_name"] = "Nora"
            if include_text:
                event_data["reply_to_text"] = "previous Nora message"

        adapter._enrich_reply_context = fake_enrich
        await adapter.handle_onebot_event(event)

        assert len(aggregated) == 1
        context = aggregated[0][2]
        assert context["explicit_bot_mention"] is False
        assert context["reply_to_bot"] is True

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
        assert "[reply:777]" in text
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


def test_onebot_group_reply_and_other_mention_use_canonical_markers():
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
            "message_id": 892,
            "sender": {"card": "Alice"},
            "message": [
                {"type": "reply", "data": {"id": "777"}},
                {"type": "at", "data": {"qq": "999"}},
                {"type": "at", "data": {"qq": "30003"}},
                {"type": "text", "data": {"text": " 看这里"}},
            ],
        }

        await adapter.handle_onebot_event(event)

        text = captured[0][1]
        context = captured[0][2]
        assert text.startswith("[reply:777][at:30003]")
        assert "[at:999]" not in text
        assert context["mentioned_user_ids"] == ["30003"]
        assert context["mentioned_users"] == [{"user_id": "30003", "display_name": "Bob"}]
        assert (
            "get_group_member_info",
            {"group_id": 10001, "user_id": 30003, "no_cache": False},
        ) in adapter.calls

    asyncio.run(run())


def test_onebot_mentioned_user_lookup_caches_success_and_failure_by_group():
    async def run():
        adapter = IncomingOnlyOneBot()
        adapter._member_label_cache_data = {}
        calls = []

        async def fake_call_api(action, params=None):
            calls.append((action, params or {}))
            if params["user_id"] == 30003:
                return {"data": {"card": "群名片", "nickname": "昵称"}}
            raise RuntimeError("lookup failed")

        adapter.call_api = fake_call_api
        first = await adapter._resolve_mentioned_users("10001", ["30003", "40004", "all", "999"])
        second = await adapter._resolve_mentioned_users("10001", ["30003", "40004"])
        other_group = await adapter._resolve_mentioned_users("10002", ["30003"])

        assert first == [{"user_id": "30003", "display_name": "群名片"}]
        assert second == first
        assert other_group == first
        assert calls == [
            ("get_group_member_info", {"group_id": 10001, "user_id": 30003, "no_cache": False}),
            ("get_group_member_info", {"group_id": 10001, "user_id": 40004, "no_cache": False}),
            ("get_group_member_info", {"group_id": 10002, "user_id": 30003, "no_cache": False}),
        ]

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


def test_onebot_client_orders_poke_with_same_chat_messages():
    async def run():
        class OrderedAdapter:
            def __init__(self):
                self.client = OneBotWebSocketClient(self)
                self.first_can_finish = asyncio.Event()
                self.handled = []

            async def handle_onebot_event(self, payload):
                marker = payload.get("message_id", payload.get("sub_type"))
                if marker == 1:
                    await self.first_can_finish.wait()
                self.handled.append(marker)

        adapter = OrderedAdapter()
        await adapter.client.handle_payload(
            {"post_type": "message", "message_type": "group", "group_id": 10001, "message_id": 1}
        )
        await adapter.client.handle_payload(
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 10001,
                "user_id": 20002,
                "target_id": 999,
            }
        )
        await adapter.client.handle_payload(
            {"post_type": "message", "message_type": "group", "group_id": 10001, "message_id": 2}
        )
        await asyncio.sleep(0)

        assert adapter.handled == []
        adapter.first_can_finish.set()
        await asyncio.gather(*adapter.client.event_tasks)
        assert adapter.handled == [1, "poke", 2]

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


class ForwardOneBot(IncomingOnlyOneBot):
    """带可编排 get_forward_msg / get_msg 返回的桩，用于聊天记录解析测试。"""

    def __init__(self, forward_payloads=None, msg_payloads=None):
        super().__init__()
        self.forward_payloads = forward_payloads or {}
        self.msg_payloads = msg_payloads or {}

    async def call_api(self, action, params=None):
        params = params or {}
        self.calls.append((action, params))
        if action == "get_forward_msg":
            key = str(params.get("id") or params.get("message_id") or "")
            return {"status": "ok", "retcode": 0, "data": self.forward_payloads.get(key)}
        if action == "get_msg" and str(params.get("message_id")) in self.msg_payloads:
            return {"status": "ok", "retcode": 0, "data": self.msg_payloads[str(params["message_id"])]}
        return await super().call_api(action, params)


def _node(name, segments, user_id="1001"):
    return {"type": "node", "data": {"nickname": name, "user_id": user_id, "content": segments}}


def _text(value):
    return {"type": "text", "data": {"text": value}}


def test_forward_segment_is_expanded_into_structured_text():
    async def run():
        adapter = ForwardOneBot(
            forward_payloads={
                "rec1": {
                    "messages": [
                        _node("Alice", [_text("今天天气不错")]),
                        _node("Bob", [_text("确实"), {"type": "image", "data": {"file": "x.jpg"}}]),
                    ]
                }
            }
        )
        text = await adapter._segment_to_nora_text({"type": "forward", "data": {"id": "rec1"}})

        assert text.startswith("[聊天记录 id=rec1]")
        assert text.endswith("[/聊天记录]")
        assert "Alice: 今天天气不错" in text
        assert "Bob: 确实[图片]" in text
        # 记录内的图片不应触发任何媒体解析
        assert not any(action == "get_image" for action, _ in adapter.calls)

    asyncio.run(run())


def test_forward_expansion_handles_nested_records():
    async def run():
        adapter = ForwardOneBot(
            forward_payloads={
                "outer": {
                    "messages": [
                        _node("Alice", [_text("看这个")]),
                        _node("Alice", [{"type": "forward", "data": {"id": "inner"}}]),
                    ]
                },
                "inner": {"messages": [_node("Carol", [_text("内层内容")])]},
            }
        )
        text = await adapter._segment_to_nora_text({"type": "forward", "data": {"id": "outer"}})

        assert "Alice: 看这个" in text
        assert "Carol: 内层内容" in text
        # 嵌套层带缩进且有独立包裹
        assert "  [聊天记录]" in text
        assert "  Carol: 内层内容" in text

    asyncio.run(run())


def test_forward_expansion_uses_inline_nodes_without_api_call():
    async def run():
        adapter = ForwardOneBot()
        text = await adapter._segment_to_nora_text(
            {"type": "forward", "data": {"id": "rec9", "content": [_node("Dave", [_text("内联节点")])]}}
        )

        assert "Dave: 内联节点" in text
        assert not any(action == "get_forward_msg" for action, _ in adapter.calls)

    asyncio.run(run())


def test_forward_expansion_truncates_long_records_with_tool_hint():
    async def run():
        adapter = ForwardOneBot(
            forward_payloads={
                "big": {"messages": [_node(f"U{i}", [_text("内容" * 40)]) for i in range(40)]}
            }
        )
        text = await adapter._segment_to_nora_text({"type": "forward", "data": {"id": "big"}})

        assert "内容过长已截断" in text
        assert "onebotv11_get_chat_history" in text
        assert len(text) < 3000
        assert text.endswith("[/聊天记录]")

    asyncio.run(run())


def test_forward_expansion_falls_back_when_record_unavailable():
    async def run():
        adapter = ForwardOneBot(forward_payloads={})
        text = await adapter._segment_to_nora_text({"type": "forward", "data": {"id": "gone"}})

        assert "无法获取" in text
        assert "gone" in text

    asyncio.run(run())


def test_get_chat_history_tool_accepts_record_id_and_returns_full_content():
    async def run():
        adapter = ForwardOneBot(
            forward_payloads={"rec1": {"messages": [_node("Alice", [_text("完整内容")])]}}
        )
        payload = _loads(await adapter.onebotv11_get_chat_history("rec1"))

        assert payload["ok"] is True
        assert payload["action"] == "get_chat_history"
        assert "Alice: 完整内容" in payload["result"]

    asyncio.run(run())


def test_get_chat_history_tool_resolves_message_id_containing_record():
    async def run():
        adapter = ForwardOneBot(
            forward_payloads={"rec2": {"messages": [_node("Bob", [_text("藏在消息里")])]}},
            msg_payloads={"555": {"message": [{"type": "forward", "data": {"id": "rec2"}}]}},
        )
        payload = _loads(await adapter.onebotv11_get_chat_history("555"))

        assert payload["ok"] is True
        assert payload["forward_id"] == "rec2"
        assert "Bob: 藏在消息里" in payload["result"]

    asyncio.run(run())


def test_get_chat_history_tool_reports_missing_record():
    async def run():
        adapter = ForwardOneBot()
        empty = _loads(await adapter.onebotv11_get_chat_history(""))
        assert empty["ok"] is False

        missing = _loads(await adapter.onebotv11_get_chat_history("nope"))
        assert missing["ok"] is False
        assert "No chat record" in missing["error"]

    asyncio.run(run())


def test_get_chat_history_tool_is_registered():
    adapter = ToolOnlyOneBot(enable_napcat_api=False)
    names = {spec.name for spec in adapter.get_adapter_tools()}
    assert "onebotv11_get_chat_history" in names
    assert "onebotv11_get_forward_msg" in names
