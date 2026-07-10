from adapters.message_controls import (
    format_mention_marker,
    format_reply_marker,
    is_valid_control_id,
    parse_message_controls,
    strip_message_control_markers,
)


def test_parse_controls_preserves_order_and_adjacent_text():
    parsed = parse_message_controls("开头[at:user-1]中间[reply:msg:2]结尾[at:user_3]")

    assert [(token.type, token.value) for token in parsed.tokens] == [
        ("text", "开头"),
        ("mention", "user-1"),
        ("text", "中间"),
        ("reply", "msg:2"),
        ("text", "结尾"),
        ("mention", "user_3"),
    ]
    assert parsed.reply_message_id == "msg:2"
    assert parsed.mention_user_ids == ["user-1", "user_3"]
    assert parsed.visible_text() == "开头中间结尾"


def test_parse_controls_accepts_whitespace_and_only_first_reply():
    parsed = parse_message_controls("[ reply : first ]正文[reply:second][AT: user.1 ]")

    assert parsed.reply_message_id == "first"
    assert parsed.mention_user_ids == ["user.1"]
    assert parsed.visible_text() == "正文"
    assert [(token.type, token.value) for token in parsed.tokens] == [
        ("reply", "first"),
        ("text", "正文"),
        ("mention", "user.1"),
    ]


def test_invalid_control_ids_are_ignored_without_removing_body():
    parsed = parse_message_controls("前[reply:bad id]中[at:]后")

    assert parsed.reply_message_id == ""
    assert parsed.mention_user_ids == []
    assert parsed.visible_text() == "前中后"


def test_format_helpers_validate_opaque_ids():
    assert is_valid_control_id("abc_123:part-1.2") is True
    assert is_valid_control_id("bad id") is False
    assert format_reply_marker(" 123 ") == "[reply:123]"
    assert format_mention_marker("user-1") == "[at:user-1]"
    assert format_reply_marker("") == ""


def test_strip_controls_removes_valid_malformed_and_legacy_looking_markers():
    cleaned = strip_message_control_markers(
        "A[reply:123]B[at:user]C[reply]D[at]E[reply:bad id]F"
    )

    assert cleaned == "ABCDEF"
