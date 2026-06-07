"""
Tests for image memory system:
- Image ID generation in multimodal.py
- IMAGE_TAGS parsing from LLM response
- view_media tool output formatting
"""

import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Image ID generation
# ---------------------------------------------------------------------------

def test_image_id_format():
    """每张图片应有 img_<8hex> 格式的唯一 ID"""
    from brain.multimodal import _generate_image_id
    
    ids = {_generate_image_id() for _ in range(100)}
    # 都不重复
    assert len(ids) == 100
    for img_id in ids:
        assert img_id.startswith("img_")
        assert len(img_id) == 12  # img_ + 8 hex chars
        assert re.fullmatch(r'img_[a-f0-9]{8}', img_id)


def test_extract_image_payloads_includes_image_id(tmp_path):
    """extract_image_payloads 应为每张图片分配唯一 ID"""
    from brain.multimodal import extract_image_payloads

    # 创建两个临时图片文件
    img1 = tmp_path / "test1.png"
    img2 = tmp_path / "test2.jpg"
    img1.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    img2.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    text = f"看看这两张图 [image: {img1}] 和 [image: {img2}]"
    clean_text, images = extract_image_payloads(text)

    assert len(images) == 2
    for img in images:
        assert "image_id" in img
        assert img["image_id"].startswith("img_")
        assert len(img["image_id"]) == 12

    # 两张图的 ID 不同
    assert images[0]["image_id"] != images[1]["image_id"]


# ---------------------------------------------------------------------------
# 2. IMAGE_TAGS parsing (test the regex directly to avoid config.yml dep)
# ---------------------------------------------------------------------------

# 复制 controller 中的正则以避免导入整个 controller（需要 config.yml）
# 注意：image_id 偶发可能出现非十六进制字符，因此这里不再限制 [a-f0-9]
_IMAGE_TAGS_PATTERN = re.compile(
    r'\[IMAGE_TAGS:([^\]\s]+)\](.*?)\[/IMAGE_TAGS\]',
    re.IGNORECASE | re.DOTALL,
)
_THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_THINK_INLINE_PATTERN = re.compile(r"</?think>", re.IGNORECASE)
_SPLIT_MARKER_PATTERN = re.compile(r"\[SPLIT(?::([0-9.]+))?\]", re.IGNORECASE)


def _strip_thinking_and_tags(text: str) -> str:
    """模拟 NoraController._strip_thinking_content 的逻辑"""
    cleaned = _THINK_BLOCK_PATTERN.sub("", text)
    cleaned = _THINK_INLINE_PATTERN.sub("", cleaned)
    cleaned = _IMAGE_TAGS_PATTERN.sub("", cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def _strip_stream_think_segment(text: str, in_think_block: bool) -> tuple[str, bool]:
    """模拟 controller 中流式片段级别的 think 清洗逻辑。"""
    if not text:
        return "", in_think_block

    lower_text = text.lower()
    out = []
    i = 0

    while i < len(text):
        if in_think_block:
            close_idx = lower_text.find("</think>", i)
            if close_idx == -1:
                return "".join(out), True
            i = close_idx + len("</think>")
            in_think_block = False
            continue

        open_idx = lower_text.find("<think>", i)
        close_idx = lower_text.find("</think>", i)

        if open_idx == -1 and close_idx == -1:
            out.append(text[i:])
            break

        if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
            out.append(text[i:close_idx])
            i = close_idx + len("</think>")
            continue

        if open_idx != -1:
            out.append(text[i:open_idx])
            i = open_idx + len("<think>")
            in_think_block = True
            continue

    return "".join(out), in_think_block


def _extract_split_ready_parts(buffer: str, in_think_block: bool) -> tuple[list[str], str, bool]:
    """模拟 controller 中 [SPLIT]/[SPILIT] 片段提取。"""
    ready_parts = []
    last_idx = 0

    for marker in _SPLIT_MARKER_PATTERN.finditer(buffer):
        raw_part = buffer[last_idx:marker.start()]
        visible_part, in_think_block = _strip_stream_think_segment(raw_part, in_think_block)
        if visible_part.strip():
            ready_parts.append(visible_part)
        last_idx = marker.end()

    remaining = buffer[last_idx:]
    return ready_parts, remaining, in_think_block


def test_image_tags_pattern_parsing():
    """_IMAGE_TAGS_PATTERN 应正确提取标签"""
    response = (
        "这是一张很美的照片！\n\n"
        "[IMAGE_TAGS:img_a1b2c3d4]\n"
        "风景, 日落, 海滩, 橙色天空, 宁静, 自然, 海浪\n"
        "[/IMAGE_TAGS]"
    )

    matches = list(_IMAGE_TAGS_PATTERN.finditer(response))
    assert len(matches) == 1
    assert matches[0].group(1) == "img_a1b2c3d4"
    assert "日落" in matches[0].group(2)
    assert "海滩" in matches[0].group(2)


def test_image_tags_multiple_images():
    """多张图片应各自有标签块"""
    response = (
        "你发了两张图！第一张是猫，第二张是狗。\n\n"
        "[IMAGE_TAGS:img_11111111]\n"
        "猫咪, 橘猫, 沙发, 室内, 可爱, 宠物\n"
        "[/IMAGE_TAGS]\n"
        "[IMAGE_TAGS:img_22222222]\n"
        "狗, 金毛, 草地, 户外, 活泼, 宠物\n"
        "[/IMAGE_TAGS]"
    )

    matches = list(_IMAGE_TAGS_PATTERN.finditer(response))
    assert len(matches) == 2
    assert matches[0].group(1) == "img_11111111"
    assert "橘猫" in matches[0].group(2)
    assert matches[1].group(1) == "img_22222222"
    assert "金毛" in matches[1].group(2)


def test_image_tags_pattern_non_hex_image_id():
    """image_id 含非十六进制字符时也应被匹配，避免标签泄漏到用户侧。"""
    response = (
        "这是你的图片分析。\n\n"
        "[IMAGE_TAGS:img_1DgqG]\n"
        "二次元, 夜景, 城市灯光\n"
        "[/IMAGE_TAGS]"
    )

    matches = list(_IMAGE_TAGS_PATTERN.finditer(response))
    assert len(matches) == 1
    assert matches[0].group(1) == "img_1DgqG"

    cleaned = _strip_thinking_and_tags(response)
    assert "IMAGE_TAGS" not in cleaned


def test_strip_thinking_content_removes_image_tags():
    """_strip_thinking_and_tags 应同时移除 <think> 和 IMAGE_TAGS"""
    text = (
        "<think>内部思考...</think>\n"
        "这是回复正文\n\n"
        "[IMAGE_TAGS:img_abcdef12]\n"
        "标签内容\n"
        "[/IMAGE_TAGS]"
    )

    cleaned = _strip_thinking_and_tags(text)
    assert "内部思考" not in cleaned
    assert "IMAGE_TAGS" not in cleaned
    assert "标签内容" not in cleaned
    assert "这是回复正文" in cleaned


def test_split_marker_parsing_standard_tokens():
    """仅标准 [SPLIT] 标记应触发分段。"""
    buf = "第一段[SPLIT]第二段[SPLIT]第三段"
    ready, remaining, in_think = _extract_split_ready_parts(buf, False)

    assert ready == ["第一段", "第二段"]
    assert remaining == "第三段"
    assert in_think is False


def test_split_marker_supports_standard_delay_variant():
    """标准 [SPLIT:秒数] 应可被识别。"""
    buf = "第一段[SPLIT:1.2]第二段[SPLIT]第三段"
    ready, remaining, in_think = _extract_split_ready_parts(buf, False)

    assert ready == ["第一段", "第二段"]
    assert remaining == "第三段"
    assert in_think is False


def test_stream_split_does_not_leak_fragmented_think_content():
    """当 <think> 在流式输出中被拆碎时，分段发送不应泄漏思考内容。"""
    in_think = False

    # 第 1 批：只到 <think> 开头，且遇到 [SPLIT]
    buf = "给你结论：<think>先想一想[SPLIT]"
    ready, remaining, in_think = _extract_split_ready_parts(buf, in_think)
    assert ready == ["给你结论："]
    assert remaining == ""
    assert in_think is True

    # 第 2 批：think 继续，直到闭合后再遇到 [SPLIT]
    buf = remaining + "这是内部推理</think>最终答案是 42[SPLIT]尾巴"
    ready, remaining, in_think = _extract_split_ready_parts(buf, in_think)
    assert ready == ["最终答案是 42"]
    assert remaining == "尾巴"
    assert in_think is False


def test_deferred_image_send_preserves_split_boundaries():
    """模拟图片标签待校验时的延迟发送：应保留 [SPLIT] 边界，避免最后并成一段。"""
    final_response_buffer = ""
    deferred_split_parts_emitted = False

    # 模拟流式阶段识别到一个完整分段
    ready_parts = ["第一段"]
    response_text_buffer = "第二段"
    for part in ready_parts:
        text_to_send = part.strip()
        if text_to_send:
            if final_response_buffer:
                final_response_buffer += "[SPLIT]"
            final_response_buffer += text_to_send
            deferred_split_parts_emitted = True

    # 模拟 turn 末尾追加剩余缓冲
    if response_text_buffer:
        if deferred_split_parts_emitted and final_response_buffer:
            final_response_buffer += "[SPLIT]"
        final_response_buffer += response_text_buffer

    assert final_response_buffer == "第一段[SPLIT]第二段"


# ---------------------------------------------------------------------------
# 3. view_media tool schema generation
# ---------------------------------------------------------------------------

def test_view_media_tool_schema_registered():
    """view_media 应出现在 ToolManager 的工具 schema 列表中"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    
    tm = ToolManager(mock_adapter, image_store=None)
    schema_names = [s["name"] for s in tm.get_tool_schemas()]
    assert "view_media" in schema_names
    assert "crop_image_for_llm" in schema_names


def test_view_media_tool_no_image_store():
    """没有 ImageStore 时 view_media 应返回错误"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    
    tm = ToolManager(mock_adapter, image_store=None)
    result = tm.view_media(keyword="猫")
    assert "Error" in result or "not available" in result


def test_view_media_tool_disabled_image_store():
    """ImageStore.enabled=False 时 view_media 应返回错误"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    mock_store = MagicMock()
    mock_store.enabled = False
    
    tm = ToolManager(mock_adapter, image_store=mock_store)
    result = tm.view_media(keyword="猫")
    assert "offline" in result.lower() or "Error" in result


def test_view_media_format_output():
    """view_media 应正确格式化搜索结果"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    mock_store = MagicMock()
    mock_store.enabled = True
    mock_store.search_images.return_value = [
        {
            "image_id": "img_test1234",
            "file_path": "/tmp/test.jpg",
            "tags": "猫咪, 橘猫, 沙发",
            "timestamp": 1709856000.0,
            "user_id": "user123",
        }
    ]

    tm = ToolManager(mock_adapter, image_store=mock_store)
    result = tm.view_media(keyword="猫")
    
    assert "img_test1234" in result
    assert "猫咪" in result
    assert "Found 1 image" in result


def test_crop_image_for_llm_preset_has_priority(tmp_path):
    """crop_image_for_llm: 同时给 preset 和像素范围时，必须优先使用 preset。"""
    from unittest.mock import MagicMock
    from PIL import Image
    from brain.tools import ToolManager

    src = tmp_path / "src.png"
    out = tmp_path / "cropped.png"
    Image.new("RGB", (200, 100), color=(255, 0, 0)).save(src)

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    tm = ToolManager(mock_adapter, image_store=None)

    result = tm.crop_image_for_llm(
        image_path=str(src),
        preset="top_half",
        left=10,
        top=10,
        right=20,
        bottom=20,
        output_path=str(out),
        return_image=False,
    )

    assert "Image crop completed." in result
    assert "Mode: preset:top_half" in result
    assert "pixel range was ignored" in result
    assert out.exists()

    with Image.open(out) as cropped:
        assert cropped.size == (200, 50)


def test_crop_image_for_llm_pixel_range_requires_all_coords(tmp_path):
    """crop_image_for_llm: 不使用 preset 时，left/top/right/bottom 必须全部提供。"""
    from unittest.mock import MagicMock
    from PIL import Image
    from brain.tools import ToolManager

    src = tmp_path / "src2.png"
    Image.new("RGB", (120, 80), color=(0, 255, 0)).save(src)

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    tm = ToolManager(mock_adapter, image_store=None)

    result = tm.crop_image_for_llm(
        image_path=str(src),
        left=10,
        top=10,
        right=90,
        bottom=None,
        return_image=False,
    )

    assert "pixel crop range is incomplete" in result


# ---------------------------------------------------------------------------
# 4. IMAGE_OCR pattern parsing
# ---------------------------------------------------------------------------

_IMAGE_OCR_PATTERN = re.compile(
    r'\[IMAGE_OCR:([^\]\s]+)\](.*?)\[/IMAGE_OCR\]',
    re.IGNORECASE | re.DOTALL,
)


def _remap_extracted_blocks_by_order(
    multimodal_images: list[dict],
    parsed_tag_blocks: list[tuple[str, str]],
    parsed_ocr_blocks: list[tuple[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """模拟 back_brain 中对 IMAGE_TAGS/IMAGE_OCR 的顺序重映射逻辑。"""
    image_tags_extracted: dict[str, str] = {
        img_id: tags for img_id, tags in parsed_tag_blocks if img_id and tags
    }
    image_ocr_extracted: dict[str, str] = {
        img_id: ocr for img_id, ocr in parsed_ocr_blocks if img_id and ocr and ocr != "（无文字）"
    }

    expected_ids = [img["image_id"] for img in multimodal_images]
    expected_set = set(expected_ids)

    for idx, expected_id in enumerate(expected_ids):
        if expected_id in image_tags_extracted:
            continue
        if idx >= len(parsed_tag_blocks):
            break
        parsed_id, parsed_tags = parsed_tag_blocks[idx]
        if parsed_id not in expected_set and parsed_tags:
            image_tags_extracted[expected_id] = parsed_tags

    for idx, expected_id in enumerate(expected_ids):
        if expected_id in image_ocr_extracted:
            continue
        if idx >= len(parsed_ocr_blocks):
            break
        parsed_id, parsed_ocr = parsed_ocr_blocks[idx]
        if parsed_id not in expected_set and parsed_ocr:
            image_ocr_extracted[expected_id] = parsed_ocr

    return image_tags_extracted, image_ocr_extracted


def test_image_ocr_pattern_parsing():
    """_IMAGE_OCR_PATTERN 应正确提取 OCR 文字"""
    response = (
        "这是一张截图！\n\n"
        "[IMAGE_TAGS:img_a1b2c3d4]\n"
        "截图, 代码, IDE, Python\n"
        "[/IMAGE_TAGS]\n"
        "[IMAGE_OCR:img_a1b2c3d4]\n"
        "def hello_world():\n"
        "    print('Hello, World!')\n"
        "[/IMAGE_OCR]"
    )

    matches = list(_IMAGE_OCR_PATTERN.finditer(response))
    assert len(matches) == 1
    assert matches[0].group(1) == "img_a1b2c3d4"
    assert "hello_world" in matches[0].group(2)
    assert "Hello, World!" in matches[0].group(2)


def test_image_ocr_no_text():
    """图片无文字时应匹配到 '（无文字）'"""
    response = (
        "好看的风景照\n\n"
        "[IMAGE_TAGS:img_11111111]\n"
        "风景, 山, 云\n"
        "[/IMAGE_TAGS]\n"
        "[IMAGE_OCR:img_11111111]（无文字）[/IMAGE_OCR]"
    )

    matches = list(_IMAGE_OCR_PATTERN.finditer(response))
    assert len(matches) == 1
    assert "无文字" in matches[0].group(2)


def test_image_ocr_multiple_images():
    """多张图片各自有 OCR 块"""
    response = (
        "[IMAGE_OCR:img_aaaa1111]\n"
        "购物清单\n苹果 3个\n牛奶 1盒\n"
        "[/IMAGE_OCR]\n"
        "[IMAGE_OCR:img_bbbb2222]\n"
        "Error 404: Not Found\n"
        "[/IMAGE_OCR]"
    )

    matches = list(_IMAGE_OCR_PATTERN.finditer(response))
    assert len(matches) == 2
    assert "购物清单" in matches[0].group(2)
    assert "Error 404" in matches[1].group(2)


def test_strip_thinking_content_removes_image_ocr():
    """_strip_thinking_and_tags 应同时移除 IMAGE_OCR 块"""
    def _strip_all(text: str) -> str:
        cleaned = _THINK_BLOCK_PATTERN.sub("", text)
        cleaned = _THINK_INLINE_PATTERN.sub("", cleaned)
        cleaned = _IMAGE_TAGS_PATTERN.sub("", cleaned)
        cleaned = _IMAGE_OCR_PATTERN.sub("", cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        return cleaned

    text = (
        "这是回复正文\n\n"
        "[IMAGE_TAGS:img_abc12345]\n标签\n[/IMAGE_TAGS]\n"
        "[IMAGE_OCR:img_abc12345]\nHello World\n[/IMAGE_OCR]"
    )

    cleaned = _strip_all(text)
    assert "IMAGE_OCR" not in cleaned
    assert "Hello World" not in cleaned
    assert "这是回复正文" in cleaned


def test_image_tags_placeholder_id_remap_to_real_image_id():
    """当 LLM 输出占位 image_id 时，应按顺序映射到真实 image_id，避免 fallback。"""
    multimodal_images = [
        {"image_id": "img_real1111"},
        {"image_id": "img_real2222"},
    ]
    parsed_tag_blocks = [
        ("img_xxxxxxxx", "猫咪, 室内, 沙发"),
        ("img_yyyyyyyy", "狗, 户外, 草地"),
    ]
    parsed_ocr_blocks = [
        ("img_xxxxxxxx", "Hello"),
        ("img_yyyyyyyy", "World"),
    ]

    tags_map, ocr_map = _remap_extracted_blocks_by_order(
        multimodal_images,
        parsed_tag_blocks,
        parsed_ocr_blocks,
    )

    assert tags_map["img_real1111"] == "猫咪, 室内, 沙发"
    assert tags_map["img_real2222"] == "狗, 户外, 草地"
    assert ocr_map["img_real1111"] == "Hello"
    assert ocr_map["img_real2222"] == "World"


def test_image_tags_keep_exact_match_without_wrong_remap():
    """当某张图已有精确 ID 命中时，不应被错误的占位块覆盖。"""
    multimodal_images = [
        {"image_id": "img_real1111"},
        {"image_id": "img_real2222"},
    ]
    parsed_tag_blocks = [
        ("img_real1111", "准确标签"),
        ("img_xxxxxxxx", "占位标签"),
    ]
    parsed_ocr_blocks = [
        ("img_real1111", "准确 OCR"),
        ("img_xxxxxxxx", "占位 OCR"),
    ]

    tags_map, ocr_map = _remap_extracted_blocks_by_order(
        multimodal_images,
        parsed_tag_blocks,
        parsed_ocr_blocks,
    )

    assert tags_map["img_real1111"] == "准确标签"
    assert ocr_map["img_real1111"] == "准确 OCR"
    assert tags_map["img_real2222"] == "占位标签"
    assert ocr_map["img_real2222"] == "占位 OCR"


# ---------------------------------------------------------------------------
# 5. view_media text_query parameter
# ---------------------------------------------------------------------------

def test_view_media_text_query_parameter():
    """view_media 应支持 text_query 参数搜索 OCR 文字"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    mock_store = MagicMock()
    mock_store.enabled = True
    mock_store.search_images.return_value = [
        {
            "image_id": "img_ocr_test",
            "file_path": "/tmp/screenshot.png",
            "tags": "截图, 代码, Python",
            "ocr_text": "def hello():\n    print('Hello World')",
            "timestamp": 1709856000.0,
            "user_id": "user123",
        }
    ]

    tm = ToolManager(mock_adapter, image_store=mock_store)
    result = tm.view_media(text_query="hello")

    # 验证 search_images 被正确调用
    mock_store.search_images.assert_called_once()
    call_kwargs = mock_store.search_images.call_args[1]
    assert call_kwargs["text_query"] == "hello"

    # 验证输出包含 OCR 文字
    assert "img_ocr_test" in result
    assert "OCR Text:" in result
    assert "hello()" in result
    assert "Found 1 image" in result


def test_view_media_text_query_and_keyword_combined():
    """view_media 应支持 text_query 与 keyword 同时使用"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    mock_store = MagicMock()
    mock_store.enabled = True
    mock_store.search_images.return_value = [
        {
            "image_id": "img_combined",
            "file_path": "/tmp/combined.png",
            "tags": "文档, 表格",
            "ocr_text": "销售报表 2024年",
            "timestamp": 1709856000.0,
            "user_id": "user123",
        }
    ]

    tm = ToolManager(mock_adapter, image_store=mock_store)
    result = tm.view_media(keyword="文档", text_query="销售报表")

    call_kwargs = mock_store.search_images.call_args[1]
    assert call_kwargs["keyword"] == "文档"
    assert call_kwargs["text_query"] == "销售报表"


def test_view_media_tool_schema_has_text_query():
    """view_media schema 应包含 text_query 参数"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    tm = ToolManager(mock_adapter, image_store=None)

    schemas = tm.get_tool_schemas()
    view_media_schema = next(s for s in schemas if s["name"] == "view_media")
    assert "text_query" in view_media_schema["parameters"]["properties"]


def test_view_media_tool_schema_has_question():
    """view_media schema 应包含 question 参数"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    tm = ToolManager(mock_adapter, image_store=None)

    schemas = tm.get_tool_schemas()
    view_media_schema = next(s for s in schemas if s["name"] == "view_media")
    assert "question" in view_media_schema["parameters"]["properties"]


def test_view_media_local_path_without_image_store(tmp_path):
    """没有 ImageStore 时，view_media 仍应支持通过 local_path 读取本地图片"""
    from unittest.mock import MagicMock
    from PIL import Image
    from brain.tools import ToolManager

    img_path = tmp_path / "local.png"
    Image.new("RGB", (16, 16), color=(0, 0, 255)).save(img_path)

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    tm = ToolManager(mock_adapter, image_store=None)

    result = tm.view_media(local_path=str(img_path), question="这张图是什么颜色？")

    assert "Found 1 image" in result
    assert "[Question] 这张图是什么颜色？" in result
    assert "MediaTag: [image:" in result
    assert str(img_path) in result


def test_view_media_question_forces_return_image():
    """question 非空时，即使 return_image=False 也应强制返回 MediaTag"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    mock_store = MagicMock()
    mock_store.enabled = True
    mock_store.search_images.return_value = [
        {
            "image_id": "img_question",
            "file_path": "/tmp/question.png",
            "tags": "截图, 按钮",
            "timestamp": 1709856000.0,
            "user_id": "user123",
        }
    ]

    tm = ToolManager(mock_adapter, image_store=mock_store)
    result = tm.view_media(keyword="按钮", question="按钮是否可见？", return_image=False)

    assert "[Question] 按钮是否可见？" in result
    assert "MediaTag: [image:" in result


# ---------------------------------------------------------------------------
# 6. ImageStore search_by_ocr_text (unit test with mock MongoDB)
# ---------------------------------------------------------------------------

def test_search_by_ocr_text_builds_regex_query():
    """search_by_ocr_text 应为每个关键词构建 $regex 条件"""
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    # Mock cursor
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = iter([
        {"image_id": "img_test1", "ocr_text": "Hello World", "tags": "test"}
    ])
    store.mongo_col.find.return_value = mock_cursor

    results = store.search_by_ocr_text("Hello World")

    # 验证 find 被调用并且查询包含 $and + $regex
    assert store.mongo_col.find.called
    query_arg = store.mongo_col.find.call_args[0][0]
    assert "$and" in query_arg
    # 应有两个 regex 条件 (Hello 和 World)
    assert len(query_arg["$and"]) == 2
    for cond in query_arg["$and"]:
        assert "ocr_text" in cond
        assert "$regex" in cond["ocr_text"]


def test_search_by_ocr_text_single_keyword():
    """单个关键词时不应使用 $and"""
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = iter([])
    store.mongo_col.find.return_value = mock_cursor

    store.search_by_ocr_text("购物清单")

    query_arg = store.mongo_col.find.call_args[0][0]
    # 单个关键词不用 $and
    assert "$and" not in query_arg
    assert "ocr_text" in query_arg
    assert "$regex" in query_arg["ocr_text"]


def test_search_by_ocr_text_with_user_id():
    """带 user_id 时应将其加入查询条件"""
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = iter([])
    store.mongo_col.find.return_value = mock_cursor

    store.search_by_ocr_text("Hello World", user_id="user123")

    query_arg = store.mongo_col.find.call_args[0][0]
    assert "$and" in query_arg
    # 应有 Hello, World 两个 regex + user_id 条件
    has_user_id = any("user_id" in cond for cond in query_arg["$and"])
    assert has_user_id


def test_search_by_time_range_allows_legacy_docs_missing_context_fields():
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = iter([])
    store.mongo_col.find.return_value = mock_cursor

    store.search_by_time_range(
        user_id="user123",
        platform="telegram",
        chat_id="chat-1",
        storage_id="user123",
    )

    query_arg = store.mongo_col.find.call_args[0][0]
    assert query_arg["user_id"] == "user123"
    assert "$and" in query_arg
    assert {"platform": "telegram"} in query_arg["$and"][0]["$or"]
    assert {"chat_id": "chat-1"} in query_arg["$and"][1]["$or"]
    assert {"storage_id": "user123"} in query_arg["$and"][2]["$or"]


def test_search_by_keyword_keeps_text_query_top_level_with_legacy_context_filters():
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = iter([])
    store.mongo_col.find.return_value = mock_cursor

    store.search_by_keyword(
        "猫",
        user_id="user123",
        platform="telegram",
        chat_id="chat-1",
        storage_id="user123",
    )

    query_arg = store.mongo_col.find.call_args[0][0]
    assert query_arg["$text"] == {"$search": "猫"}
    assert query_arg["user_id"] == "user123"
    assert "$and" in query_arg


def test_search_by_semantic_filters_legacy_payloads_in_memory():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = None
    store.qdrant = MagicMock()
    store.embed_client = MagicMock()
    store.embed_client.enabled = True
    store.embed_client.get_embedding.return_value = [0.1, 0.2]
    store.enabled = True

    store.qdrant.query_points.return_value = SimpleNamespace(points=[
        SimpleNamespace(payload={"image_id": "legacy", "user_id": "user123"}, score=0.9),
        SimpleNamespace(payload={"image_id": "wrong", "user_id": "user123", "chat_id": "other"}, score=0.9),
    ])

    results = store.search_by_semantic(
        "猫",
        user_id="user123",
        platform="telegram",
        chat_id="chat-1",
        storage_id="user123",
    )

    assert [item["image_id"] for item in results] == ["legacy"]


# ---------------------------------------------------------------------------
# 7. search_images unified entry supports text_query
# ---------------------------------------------------------------------------

def test_search_images_text_query_only():
    """search_images 仅有 text_query 时应调用 search_by_ocr_text"""
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = MagicMock()
    store.embed_client = MagicMock()
    store.embed_client.enabled = True
    store.enabled = True

    store.search_by_ocr_text = MagicMock(return_value=[{"image_id": "img_ocr1"}])

    results = store.search_images(text_query="测试文字")

    store.search_by_ocr_text.assert_called_once_with("测试文字", user_id="", limit=10)
    assert len(results) == 1
    assert results[0]["image_id"] == "img_ocr1"


def test_search_images_text_query_and_keyword():
    """text_query + keyword 同时使用时应合并结果"""
    from unittest.mock import MagicMock

    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = MagicMock()
    store.embed_client = MagicMock()
    store.embed_client.enabled = True
    store.enabled = True

    store.search_by_ocr_text = MagicMock(return_value=[
        {"image_id": "img_ocr1"},
    ])
    store.search_by_semantic = MagicMock(return_value=[
        {"image_id": "img_sem1"},
        {"image_id": "img_ocr1"},  # 重复项
    ])

    results = store.search_images(text_query="文字", keyword="标签")

    # 应该去重，OCR 结果优先
    assert len(results) == 2
    ids = [r["image_id"] for r in results]
    assert ids[0] == "img_ocr1"  # OCR 优先
    assert "img_sem1" in ids


# ---------------------------------------------------------------------------
# 8. Phase 3: 跨平台共享作用域 (memory_scope_id)
# ---------------------------------------------------------------------------

SCOPE = "relationship:owner:default"


def _bare_store():
    """构造一个绕过 __init__ 的 ImageStore，仅挂载所需依赖。"""
    from unittest.mock import MagicMock
    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.embed_client.enabled = True
    store.enabled = True
    return store


def test_scope_payload_builds_fields_and_derives_place():
    """_scope_payload：有 scope 时写字段并由 platform:chat_id 推导 place_scope_id。"""
    from memory.image_store import ImageStore

    fields = ImageStore._scope_payload(
        memory_scope_id=SCOPE,
        owner_id="owner:default",
        relationship_id=SCOPE,
        actor_display_name="张三",
        platform="telegram",
        chat_id="123",
    )
    assert fields["memory_scope_id"] == SCOPE
    assert fields["place_scope_id"] == "telegram:123"
    assert fields["owner_id"] == "owner:default"
    assert fields["actor_display_name"] == "张三"


def test_scope_payload_empty_without_scope():
    """_scope_payload：无 memory_scope_id 时返回空，保持旧记录形态。"""
    from memory.image_store import ImageStore

    assert ImageStore._scope_payload(platform="telegram", chat_id="123") == {}


def test_add_optional_context_filters_scope_mode_ignores_platform():
    """scope 模式：只按 memory_scope_id 过滤（旧记录放行），忽略 platform/chat_id。"""
    from memory.image_store import ImageStore

    query = ImageStore._add_optional_context_filters(
        {},
        platform="telegram",
        chat_id="chat-1",
        storage_id="storage-1",
        memory_scope_id=SCOPE,
    )
    assert "$and" in query
    # 只有一个过滤条件：memory_scope_id（带旧记录回退）
    assert len(query["$and"]) == 1
    or_clause = query["$and"][0]["$or"]
    assert {"memory_scope_id": SCOPE} in or_clause
    # 旧记录缺字段也放行
    assert {"memory_scope_id": {"$exists": False}} in or_clause


def test_payload_matches_context_scope_mode_allows_legacy_and_blocks_mismatch():
    """_payload_matches_context scope 模式：旧记录放行、同 scope 命中、异 scope 拦截。"""
    from memory.image_store import ImageStore

    # 旧记录（无 memory_scope_id）放行
    assert ImageStore._payload_matches_context({"image_id": "old"}, memory_scope_id=SCOPE)
    # 同 scope 命中
    assert ImageStore._payload_matches_context(
        {"memory_scope_id": SCOPE}, memory_scope_id=SCOPE
    )
    # 异 scope 拦截
    assert not ImageStore._payload_matches_context(
        {"memory_scope_id": "relationship:other"}, memory_scope_id=SCOPE
    )


def test_search_by_time_range_scope_mode_filters_by_scope():
    """search_by_time_range scope 模式：按 memory_scope_id 过滤，不带 platform/chat_id。"""
    store = _bare_store()
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = iter([])
    store.mongo_col.find.return_value = mock_cursor

    store.search_by_time_range(
        user_id="user123",
        platform="telegram",
        chat_id="chat-1",
        storage_id="user123",
        memory_scope_id=SCOPE,
    )

    query_arg = store.mongo_col.find.call_args[0][0]
    assert "$and" in query_arg
    # scope 模式只有一个 $or 过滤组，且是 memory_scope_id
    assert len(query_arg["$and"]) == 1
    assert {"memory_scope_id": SCOPE} in query_arg["$and"][0]["$or"]


def test_search_by_semantic_scope_mode_aggregates_cross_platform():
    """search_by_semantic scope 模式：跨平台聚合，旧记录放行，异 scope 排除。"""
    from types import SimpleNamespace

    store = _bare_store()
    store.mongo_col = None
    store.qdrant = MagicMock()
    store.embed_client.get_embedding.return_value = [0.1, 0.2]

    store.qdrant.query_points.return_value = SimpleNamespace(points=[
        SimpleNamespace(payload={"image_id": "tg", "memory_scope_id": SCOPE}, score=0.9),
        SimpleNamespace(payload={"image_id": "web", "memory_scope_id": SCOPE}, score=0.8),
        SimpleNamespace(payload={"image_id": "legacy"}, score=0.7),  # 旧记录放行
        SimpleNamespace(payload={"image_id": "other", "memory_scope_id": "relationship:other"}, score=0.6),
    ])

    results = store.search_by_semantic("猫", user_id="user123", memory_scope_id=SCOPE)
    ids = [item["image_id"] for item in results]
    assert ids == ["tg", "web", "legacy"]
    assert "other" not in ids


def test_search_images_threads_memory_scope_id():
    """search_images 应把 memory_scope_id 透传给底层检索方法。"""
    store = _bare_store()
    store.search_by_semantic = MagicMock(return_value=[{"image_id": "img1"}])
    store._enrich_with_mongo = MagicMock(side_effect=lambda r: r)

    store.search_images(keyword="猫", memory_scope_id=SCOPE)

    call_kwargs = store.search_by_semantic.call_args[1]
    assert call_kwargs["memory_scope_id"] == SCOPE


def test_save_image_stub_persists_scope_fields():
    """save_image_stub 应把 memory_scope_id/place_scope_id 写入 Mongo 文档。"""
    store = _bare_store()

    store.save_image_stub(
        image_id="img_stub1",
        file_path="/tmp/x.png",
        user_id="user123",
        chat_id="123",
        platform="telegram",
        storage_id="user123",
        memory_scope_id=SCOPE,
    )

    doc = store.mongo_col.replace_one.call_args[0][1]
    assert doc["memory_scope_id"] == SCOPE
    assert doc["place_scope_id"] == "telegram:123"


def test_update_image_tags_inherits_scope_from_stub():
    """update_image_tags 未显式传 scope 时，应继承占位记录里已有的 memory_scope_id。"""
    store = _bare_store()
    store.qdrant = None  # 不写向量
    # 模拟占位记录已带 scope
    store.mongo_col.find_one.return_value = {
        "image_id": "img1",
        "file_path": "/tmp/x.png",
        "user_id": "user123",
        "chat_id": "123",
        "storage_id": "user123",
        "platform": "telegram",
        "memory_scope_id": SCOPE,
        "place_scope_id": "telegram:123",
        "timestamp": 1700000000.0,
    }
    result = MagicMock()
    result.matched_count = 1
    store.mongo_col.update_one.return_value = result

    store.update_image_tags(image_id="img1", tags="猫, 室内")

    set_doc = store.mongo_col.update_one.call_args[0][1]["$set"]
    assert set_doc["memory_scope_id"] == SCOPE
    assert set_doc["place_scope_id"] == "telegram:123"
