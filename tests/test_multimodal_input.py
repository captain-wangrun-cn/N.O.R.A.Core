import base64
import io

from PIL import Image

from brain.multimodal import extract_image_payloads, extract_video_payloads, _convert_gif_to_png
from brain.providers.openai import OpenAIProvider


def test_extract_image_payloads_loads_local_file(tmp_path):
    img_path = tmp_path / "sample.png"
    # 写入最小 PNG 头 + 假数据（用于路径/编码流程测试）
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test-bytes")

    text = f"[image: {img_path}]\n请分析这张图"
    clean_text, images = extract_image_payloads(text)

    assert "[image:" not in clean_text
    assert "请分析这张图" in clean_text
    assert len(images) == 1
    assert images[0]["path"].endswith("sample.png")
    assert images[0]["mime_type"] == "image/png"
    assert isinstance(images[0]["bytes"], (bytes, bytearray))
    assert images[0]["base64"] == base64.b64encode(images[0]["bytes"]).decode("utf-8")


def test_extract_sticker_payloads_marks_webp_as_sticker(tmp_path):
    img_path = tmp_path / "sticker.webp"
    image = Image.new("RGB", (8, 8), color="blue")
    image.save(img_path, format="WEBP")

    clean_text, images = extract_image_payloads(
        f"[sticker: 🙂 from pack]\n[sticker: {img_path}]\n看看这个表情"
    )

    assert "[sticker:" not in clean_text
    assert "看看这个表情" in clean_text
    assert len(images) == 1
    assert images[0]["path"].endswith("sticker.webp")
    assert images[0]["mime_type"] == "image/webp"
    assert images[0]["is_sticker"] is True
    assert images[0]["base64"] == base64.b64encode(images[0]["bytes"]).decode("utf-8")


def test_extract_sticker_payloads_skips_webm(tmp_path):
    sticker_path = tmp_path / "animated.webm"
    sticker_path.write_bytes(b"\x1aE\xdf\xa3webm")

    clean_text, images = extract_image_payloads(f"[sticker: {sticker_path}]")

    assert clean_text == ""
    assert images == []


def test_openai_build_user_content_with_images():
    payload = [{
        "mime_type": "image/png",
        "base64": base64.b64encode(b"abc").decode("utf-8"),
    }]

    content = OpenAIProvider._build_user_content("看图说话", payload)

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "看图说话"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_video_payloads_sniffs_mp4_when_extension_is_bin(tmp_path):
    video_path = tmp_path / "video_payload.bin"
    video_bytes = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
    video_path.write_bytes(video_bytes)

    clean_text, videos = extract_video_payloads(f"[video: {video_path}]\n看看这个视频")

    assert "[video:" not in clean_text
    assert "看看这个视频" in clean_text
    assert len(videos) == 1
    assert videos[0]["path"].endswith("video_payload.bin")
    assert videos[0]["mime_type"] == "video/mp4"
    assert videos[0]["bytes"] == video_bytes
    assert videos[0]["base64"] == base64.b64encode(video_bytes).decode("utf-8")


def _make_gif_bytes(size: tuple = (10, 10), frames: int = 1) -> bytes:
    """用 Pillow 生成最小合法 GIF 二进制。"""
    img = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    if frames > 1:
        frames_list = [
            Image.new("RGB", size, color=("red" if i % 2 == 0 else "blue"))
            for i in range(frames)
        ]
        frames_list[0].save(
            buf, format="GIF", save_all=True,
            append_images=frames_list[1:], duration=100, loop=0,
        )
    else:
        img.save(buf, format="GIF")
    return buf.getvalue()


def test_convert_gif_to_png_basic():
    """单帧 GIF 转 PNG，验证输出是合法 PNG。"""
    gif_bytes = _make_gif_bytes()
    png_bytes = _convert_gif_to_png(gif_bytes)
    assert png_bytes is not None
    # PNG 文件头
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    # 可被 Pillow 重新打开
    img = Image.open(io.BytesIO(png_bytes))
    assert img.format == "PNG"


def test_convert_gif_to_png_animated():
    """多帧动图 GIF 转 PNG，应只提取第一帧。"""
    gif_bytes = _make_gif_bytes(frames=3)
    png_bytes = _convert_gif_to_png(gif_bytes)
    assert png_bytes is not None
    img = Image.open(io.BytesIO(png_bytes))
    assert img.format == "PNG"
    assert img.size == (10, 10)


def test_extract_image_payloads_converts_gif(tmp_path):
    """extract_image_payloads 对 GIF 文件自动转为 PNG。"""
    gif_path = tmp_path / "test.gif"
    gif_path.write_bytes(_make_gif_bytes())

    text = f"[image: {gif_path}]"
    clean_text, images = extract_image_payloads(text)

    assert len(images) == 1
    assert images[0]["mime_type"] == "image/png"
    # base64 解码后应是 PNG 文件头
    decoded = base64.b64decode(images[0]["base64"])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
    # bytes 字段也应是 PNG
    assert images[0]["bytes"][:8] == b"\x89PNG\r\n\x1a\n"
