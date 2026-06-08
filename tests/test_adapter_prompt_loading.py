from pathlib import Path

import brain.prompts as prompts


def test_load_adapter_prompt_includes_global_before_platform(tmp_path: Path, monkeypatch):
    adapters_dir = tmp_path / "adapters"
    telegram_dir = adapters_dir / "telegram"
    telegram_dir.mkdir(parents=True)
    global_prompt = adapters_dir / "PROMPT.md"
    telegram_prompt = telegram_dir / "PROMPT.md"
    global_prompt.write_text("通用规则", encoding="utf-8")
    telegram_prompt.write_text("Telegram 规则", encoding="utf-8")

    monkeypatch.setattr(prompts, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(prompts, "ADAPTER_GLOBAL_PROMPT_FILE", str(global_prompt))

    block = prompts.load_adapter_prompt("telegram")

    assert "通用规则" in block
    assert "Telegram 规则" in block
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
