import cli


def test_step_models_does_not_load_config_file_during_first_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    state = {
        "provider": "gemini",
        "providers": {
            "gemini": {
                "type": "gemini",
                "api_key": "dummy-key",
            }
        },
        "cost_tracking": {
            "custom_prices": {
                "picked-model": {"input": 1.0, "output": 2.0},
            }
        },
        "model_providers": {},
        "models": {},
    }

    monkeypatch.setattr(cli.StepModels, "_select_provider_for_role", lambda self, role, entries: "gemini")
    monkeypatch.setattr(cli.StepModels, "_fetch_models_for_provider", lambda self, name, cfg: ["picked-model"])
    monkeypatch.setattr(cli.StepModels, "select_model", lambda self, models, role, provider: "picked-model")
    monkeypatch.setattr(cli.questionary, "confirm", lambda *args, **kwargs: _Confirm(False))

    step = cli.StepModels(state)

    assert step.run() is True
    assert state["model_prices"] == {"picked-model": {"input": 1.0, "output": 2.0}}
    assert not (tmp_path / "config.yml").exists()


def test_model_price_info_does_not_load_config_file_during_first_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    price_info = cli.get_model_price_info(
        "gemini",
        "picked-model",
        custom_prices={"picked-model": {"input": 1.0, "output": 2.0}},
    )

    assert price_info == " 💰 $1.00/$2.00 per M"
    assert not (tmp_path / "config.yml").exists()


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


class _Confirm:
    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        return self.answer
