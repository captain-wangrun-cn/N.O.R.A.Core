from adapters.base import MessageSegment
from adapters.telegram.constants import FILE_PATTERN, TG_MSG_MAX_LENGTH
from adapters.telegram.formatting import _markdown_to_html, _split_long_text
from adapters.telegram.message import media_message
from adapters.telegram.main import TelegramAdapter


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
