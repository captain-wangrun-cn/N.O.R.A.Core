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
_SPLIT_MARKER_PATTERN = re.compile(r"\[(?:SPLIT|SPILIT)\]", re.IGNORECASE)


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


def test_split_marker_supports_spilit_typo():
    """应兼容常见拼写错误 [SPILIT]，避免整段不分发。"""
    buf = "第一段[SPILIT]第二段[SPLIT]第三段"
    ready, remaining, in_think = _extract_split_ready_parts(buf, False)

    assert ready == ["第一段", "第二段"]
    assert remaining == "第三段"
    assert in_think is False


def test_stream_split_does_not_leak_fragmented_think_content():
    """当 <think> 在流式输出中被拆碎时，分段发送不应泄漏思考内容。"""
    in_think = False

    # 第 1 批：只到 <think> 开头，且遇到 [SPLIT]
    buf = "给你结论：<think>先想一想[SPILIT]"
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


# ---------------------------------------------------------------------------
# 4. IMAGE_OCR pattern parsing
# ---------------------------------------------------------------------------

_IMAGE_OCR_PATTERN = re.compile(
    r'\[IMAGE_OCR:([^\]\s]+)\](.*?)\[/IMAGE_OCR\]',
    re.IGNORECASE | re.DOTALL,
)


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


# ---------------------------------------------------------------------------
# 5. view_image text_query parameter
# ---------------------------------------------------------------------------

def test_view_image_text_query_parameter():
    """view_image 应支持 text_query 参数搜索 OCR 文字"""
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
    result = tm.view_image(text_query="hello")

    # 验证 search_images 被正确调用
    mock_store.search_images.assert_called_once()
    call_kwargs = mock_store.search_images.call_args[1]
    assert call_kwargs["text_query"] == "hello"

    # 验证输出包含 OCR 文字
    assert "img_ocr_test" in result
    assert "OCR Text:" in result
    assert "hello()" in result
    assert "Found 1 image" in result


def test_view_image_text_query_and_keyword_combined():
    """view_image 应支持 text_query 与 keyword 同时使用"""
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
    result = tm.view_image(keyword="文档", text_query="销售报表")

    call_kwargs = mock_store.search_images.call_args[1]
    assert call_kwargs["keyword"] == "文档"
    assert call_kwargs["text_query"] == "销售报表"


def test_view_image_tool_schema_has_text_query():
    """view_image schema 应包含 text_query 参数"""
    from unittest.mock import MagicMock
    from brain.tools import ToolManager

    mock_adapter = MagicMock()
    mock_adapter.platform_features = MagicMock()
    tm = ToolManager(mock_adapter, image_store=None)

    schemas = tm.get_tool_schemas()
    view_image_schema = next(s for s in schemas if s["name"] == "view_image")
    assert "text_query" in view_image_schema["parameters"]["properties"]


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