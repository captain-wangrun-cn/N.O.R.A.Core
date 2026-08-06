"""媒体消息正文为空时的入库占位。

背景：`[image:...]` / `[sticker:...]` / `[video:...]` 标记会被 extract_*_payloads
剥光，用户只发一张图不配文字时，入库内容就只剩时间戳前缀。followup 的
`_detect_followup_intent` 读到「用户空消息 + AI 没吭声」会直接判 END 静默收尾，
用户视角就是发完图之后 AI 再也没动静（见 docs/onboarding/COMMON_PITFALLS.md）。
"""

from core.message_handler import group_message_content, media_placeholder_text


def _image_ctx(**overrides):
    ctx = {"multimodal_images": [{"image_id": "img-1"}]}
    ctx.update(overrides)
    return ctx


def test_image_only_message_gets_placeholder():
    assert media_placeholder_text(_image_ctx(), "") == "[图片]"


def test_sticker_only_message_gets_sticker_placeholder():
    ctx = {"multimodal_images": [{"image_id": "img-1", "is_sticker": True}]}
    assert media_placeholder_text(ctx, "") == "[表情包]"


def test_mixed_image_and_sticker_is_not_sticker_only():
    ctx = {
        "multimodal_images": [
            {"image_id": "img-1", "is_sticker": True},
            {"image_id": "img-2"},
        ]
    }
    assert media_placeholder_text(ctx, "") == "[图片]"


def test_video_takes_precedence_over_image():
    ctx = _image_ctx(multimodal_videos=[{"video_id": "vid-1"}])
    assert media_placeholder_text(ctx, "") == "[视频]"


def test_existing_text_is_never_replaced():
    assert media_placeholder_text(_image_ctx(), "看看这个") == "看看这个"


def test_whitespace_only_text_is_treated_as_empty():
    assert media_placeholder_text(_image_ctx(), "   \n  ") == "[图片]"


def test_plain_empty_text_without_media_stays_empty():
    assert media_placeholder_text({}, "") == ""


def test_group_message_content_applies_placeholder_with_sender_prefix():
    content = group_message_content(_image_ctx(), "Alice", "", "group")
    assert content == "Alice: [图片]"


def test_private_message_content_applies_placeholder_without_prefix():
    assert group_message_content(_image_ctx(), "Alice", "", "private") == "[图片]"
