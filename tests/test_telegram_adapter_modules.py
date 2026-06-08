import asyncio
from types import SimpleNamespace

from adapters.base import MessageSegment
from adapters.telegram.constants import FILE_PATTERN, TG_MSG_MAX_LENGTH
from adapters.telegram.formatting import _markdown_to_html, _split_long_text
from adapters.telegram.message import context_from_update, has_bot_mention, media_message, strip_bot_mention
from adapters.telegram.main import TelegramAdapter
from core.back_brain import _image_ids_by_platform_message_id
from core.message_handler import _attach_platform_ids_to_media


class _MetadataOnlyTelegram(TelegramAdapter):
    def __init__(self):
        # Avoid requiring a real Telegram token for metadata tests.
        pass


def test_markdown_to_html_handles_common_formatting():
    html = _markdown_to_html("**粗体** and `code`")

    assert "<b>粗体</b>" in html
    assert "<code>code</code>" in html


def test_split_long_text_respects_limit():
    text = "a" * (TG_MSG_MAX_LENGTH + 10)

    chunks = _split_long_text(text, TG_MSG_MAX_LENGTH)

    assert len(chunks) == 2
    assert all(len(chunk) <= TG_MSG_MAX_LENGTH for chunk in chunks)


def test_file_pattern_matches_common_media_tags():
    text = "发图 [image: data/telegram/a.jpg] 和 [video: data/telegram/b.mp4]"

    matches = [next(group for group in match.groups() if group) for match in FILE_PATTERN.finditer(text)]

    assert "data/telegram/a.jpg" in matches
    assert "data/telegram/b.mp4" in matches


def test_media_message_serializes_to_nora_marker():
    message = media_message("image", "data/telegram/a.jpg", "说明")

    text = message.to_text()

    assert "[image: data/telegram/a.jpg]" in text
    assert "说明" in text


def test_telegram_metadata_can_be_loaded_without_token():
    adapter = _MetadataOnlyTelegram()

    metadata = adapter.load_metadata()

    assert metadata.adapter_id == "telegram"
    assert metadata.platform.name == "Telegram"
    assert metadata.limits["max_message_length"] == 4096


def test_message_segment_sticker_marker_includes_file_when_present():
    segment = MessageSegment.sticker("🙂", "pack", "data/telegram/sticker.webp")

    text = segment.to_text_marker()

    assert "[sticker: 🙂 from pack]" in text
    assert "[file: data/telegram/sticker.webp]" in text


def _fake_update(
    *,
    chat_type="private",
    text="",
    caption="",
    user_id=123,
    first_name="Alice",
    last_name="Smith",
    username="alice",
    message_id=456,
    reply_to_message=None,
):
    chat = SimpleNamespace(id=-100 if chat_type != "private" else user_id, type=chat_type)
    user = SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
    )
    message = SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
        photo=None,
        document=None,
        sticker=None,
    )
    return SimpleNamespace(effective_chat=chat, effective_user=user, message=message)


class _IncomingOnlyTelegram(TelegramAdapter):
    def __init__(self):
        self.bot_username = "NoraBot"
        self.application = SimpleNamespace(bot=SimpleNamespace(id=999))


def test_context_from_update_includes_sender_identity_and_platform_message_id():
    update = _fake_update()

    context = context_from_update(update, "hello")

    assert context["platform"] == "telegram"
    assert context["chat_id"] == "123"
    assert context["user_id"] == "123"
    assert context["user_name"] == "Alice Smith"
    assert context["chat_type"] == "private"
    assert context["platform_message_id"] == "456"


def test_strip_bot_mention_removes_trigger_only_text_case_insensitive():
    assert strip_bot_mention("@NoraBot hello", "norabot") == "hello"
    assert strip_bot_mention("hello @norabot", "NoraBot") == "hello"
    assert strip_bot_mention("/context@NoraBot", "NoraBot") == "/context"


def test_has_bot_mention_uses_explicit_mention_boundaries():
    assert has_bot_mention("@NoraBot hello", "NoraBot") is True
    assert has_bot_mention("/context@NoraBot", "NoraBot") is True
    assert has_bot_mention("foo@NoraBot should not wake", "NoraBot") is False


def test_group_processing_requires_bot_mention_even_when_replying_to_bot():
    adapter = _IncomingOnlyTelegram()
    replied_bot_message = SimpleNamespace(from_user=SimpleNamespace(id=999))
    update = _fake_update(chat_type="group", text="reply without mention", reply_to_message=replied_bot_message)

    should_process = asyncio.run(adapter._should_process_message(update))

    assert should_process is False


def test_group_processing_accepts_bot_mention_and_strips_before_aggregation():
    async def run():
        adapter = _IncomingOnlyTelegram()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        adapter.current_chat_id = None
        async def fake_extract_reply_info(message):
            return None

        adapter._extract_reply_info = fake_extract_reply_info
        update = _fake_update(chat_type="group", text="@NoraBot hello there", message_id=777)

        await adapter._handle_incoming_message(update, None)

        assert len(captured) == 1
        chat_id, text, context = captured[0]
        assert chat_id == "-100"
        assert text == "hello there"
        assert context["text"] == "hello there"
        assert context["user_id"] == "123"
        assert context["user_name"] == "Alice Smith"
        assert context["chat_type"] == "group"
        assert context["platform_message_id"] == "777"

    asyncio.run(run())


def test_group_mention_with_reply_keeps_reply_prefix_after_stripping_mention():
    async def run():
        adapter = _IncomingOnlyTelegram()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((chat_id, text, context))

        adapter._aggregator = FakeAggregator()
        async def fake_extract_reply_info(message):
            return "quoted text"

        adapter._extract_reply_info = fake_extract_reply_info
        update = _fake_update(chat_type="group", text="@NoraBot answer this", message_id=778)

        await adapter._handle_incoming_message(update, None)

        assert captured[0][1] == "[回复: quoted text]\nanswer this"

    asyncio.run(run())


def test_media_group_flush_preserves_all_platform_message_ids():
    async def run():
        adapter = _IncomingOnlyTelegram()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append(context)

        adapter._aggregator = FakeAggregator()
        update = _fake_update(chat_type="group", text="@NoraBot album")
        adapter._media_group_buffers = {
            "g1": {
                "photos": ["[image: data/telegram/a.jpg]", "[image: data/telegram/b.jpg]"],
                "caption": "caption",
                "chat_id": "-100",
                "context": context_from_update(update, ""),
                "chat_type": "group",
                "reply_info": None,
                "platform": "telegram",
                "message_ids": ["101", "102"],
            }
        }
        adapter._media_group_timers = {"g1": object()}

        await adapter._flush_media_group("g1")

        assert captured[0]["platform_message_id"] == "101"
        assert captured[0]["platform_message_ids"] == ["101", "102"]

    asyncio.run(run())


def test_platform_message_ids_attach_to_media_by_order():
    media = [{"image_id": "img_a"}, {"image_id": "img_b"}]

    _attach_platform_ids_to_media(media, ["101", "102"])

    assert media[0]["platform_message_id"] == "101"
    assert media[1]["platform_message_id"] == "102"


def test_image_ids_by_platform_message_id_maps_each_media_item():
    mapping = _image_ids_by_platform_message_id(
        [
            {"image_id": "img_a", "platform_message_id": "101"},
            {"image_id": "img_b", "platform_message_id": "102"},
        ]
    )

    assert mapping == {"101": "img_a", "102": "img_b"}


def test_reply_metadata_picks_image_id_for_exact_platform_message_id():
    adapter = _IncomingOnlyTelegram()
    metadata = {
        "image_ids": ["img_a", "img_b"],
        "image_ids_by_platform_message_id": {
            "101": "img_a",
            "102": "img_b",
        },
    }

    assert adapter._pick_image_id_from_metadata(metadata, "102") == "img_b"


def test_reply_history_match_without_image_id_still_uses_image_store():
    async def run():
        adapter = _IncomingOnlyTelegram()
        adapter.workspace_root = "D:/repo"
        reply_photo = SimpleNamespace()
        reply_msg = SimpleNamespace(
            message_id=102,
            chat=SimpleNamespace(id=-100),
            text=None,
            caption="old caption",
            photo=[reply_photo],
        )
        message = SimpleNamespace(reply_to_message=reply_msg)
        adapter.message_history = SimpleNamespace(
            get_context_messages=lambda platform, chat_id: [
                {
                    "content": "Alice: old caption",
                    "metadata": {},
                    "platform_message_ids": ["102"],
                    "message_id": 1,
                }
            ]
        )

        class FakeStore:
            def get_by_platform_message_id(self, platform, platform_message_id, chat_id=None):
                assert platform == "telegram"
                assert platform_message_id == "102"
                assert chat_id == "-100"
                return {"image_id": "img_b", "file_path": "D:/repo/data/telegram/b.jpg"}

        adapter._get_image_store = lambda: FakeStore()
        adapter._resolve_local_media_path = lambda raw_path: raw_path

        result = await adapter._extract_reply_info(message)

        assert result.startswith("[image: data/telegram/b.jpg]")
        assert "Alice: old caption" in result

    asyncio.run(run())


def test_group_media_group_continuation_is_allowed_after_mentioned_first_item():
    adapter = _IncomingOnlyTelegram()
    update = _fake_update(chat_type="group", caption="", message_id=202)
    update.message.media_group_id = "album-1"
    adapter._media_group_buffers = {"album-1": {"photos": []}}

    should_process = asyncio.run(adapter._should_process_message(update))

    assert should_process is True


def test_group_command_requires_mention_and_context_strips_mention():
    async def run():
        adapter = _IncomingOnlyTelegram()
        calls = []

        async def fake_message_handler(ctx):
            calls.append(ctx)

        adapter._message_handler = fake_message_handler

        no_mention = _fake_update(chat_type="group", text="/context", message_id=801)
        await adapter._context_command(no_mention, None)
        assert calls == []

        with_mention = _fake_update(chat_type="group", text="/context @NoraBot", message_id=802)
        await adapter._context_command(with_mention, None)

        assert len(calls) == 1
        assert calls[0]["text"] == "/context"
        assert calls[0]["user_id"] == "123"
        assert calls[0]["user_name"] == "Alice Smith"
        assert calls[0]["platform_message_id"] == "802"

    asyncio.run(run())


def test_callback_context_includes_sender_identity_and_platform_message_id():
    adapter = _IncomingOnlyTelegram()
    query = SimpleNamespace(
        id="cb-909",
        from_user=SimpleNamespace(
            id=321,
            first_name="Carol",
            last_name="Jones",
            username="carol",
        ),
        message=SimpleNamespace(
            message_id=909,
            chat=SimpleNamespace(id=-100, type="group"),
        ),
    )

    context = adapter._callback_context(query, "/set_stream on")

    assert context["platform"] == "telegram"
    assert context["chat_id"] == "-100"
    assert context["user_id"] == "321"
    assert context["user_name"] == "Carol Jones"
    assert context["chat_type"] == "group"
    assert context["platform_message_id"] == "cb-909"
