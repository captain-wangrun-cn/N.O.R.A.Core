import json

import main


def test_auto_selects_onebot_when_telegram_is_placeholder(tmp_path, monkeypatch):
    adapters_dir = tmp_path / "adapters"
    telegram_dir = adapters_dir / "telegram"
    onebot_dir = adapters_dir / "onebotv11"
    telegram_dir.mkdir(parents=True)
    onebot_dir.mkdir(parents=True)
    (telegram_dir / "config.json").write_text(
        json.dumps({"bot_token": "YOUR_TELEGRAM_BOT_TOKEN"}),
        encoding="utf-8",
    )
    (onebot_dir / "config.json").write_text(
        json.dumps({"connection_type": "websocket", "websocket_url": "ws://127.0.0.1:6700/"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(main, "read_adapter_config", lambda name: json.loads((adapters_dir / name / "config.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(
        main,
        "load_adapter_config",
        lambda name, defaults=None, create_if_missing=True: {
            **(defaults or {}),
            **json.loads((adapters_dir / name / "config.json").read_text(encoding="utf-8")),
        },
    )

    assert main.resolve_adapter_name("auto") == "onebotv11"


def test_auto_selects_telegram_when_only_telegram_is_configured(tmp_path, monkeypatch):
    adapters_dir = tmp_path / "adapters"
    telegram_dir = adapters_dir / "telegram"
    onebot_dir = adapters_dir / "onebotv11"
    telegram_dir.mkdir(parents=True)
    onebot_dir.mkdir(parents=True)
    (telegram_dir / "config.json").write_text(
        json.dumps({"bot_token": "123456:ABC"}),
        encoding="utf-8",
    )
    (onebot_dir / "config.json").write_text(
        json.dumps({"connection_type": "websocket", "websocket_url": ""}),
        encoding="utf-8",
    )

    monkeypatch.setattr(main, "read_adapter_config", lambda name: json.loads((adapters_dir / name / "config.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(
        main,
        "load_adapter_config",
        lambda name, defaults=None, create_if_missing=True: {
            **(defaults or {}),
            **json.loads((adapters_dir / name / "config.json").read_text(encoding="utf-8")),
        },
    )

    assert main.resolve_adapter_name("auto") == "telegram"
