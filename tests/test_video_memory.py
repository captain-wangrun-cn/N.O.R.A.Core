"""Tests for hidden video metadata emitted by the back-brain model."""

import re


_VIDEO_TAGS_PATTERN = re.compile(
    r'\[VIDEO_TAGS:([^\]\s]+)\](.*?)\[/VIDEO_TAGS\]',
    re.IGNORECASE | re.DOTALL,
)
_VIDEO_DESC_PATTERN = re.compile(
    r'\[VIDEO_DESC:([^\]\s]+)\](.*?)\[/VIDEO_DESC\]',
    re.IGNORECASE | re.DOTALL,
)


def _strip_video_metadata(text: str) -> str:
    cleaned = _VIDEO_TAGS_PATTERN.sub("", text)
    cleaned = _VIDEO_DESC_PATTERN.sub("", cleaned)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


def test_video_desc_pattern_parses_multiline_description():
    response = (
        "哈哈，它跑得还挺快。\n\n"
        "[VIDEO_TAGS:vid_a1b2c3d4]\n"
        "小狗, 草地, 奔跑, 户外\n"
        "[/VIDEO_TAGS]\n"
        "[VIDEO_DESC:vid_a1b2c3d4]\n"
        "一只小狗在草地上快速奔跑。\n"
        "它随后停在镜头前。\n"
        "[/VIDEO_DESC]"
    )

    matches = list(_VIDEO_DESC_PATTERN.finditer(response))

    assert len(matches) == 1
    assert matches[0].group(1) == "vid_a1b2c3d4"
    assert "快速奔跑" in matches[0].group(2)
    assert "停在镜头前" in matches[0].group(2)


def test_strip_video_metadata_removes_tags_and_description():
    response = (
        "哈哈，它跑得还挺快。\n\n"
        "[VIDEO_TAGS:vid_1DgqG]\n"
        "小狗, 草地, 奔跑\n"
        "[/VIDEO_TAGS]\n"
        "[VIDEO_DESC:vid_1DgqG]\n"
        "一只小狗在草地上奔跑。\n"
        "[/VIDEO_DESC]"
    )

    cleaned = _strip_video_metadata(response)

    assert cleaned == "哈哈，它跑得还挺快。"
    assert "VIDEO_TAGS" not in cleaned
    assert "VIDEO_DESC" not in cleaned
    assert "一只小狗在草地上奔跑" not in cleaned


def test_video_metadata_wrong_ids_can_remap_by_input_order():
    expected_ids = ["vid_first", "vid_second"]
    expected_set = set(expected_ids)
    response = (
        "[VIDEO_DESC:vid_xxxxxxxx]\n第一段视频描述。\n[/VIDEO_DESC]\n"
        "[VIDEO_DESC:vid_placeholder]\n第二段视频描述。\n[/VIDEO_DESC]"
    )
    parsed_blocks = [
        (match.group(1).strip(), match.group(2).strip())
        for match in _VIDEO_DESC_PATTERN.finditer(response)
    ]
    extracted = {parsed_id: desc for parsed_id, desc in parsed_blocks}

    for idx, expected_id in enumerate(expected_ids):
        if expected_id in extracted or idx >= len(parsed_blocks):
            continue
        parsed_id, parsed_desc = parsed_blocks[idx]
        if parsed_id not in expected_set and parsed_desc:
            extracted[expected_id] = parsed_desc

    assert extracted["vid_first"] == "第一段视频描述。"
    assert extracted["vid_second"] == "第二段视频描述。"
