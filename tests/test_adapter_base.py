import asyncio

from adapters.base import (
    AdapterToolSpec,
    AdapterEvent,
    AdapterMessage,
    BaseAdapter,
    MessageSegment,
)


class DummyAdapter(BaseAdapter):
    @property
    def platform_name(self) -> str:
        return "telegram"

    def run(self, message_handler):
        self._message_handler = message_handler

    async def send_message(self, chat_id: str, text: str, **kwargs) -> list[str]:
        return ["1"]

    async def start_typing(self, chat_id: str):
        return None

    async def stop_typing(self, chat_id: str):
        return None


def test_message_segments_serialize_to_existing_markers():
    message = AdapterMessage(
        [
            MessageSegment.text("看这个"),
            MessageSegment.image("data/telegram/a.jpg"),
            MessageSegment.video("data/telegram/b.mp4"),
            MessageSegment.reply_reference("message-1"),
            MessageSegment.mention("user-1"),
        ]
    )

    text = message.to_text()

    assert "看这个" in text
    assert "[image: data/telegram/a.jpg]" in text
    assert "[video: data/telegram/b.mp4]" in text
    assert "[reply:message-1]" in text
    assert "[at:user-1]" in text


def test_adapter_event_from_context_defaults_and_roundtrip():
    event = AdapterEvent.from_context(
        {
            "platform": "telegram",
            "chat_id": "chat-1",
            "text": "hello",
            "reply_to_message_id": 101,
            "reply_to_user_id": 202,
            "reply_to_user_name": "Alice",
            "mentioned_user_ids": [303, "404", ""],
        }
    )

    context = event.to_context()

    assert event.user_id == "chat-1"
    assert event.chat_type == "private"
    assert event.reply_to_message_id == "101"
    assert event.reply_to_user_id == "202"
    assert event.mentioned_user_ids == ["303", "404"]
    assert context["platform"] == "telegram"
    assert context["text"] == "hello"
    assert context["reply_to_message_id"] == "101"
    assert context["reply_to_user_id"] == "202"
    assert context["reply_to_user_name"] == "Alice"
    assert context["mentioned_user_ids"] == ["303", "404"]


def test_base_adapter_loads_metadata():
    adapter = DummyAdapter()

    metadata = adapter.load_metadata()

    assert metadata.adapter_id == "telegram"
    assert metadata.platform.name == "Telegram"
    assert metadata.features["supports_rich_media"] is True
    assert metadata.features["supports_chat_member_lookup"] is True


def test_base_adapter_tools_default_empty():
    adapter = DummyAdapter()

    assert adapter.get_adapter_tools() == []


def test_adapter_tool_spec_shape():
    def sample_tool():
        return "ok"

    spec = AdapterToolSpec(
        name="sample_tool",
        callable=sample_tool,
        intro="sample",
        risk="high",
        requires_owner_confirmation=True,
        platform="dummy",
    )

    assert spec.name == "sample_tool"
    assert spec.callable() == "ok"
    assert spec.requires_owner_confirmation is True


def test_dispatch_message_runs_hooks_and_handler():
    class HookedAdapter(DummyAdapter):
        async def before_message(self, context):
            context["text"] += " before"
            calls.append("before")
            return context

        async def after_message(self, context, result=None):
            calls.append(("after", result))

    async def run():
        adapter = HookedAdapter()

        async def handler(context):
            calls.append(("handler", context["text"]))
            return "ok"

        adapter.run(handler)
        result = await adapter.dispatch_message(
            {
                "platform": "telegram",
                "chat_id": "1",
                "user_id": "u1",
                "text": "hello",
            }
        )
        return result

    calls = []
    result = asyncio.run(run())

    assert result == "ok"
    assert calls == ["before", ("handler", "hello before"), ("after", "ok")]
