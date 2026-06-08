import cli


def test_main_auto_runs_wizard_and_continues_when_config_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "argv", ["cli.py", "status"])

    events = []

    def fake_run_wizard():
        events.append("wizard")
        (tmp_path / "config.yml").write_text("llm: {}\n", encoding="utf-8")
        return True

    def fake_show_status():
        events.append("status")

    monkeypatch.setattr(cli, "run_wizard", fake_run_wizard)
    monkeypatch.setattr(cli, "show_status", fake_show_status)

    cli.main()

    assert events == ["wizard", "status"]
    assert (tmp_path / "config.yml").exists()

    output = capsys.readouterr().out
    assert "未检测到配置文件 config.yml" in output
    assert "自动创建并保存该文件" in output


def test_main_stops_when_wizard_cancelled_on_missing_config(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "argv", ["cli.py", "status"])

    events = []

    def fake_run_wizard():
        events.append("wizard")
        return False

    def fake_show_status():
        events.append("status")

    monkeypatch.setattr(cli, "run_wizard", fake_run_wizard)
    monkeypatch.setattr(cli, "show_status", fake_show_status)

    cli.main()

    assert events == ["wizard"]
    assert not (tmp_path / "config.yml").exists()

    output = capsys.readouterr().out
    assert "配置未完成，当前操作已取消" in output
