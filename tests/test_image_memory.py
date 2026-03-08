"""
Tests for image memory system:
- Image ID generation in multimodal.py
- IMAGE_TAGS parsing from LLM response
- view_image tool output formatting
"""

import os
import re
import tempfile

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


def _strip_thinking_and_tags(text: str) -> str:
    """模拟 NoraController._strip_thinking_content 的逻辑"""
    cleaned = _THINK_BLOCK_PATTERN.sub("", text)
    cleaned = _THINK_INLINE_PATTERN.sub("", cleaned)
    cleaned = _IMAGE_TAGS_PATTERN.sub("", cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


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


# ---------------------------------------------------------------------------
# 3. view_image tool schema generation
# ---------------------------------------------------------------------------

def test_view_image_tool_schema_registered():
    """view_image 应出现在 ToolManager 的工具 schema 列表中"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    
    tm = ToolManager(mock_adapter, image_store=None)
    schema_names = [s["name"] for s in tm.get_tool_schemas()]
    assert "view_image" in schema_names
    assert "crop_image_for_llm" in schema_names


def test_view_image_tool_no_image_store():
    """没有 ImageStore 时 view_image 应返回错误"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    
    tm = ToolManager(mock_adapter, image_store=None)
    result = tm.view_image(keyword="猫")
    assert "Error" in result or "not available" in result


def test_view_image_tool_disabled_image_store():
    """ImageStore.enabled=False 时 view_image 应返回错误"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    mock_store = MagicMock()
    mock_store.enabled = False
    
    tm = ToolManager(mock_adapter, image_store=mock_store)
    result = tm.view_image(keyword="猫")
    assert "offline" in result.lower() or "Error" in result


def test_view_image_format_output():
    """view_image 应正确格式化搜索结果"""
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
    result = tm.view_image(keyword="猫")
    
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
