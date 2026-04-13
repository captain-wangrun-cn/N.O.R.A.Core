from pathlib import Path

from lexicon.manager import LexiconManager


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_always_terms_loaded_on_init(tmp_path: Path) -> None:
    _write(
        tmp_path / "always" / "common.dict",
        "YYDS: 永远的神\n# 注释\n\n摸鱼: 工作时开小差\n",
    )

    manager = LexiconManager(base_dir=tmp_path)

    assert manager.get("YYDS") == "永远的神"
    assert manager.get("摸鱼") == "工作时开小差"
    assert manager.stats()["lazy_files_loaded"] == 0


def test_lazy_terms_loaded_on_demand(tmp_path: Path) -> None:
    _write(tmp_path / "always" / "common.dict", "YYDS: 永远的神\n")
    _write(tmp_path / "lazy" / "rare.dict", "夏普比率: 风险收益指标\n")

    manager = LexiconManager(base_dir=tmp_path)
    before = manager.stats()

    assert before["lazy_files_total"] == 1
    assert before["lazy_files_loaded"] == 0

    assert manager.get("夏普比率") == "风险收益指标"

    after = manager.stats()
    assert after["lazy_files_loaded"] == 1
    assert after["lazy_hit_count"] == 1


def test_invalid_lines_are_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path / "always" / "bad.dict",
        "无分隔符\n: 没有词\n有词: \n合法词: 合法释义\n",
    )

    manager = LexiconManager(base_dir=tmp_path)

    assert manager.get("合法词") == "合法释义"
    assert manager.get("无分隔符") is None


def test_search_respects_limit(tmp_path: Path) -> None:
    _write(
        tmp_path / "always" / "search.dict",
        "摆烂: 放弃努力\n红温: 情绪激动\n拿捏: 准确掌握\n",
    )

    manager = LexiconManager(base_dir=tmp_path)

    results = manager.search("", limit=10)
    assert results == []

    results = manager.search("", limit=0)
    assert results == []

    results = manager.search("", limit=-1)
    assert results == []

    # 含义命中 + term 命中
    results = manager.search("情绪", limit=1)
    assert len(results) == 1
    assert results[0][0] == "红温"


def test_duplicate_term_last_write_wins(tmp_path: Path) -> None:
    _write(tmp_path / "always" / "a.dict", "YYDS: 第一版\n")
    _write(tmp_path / "always" / "z.dict", "YYDS: 覆盖版\n")

    manager = LexiconManager(base_dir=tmp_path)

    assert manager.get("YYDS") == "覆盖版"


def test_lazy_manifest_is_built_from_lazy_keys(tmp_path: Path) -> None:
    _write(tmp_path / "always" / "common.dict", "YYDS: 永远的神\n")
    _write(tmp_path / "lazy" / "game.dict", "红温: 情绪激动\n开荒: 首次尝试\n")

    manager = LexiconManager(base_dir=tmp_path)

    assert "红温" in manager.lazy_manifest
    assert "开荒" in manager.lazy_manifest
    assert manager.stats()["lazy_manifest_terms"] == 2


def test_supplement_meanings_for_text_matches_lazy_terms(tmp_path: Path) -> None:
    _write(tmp_path / "always" / "common.dict", "YYDS: 永远的神\n")
    _write(tmp_path / "lazy" / "game.dict", "红温: 情绪激动\n开荒: 首次尝试\n")

    manager = LexiconManager(base_dir=tmp_path)
    supplements = manager.supplement_meanings_for_text("YYDS，这把我红温了，准备继续开荒", limit=5)

    supplement_map = dict(supplements)
    assert supplement_map["红温"] == "情绪激动"
    assert supplement_map["开荒"] == "首次尝试"
    assert "YYDS" not in supplement_map

    stats = manager.stats()
    assert stats["lazy_files_loaded"] == 1
    assert stats["lazy_hit_count"] == 2
