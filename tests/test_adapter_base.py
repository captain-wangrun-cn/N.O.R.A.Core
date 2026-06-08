import asyncio

from adapters.base import (
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
        ]
    )

    text = message.to_text()

    assert "看这个" in text
    assert "[image: data/telegram/a.jpg]" in text
    assert "[video: data/telegram/b.mp4]" in text


def test_adapter_event_from_context_defaults_and_roundtrip():
    event = AdapterEvent.from_context(
        {
            "platform": "telegram",
            "chat_id": "chat-1",
            "text": "hello",
        }
    )

    context = event.to_context()

    assert event.user_id == "chat-1"
    assert event.chat_type == "private"
    assert context["platform"] == "telegram"
    assert context["text"] == "hello"


def test_base_adapter_loads_metadata():
    adapter = DummyAdapter()

    metadata = adapter.load_metadata()

    assert metadata.adapter_id == "telegram"
    assert metadata.platform.name == "Telegram"
    assert metadata.features["supports_rich_media"] is True


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
