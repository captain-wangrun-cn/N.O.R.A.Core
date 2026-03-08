import base64

from brain.multimodal import extract_image_payloads
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
