import base64

from brain.multimodal import extract_image_payloads, extract_video_payloads
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
