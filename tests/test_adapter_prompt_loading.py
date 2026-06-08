from pathlib import Path
import json

import brain.prompts as prompts


def test_load_adapter_prompt_includes_global_before_platform(tmp_path: Path, monkeypatch):
    adapters_dir = tmp_path / "adapters"
    telegram_dir = adapters_dir / "telegram"
    telegram_dir.mkdir(parents=True)
    global_prompt = adapters_dir / "PROMPT.md"
    telegram_prompt = telegram_dir / "PROMPT.md"
    metadata_file = telegram_dir / "metadata.json"
    global_prompt.write_text("通用规则", encoding="utf-8")
    telegram_prompt.write_text("Telegram 规则", encoding="utf-8")
    metadata_file.write_text(
        json.dumps(
            {
                "platform": {
                    "name": "Telegram",
                    "description": "Telegram 是一个即时通讯平台。",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(prompts, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(prompts, "ADAPTER_GLOBAL_PROMPT_FILE", str(global_prompt))

    block = prompts.load_adapter_prompt("telegram")

    assert "通用规则" in block
    assert "平台名称: Telegram" in block
    assert "平台描述: Telegram 是一个即时通讯平台。" in block
    assert "Telegram 规则" in block
    assert block.index("通用规则") < block.index("平台名称: Telegram")
    assert block.index("平台名称: Telegram") < block.index("Telegram 规则")
    assert block.index("通用规则") < block.index("Telegram 规则")


def test_load_adapter_prompt_falls_back_to_global_only(tmp_path: Path, monkeypatch):
    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    global_prompt = adapters_dir / "PROMPT.md"
    global_prompt.write_text("通用规则", encoding="utf-8")

    monkeypatch.setattr(prompts, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(prompts, "ADAPTER_GLOBAL_PROMPT_FILE", str(global_prompt))

    block = prompts.load_adapter_prompt("missing")

    assert "通用规则" in block
    assert "missing 平台专属协议" not in block


def test_adapter_platform_metadata_prompt_block_uses_name_and_description(
    tmp_path: Path, monkeypatch
):
    telegram_dir = tmp_path / "adapters" / "telegram"
    telegram_dir.mkdir(parents=True)
    (telegram_dir / "metadata.json").write_text(
        json.dumps(
            {
                "description": "这是 adapter 职责说明，不应该注入到平台信息块。",
                "platform": {
                    "name": "Telegram",
                    "description": "Telegram 是一个跨平台即时通讯服务。",
                    "notes": ["群聊中仅在 @ 或 reply 时处理。"],
                    "privacy_notes": ["群聊是公开场景。"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(prompts, "PROJECT_ROOT", str(tmp_path))

    block = prompts.get_adapter_platform_metadata_prompt_block("telegram")

    assert "平台名称: Telegram" in block
    assert "平台描述: Telegram 是一个跨平台即时通讯服务。" in block
    assert "adapter 职责说明" not in block
    assert "群聊中仅在 @ 或 reply 时处理" not in block
    assert "群聊是公开场景" not in block
