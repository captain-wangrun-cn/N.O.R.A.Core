from pathlib import Path

import brain.prompts as prompts


def test_get_lexicon_global_system_prompt_block_reads_file(tmp_path: Path, monkeypatch) -> None:
    prompt_file = tmp_path / "PROMPT.md"
    prompt_file.write_text("词库用于解释轻量词义", encoding="utf-8")

    monkeypatch.setattr(prompts, "LEXICON_GLOBAL_PROMPT_FILE", str(prompt_file))

    block = prompts.get_lexicon_global_system_prompt_block()

    assert "词库全局说明" in block
    assert "词库用于解释轻量词义" in block


def test_get_lexicon_global_system_prompt_block_empty_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(prompts, "LEXICON_GLOBAL_PROMPT_FILE", "D:/__not_exists__/PROMPT.md")

    block = prompts.get_lexicon_global_system_prompt_block()

    assert block == ""
