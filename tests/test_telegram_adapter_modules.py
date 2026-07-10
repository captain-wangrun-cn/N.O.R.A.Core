import asyncio
from types import SimpleNamespace

from adapters.base import MessageSegment
from adapters.telegram.constants import FILE_PATTERN, TG_MSG_MAX_LENGTH
from adapters.telegram.formatting import _markdown_to_html, _split_long_text
from adapters.telegram.incoming import MENTION_ONLY_TEXT
from adapters.telegram.message import (
    TelegramUserLabelCache,
    context_from_update,
    decorate_native_references,
    has_bot_mention,
    media_message,
    strip_bot_mention,
    telegram_reliable_users,
    telegram_text_mention_markers,
)
from adapters.telegram.main import TelegramAdapter
from brain.prompts import load_identity_context
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
    chat_title="测试群",
    entities=None,
    caption_entities=None,
):
    chat = SimpleNamespace(id=-100 if chat_type != "private" else user_id, type=chat_type)
    if chat_type != "private":
        chat.title = chat_title
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
        entities=list(entities or []),
        caption_entities=list(caption_entities or []),
        photo=None,
        document=None,
        sticker=None,
    )
    return SimpleNamespace(effective_chat=chat, effective_user=user, message=message)


class _IncomingOnlyTelegram(TelegramAdapter):
    def __init__(self):
        self.bot_username = "NoraBot"
        self.bot_user_id = "999"
        self._user_label_cache = TelegramUserLabelCache()
        class FakeBot:
            id = 999

            async def get_chat_member_count(self, chat_id):
                return 42

        self.application = SimpleNamespace(bot=FakeBot())


class _SendingOnlyTelegram(TelegramAdapter):
    def __init__(self):
        self.workspace_root = "D:/repo"
        self.downloads_dir = "D:/repo/downloads"
        self.calls = []
        self.member_calls = []
        self.members = {}
        self._user_label_cache = TelegramUserLabelCache()

        class FakeBot:
            def __init__(self, outer):
                self.outer = outer

            async def send_message(self, **kwargs):
                self.outer.calls.append(("send_message", kwargs))
                return SimpleNamespace(message_id=901)

            async def get_chat_member(self, chat_id, user_id):
                self.outer.member_calls.append((str(chat_id), str(user_id)))
                member = self.outer.members.get(str(user_id))
                if isinstance(member, Exception):
                    raise member
                if member is None:
                    raise LookupError(user_id)
                return SimpleNamespace(user=member)

            async def send_chat_action(self, **kwargs):
                self.outer.calls.append(("send_chat_action", kwargs))

            async def send_photo(self, **kwargs):
                self.outer.calls.append(("send_photo", kwargs))
                return SimpleNamespace(message_id=902)

        self.application = SimpleNamespace(bot=FakeBot(self))

    async def _send_with_retry(self, send_coro_factory, retries=2, base_delay=1.0):
        return await send_coro_factory()


def test_telegram_sender_uses_explicit_reply_to_message_id():
    adapter = _SendingOnlyTelegram()

    ids = asyncio.run(
        adapter.send_message(
            "123",
            "hello",
            parse_media=False,
            reply_to_message_id="456",
        )
    )

    assert ids == ["901"]
    assert adapter.calls == [
        (
            "send_message",
            {
                "chat_id": "123",
                "text": "hello",
                "parse_mode": "HTML",
                "reply_to_message_id": 456,
                "allow_sending_without_reply": True,
            },
        )
    ]


def test_telegram_sender_ignores_non_numeric_reply_to_message_id():
    adapter = _SendingOnlyTelegram()

    asyncio.run(
        adapter.send_message(
            "123",
            "hello",
            parse_media=False,
            reply_to_message_id="cb-456",
        )
    )

    assert "reply_to_message_id" not in adapter.calls[0][1]
    assert "allow_sending_without_reply" not in adapter.calls[0][1]


def test_telegram_sender_canonical_reply_overrides_fallback_and_mentions_group_user():
    adapter = _SendingOnlyTelegram()
    adapter.members["321"] = SimpleNamespace(
        id=321,
        first_name="Alice",
        last_name="Smith",
        username="alice",
    )

    asyncio.run(
        adapter.send_message(
            "-100",
            "[reply:777]hello [at:321]world",
            parse_media=False,
            chat_type="group",
            reply_to_message_id="456",
        )
    )

    call = adapter.calls[0][1]
    assert call["reply_to_message_id"] == 777
    assert call["allow_sending_without_reply"] is True
    assert '<a href="tg://user?id=321">Alice Smith</a>' in call["text"]
    assert adapter.member_calls == [("-100", "321")]
    assert "[reply:" not in call["text"]
    assert "[at:" not in call["text"]


def test_telegram_sender_reuses_cached_mention_label_and_escapes_html():
    adapter = _SendingOnlyTelegram()
    adapter._user_label_cache.put("321", "Alice & <Admin>")

    asyncio.run(
        adapter.send_message(
            "-100",
            "[at:321]hello [at:321]again",
            parse_media=False,
            chat_type="supergroup",
        )
    )

    text = adapter.calls[0][1]["text"]
    assert text.count('<a href="tg://user?id=321">Alice &amp; &lt;Admin&gt;</a>') == 2
    assert adapter.member_calls == []


def test_telegram_sender_uses_username_and_caches_chat_member_lookup():
    adapter = _SendingOnlyTelegram()
    adapter.members["321"] = SimpleNamespace(
        id=321,
        first_name="",
        last_name="",
        username="alice",
    )

    asyncio.run(
        adapter.send_message(
            "-100",
            "[at:321]first [at:321]second",
            parse_media=False,
            chat_type="group",
        )
    )
    asyncio.run(
        adapter.send_message(
            "-100",
            "[at:321]third",
            parse_media=False,
            chat_type="group",
        )
    )

    assert adapter.member_calls == [("-100", "321")]
    assert '<a href="tg://user?id=321">@alice</a>' in adapter.calls[0][1]["text"]
    assert '<a href="tg://user?id=321">@alice</a>' in adapter.calls[1][1]["text"]


def test_telegram_sender_failed_lookup_falls_back_and_negative_caches():
    adapter = _SendingOnlyTelegram()
    adapter.members["321"] = LookupError("not found")

    asyncio.run(
        adapter.send_message(
            "-100",
            "[at:321]first",
            parse_media=False,
            chat_type="group",
        )
    )
    asyncio.run(
        adapter.send_message(
            "-100",
            "[at:321]second",
            parse_media=False,
            chat_type="group",
        )
    )

    assert adapter.member_calls == [("-100", "321")]
    assert '<a href="tg://user?id=321">@321</a>' in adapter.calls[0][1]["text"]
    assert '<a href="tg://user?id=321">@321</a>' in adapter.calls[1][1]["text"]


def test_telegram_sender_private_and_invalid_mentions_do_not_query_members():
    adapter = _SendingOnlyTelegram()

    asyncio.run(
        adapter.send_message(
            "123",
            "[at:321]private [at:abc]invalid",
            parse_media=False,
            chat_type="private",
        )
    )

    assert adapter.member_calls == []
    assert "[at:" not in adapter.calls[0][1]["text"]
def test_telegram_sender_removes_mentions_outside_group_and_ignores_invalid_ids():
    adapter = _SendingOnlyTelegram()

    asyncio.run(
        adapter.send_message(
            "123",
            "before[reply:not-a-number][at:abc]after",
            parse_media=False,
            chat_type="private",
        )
    )

    call = adapter.calls[0][1]
    assert call["text"] == "beforeafter"
    assert "reply_to_message_id" not in call


def test_telegram_sender_reply_applies_only_to_first_platform_chunk():
    adapter = _SendingOnlyTelegram()

    asyncio.run(
        adapter.send_message(
            "123",
            "[reply:777]" + ("a" * (TG_MSG_MAX_LENGTH + 10)),
            parse_media=False,
        )
    )

    assert len(adapter.calls) == 2
    assert adapter.calls[0][1]["reply_to_message_id"] == 777
    assert "reply_to_message_id" not in adapter.calls[1][1]


def test_telegram_sender_reply_applies_to_first_media_when_no_text():
    adapter = _SendingOnlyTelegram()

    asyncio.run(
        adapter.send_message(
            "-100",
            "[reply:777][image: https://example.com/photo.jpg]",
            chat_type="group",
        )
    )

    media_calls = [call for name, call in adapter.calls if name == "send_photo"]
    assert len(media_calls) == 1
    assert media_calls[0]["reply_to_message_id"] == 777
    assert media_calls[0]["allow_sending_without_reply"] is True


def test_telegram_html_fallback_does_not_leak_message_controls():
    class _FallbackTelegram(_SendingOnlyTelegram):
        def __init__(self):
            super().__init__()
            original_send = self.application.bot.send_message
            state = {"failed": False}

            async def send_message(**kwargs):
                if kwargs.get("parse_mode") == "HTML" and not state["failed"]:
                    state["failed"] = True
                    self.calls.append(("send_message", kwargs))
                    raise ValueError("bad html")
                return await original_send(**kwargs)

            self.application.bot.send_message = send_message

    adapter = _FallbackTelegram()
    ids = asyncio.run(
        adapter.send_message(
            "-100",
            "[reply:777][at:321]hello",
            parse_media=False,
            chat_type="group",
        )
    )

    assert ids == ["901"]
    fallback = adapter.calls[-1][1]
    assert fallback["reply_to_message_id"] == 777
    assert "[reply:" not in fallback["text"]
    assert "[at:" not in fallback["text"]


def test_telegram_native_reference_helpers_use_reliable_ids_only():
    reply = SimpleNamespace(message_id=777)
    text_mention = SimpleNamespace(
        type="text_mention",
        offset=0,
        length=5,
        user=SimpleNamespace(id=321),
    )
    bot_mention = SimpleNamespace(
        type="text_mention",
        offset=6,
        length=4,
        user=SimpleNamespace(id=999),
    )
    username_mention = SimpleNamespace(
        type="mention",
        offset=11,
        length=6,
        user=None,
    )
    message = SimpleNamespace(
        text="Alice Nora @carol",
        caption=None,
        entities=[text_mention, bot_mention, username_mention],
        caption_entities=[],
        reply_to_message=reply,
    )

    user_ids, markers = telegram_text_mention_markers(message, bot_user_id="999")
    decorated, decorated_ids = decorate_native_references(
        message,
        "hello",
        bot_user_id="999",
    )

    assert user_ids == ["321"]
    assert markers == "[at:321]"
    assert decorated == "[reply:777][at:321]hello"
    assert decorated_ids == ["321"]


def test_telegram_reliable_users_collects_sender_reply_and_text_mentions():
    reply_user = SimpleNamespace(id=222, first_name="Reply", last_name="User", username="reply")
    text_user = SimpleNamespace(id=333, first_name="Text", last_name="Mention", username="text")
    caption_user = SimpleNamespace(id=444, first_name="Caption", last_name="Mention", username="caption")
    update = _fake_update(
        reply_to_message=SimpleNamespace(from_user=reply_user),
        entities=[SimpleNamespace(type="text_mention", user=text_user)],
        caption_entities=[SimpleNamespace(type="text_mention", user=caption_user)],
    )

    users = telegram_reliable_users(update)

    assert [str(user.id) for user in users] == ["123", "222", "333", "444"]


def test_group_filter_populates_user_label_cache_before_rejecting_message():
    adapter = _IncomingOnlyTelegram()
    update = _fake_update(chat_type="group", text="not addressed to bot")

    should_process = asyncio.run(adapter._should_process_message(update))

    assert should_process is False
    assert adapter._user_label_cache.get("123") == (True, "Alice Smith")


def test_telegram_user_label_cache_bounds_expires_and_allows_positive_override():
    now = [100.0]
    cache = TelegramUserLabelCache(
        max_size=2,
        ttl=10.0,
        negative_ttl=2.0,
        clock=lambda: now[0],
    )
    cache.put("1", "One")
    cache.put("2", "Two")
    cache.put("3", "Three")

    assert cache.get("1") == (False, None)
    assert cache.get("2") == (True, "Two")
    cache.put_missing("4")
    assert cache.get("4") == (True, None)
    now[0] += 3.0
    assert cache.get("4") == (False, None)
    cache.put_missing("4")
    cache.put("4", "Four")
    assert cache.get("4") == (True, "Four")
    now[0] += 11.0
    assert cache.get("4") == (False, None)


    entities = [
        SimpleNamespace(
            type="text_mention",
            offset=0,
            length=5,
            user=SimpleNamespace(id=321),
        ),
        SimpleNamespace(
            type="text_mention",
            offset=6,
            length=4,
            user=SimpleNamespace(id=999),
        ),
    ]
    update = _fake_update(text="Alice Nora", entities=entities)

    context = context_from_update(update, "hello", bot_user_id="999")

    assert context["mentioned_user_ids"] == ["321"]


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
        assert context["chat_title"] == "测试群"
        assert context["group_member_count"] == 42
        assert context["group_online_count"] is None
        assert context["group_online_count_status"] == "unavailable:telegram_bot_api"

    asyncio.run(run())


def test_group_mention_only_still_reaches_aggregator():
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
        update = _fake_update(chat_type="group", text="@NoraBot", message_id=779)

        await adapter._handle_incoming_message(update, None)

        assert len(captured) == 1
        chat_id, text, context = captured[0]
        assert chat_id == "-100"
        assert text == MENTION_ONLY_TEXT
        assert context["text"] == MENTION_ONLY_TEXT
        assert context["chat_type"] == "group"
        assert context["user_id"] == "123"
        assert context["user_name"] == "Alice Smith"
        assert context["platform_message_id"] == "779"

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
                "update": update,
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
        assert captured[0]["chat_title"] == "测试群"
        assert captured[0]["group_member_count"] == 42

    asyncio.run(run())


def test_media_group_flush_preserves_native_reply_and_caption_mentions_once():
    async def run():
        adapter = _IncomingOnlyTelegram()
        captured = []

        class FakeAggregator:
            async def add_message(self, chat_id, text, context):
                captured.append((text, context))

        adapter._aggregator = FakeAggregator()
        reply = SimpleNamespace(message_id=777)
        caption_entities = [
            SimpleNamespace(
                type="text_mention",
                offset=0,
                length=5,
                user=SimpleNamespace(id=321),
            )
        ]
        update = _fake_update(
            chat_type="group",
            caption="Alice album",
            reply_to_message=reply,
            caption_entities=caption_entities,
        )
        decorated_caption, user_ids = adapter._decorate_native_references(
            update,
            "Alice album",
        )
        adapter._media_group_buffers = {
            "g1": {
                "photos": ["[image: data/telegram/a.jpg]", "[image: data/telegram/b.jpg]"],
                "caption": decorated_caption,
                "chat_id": "-100",
                "update": update,
                "context": context_from_update(update, "", bot_user_id="999"),
                "chat_type": "group",
                "reply_info": "quoted text",
                "mentioned_user_ids": user_ids,
                "native_references_decorated": True,
                "platform": "telegram",
                "message_ids": ["101", "102"],
            }
        }
        adapter._media_group_timers = {"g1": object()}

        await adapter._flush_media_group("g1")

        text, context = captured[0]
        assert text.count("[reply:777]") == 1
        assert text.count("[at:321]") == 1
        assert "[回复: quoted text]" in text
        assert context["mentioned_user_ids"] == ["321"]
        assert context["platform_message_ids"] == ["101", "102"]

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
        assert calls[0]["chat_title"] == "测试群"
        assert calls[0]["group_member_count"] == 42

    asyncio.run(run())


def test_callback_context_includes_sender_identity_and_platform_message_id():
    async def run():
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
                chat=SimpleNamespace(id=-100, type="group", title="按钮群"),
            ),
        )

        context = await adapter._callback_context(query, "/set_stream on")

        assert context["platform"] == "telegram"
        assert context["chat_id"] == "-100"
        assert context["user_id"] == "321"
        assert context["user_name"] == "Carol Jones"
        assert context["chat_type"] == "group"
        assert context["platform_message_id"] == "cb-909"
        assert context["chat_title"] == "按钮群"
        assert context["group_member_count"] == 42

    asyncio.run(run())


def test_identity_context_includes_group_chat_scene_counts():
    block = load_identity_context(
        include_schedule=False,
        actor_display_name="Alice",
        is_owner=False,
        chat_type="group",
        chat_title="测试群",
        group_member_count=42,
        group_member_count_status="ok",
        group_online_count=None,
        group_online_count_status="unavailable:telegram_bot_api",
    )

    assert "<current_chat>" in block
    assert "群聊名称：测试群" in block
    assert "群成员总数：42" in block
    assert "当前在线人数：未知（unavailable:telegram_bot_api）" in block
    assert "不要臆测具体在线人数" in block
