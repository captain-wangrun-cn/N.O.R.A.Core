import cli


class _Answer:
    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        return self.answer


class _EmptyConfig:
    """最小 config 模块替身，避免菜单测试读取真实 config.yml。"""

    CONFIG_FILE = "config.yml"

    @staticmethod
    def get_config():
        return {}

    @staticmethod
    def get_proxy_config():
        return {}

    @staticmethod
    def get_owner_config():
        return {}


def test_mask_api_key_redacts_sensitive_values():
    data = {
        "api_key": "sk-abcdefghij",
        "access_token": "tok12345",
        "bot_token": "123456:ABCDEF",
        "url": "http://localhost:8080",
        "timeout": 10,
        "nested": {"secret": "verysecretvalue", "host": "localhost"},
        "list": ["a", "b"],
    }
    masked = cli._mask_api_key(data)
    assert masked["url"] == "http://localhost:8080"
    assert masked["timeout"] == 10
    assert masked["nested"]["host"] == "localhost"
    assert masked["list"] == ["a", "b"]
    # 敏感键必须被掩码（原值不可见）
    assert "sk-abcdefghij" not in masked["api_key"]
    assert "tok12345" not in masked["access_token"]
    assert "123456:ABCDEF" not in masked["bot_token"]
    assert "verysecretvalue" not in masked["nested"]["secret"]
    # 保留首尾用于辨认
    assert masked["api_key"].startswith("sk-a")
    assert masked["api_key"].endswith("ij")


def test_mask_secret_short_value():
    assert cli._mask_secret("ab") == "**"
    assert cli._mask_secret("") == ""
    assert cli._mask_secret("abcdef") == "abcd**ef"


def test_section_nonempty():
    assert cli._section_nonempty({}) is False
    assert cli._section_nonempty({"a": ""}) is False
    assert cli._section_nonempty({"a": None}) is False
    assert cli._section_nonempty({"a": "x"}) is True
    assert cli._section_nonempty({"a": {"b": 0}}) is True
    assert cli._section_nonempty({"a": {}}) is False
    assert cli._section_nonempty(0) is False
    assert cli._section_nonempty(5) is True


def test_show_config_summary_marks_missing_and_masked(monkeypatch, capsys):
    """总览应标记缺失项、掩码敏感值，并输出预填摘要。"""
    cfg = {
        "workspace": {"root_path": "~/ws"},
        "network": {"proxy": {"enabled": True, "url": "http://127.0.0.1:7890"}},
        "llm": {
            "provider": "gemini_main",
            "providers": {
                "gemini_main": {"type": "gemini", "api_key": "sk-A1B2C3D4E5F6"},
            },
            "models": {"smart": "gemini-2.5-pro", "video": "gemini-2.5-flash"},
            "model_providers": {"smart": "gemini_main"},
        },
        "memory": {
            "qdrant": {"host": "localhost", "port": 6333},
        },
        "tavily": {},
        "cost_tracking": {"enabled": True},
        "security": {"enabled": False},
        "schedule": {"enabled": True},
        "interaction": {"group_listener": {"enabled": True}},
    }

    class DummyConfigLoader:
        CONFIG_FILE = "config.yml"

        @staticmethod
        def get_config():
            return cfg

        @staticmethod
        def get_proxy_config():
            return {"enabled": True, "url": "http://127.0.0.1:7890"}

        @staticmethod
        def get_owner_config():
            return {}

    monkeypatch.setitem(cli.sys.modules, "config", DummyConfigLoader)

    # questionary.print 在非 Windows 控制台环境会崩（prompt_toolkit），
    # 改为收集到列表；普通 print 输出仍走 capsys。
    printed = []
    monkeypatch.setattr(cli.questionary, "print", lambda text, style="": printed.append(text))

    cli.show_config_summary()

    output = "".join(printed) + capsys.readouterr().out
    # 敏感 key 不出现在输出中
    assert "sk-A1B2C3D4E5F6" not in output
    # 预填摘要
    assert "gemini-2.5-pro" in output
    assert "📍 工作区" in output
    # 缺失项有提示
    assert "未配置" in output
    # 可选模型以“可选”标注
    assert "（可选，未配置）" in output


def test_config_menu_returns_via_menus(monkeypatch, tmp_path):
    """config_menu 循环：重跑向导 -> 返回（不崩溃）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("llm: {}\n", encoding="utf-8")
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)

    selections = iter(["✏️ 重新运行完整配置向导", "⬅️ 返回"])
    monkeypatch.setattr(cli.questionary, "select", lambda *args, **kwargs: _Answer(next(selections)))
    monkeypatch.setattr(cli.questionary, "confirm", lambda *args, **kwargs: _Answer(False))
    monkeypatch.setattr(cli.questionary, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "run_wizard", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")

    cli.config_menu()  # 不应抛异常


def test_config_menu_adapter_view(monkeypatch, tmp_path):
    """config_menu 中查看平台适配器配置，mask 私有 token。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("llm: {}\n", encoding="utf-8")
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)
    adapter_dir = tmp_path / "adapters" / "telegram"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "config.json").write_text(
        '{"bot_token": "123456:ABCDEF-SECRET-TOKEN", "chat_ids": ["1"]}',
        encoding="utf-8",
    )
    (adapter_dir / "config.example.json").write_text("{}", encoding="utf-8")

    selections = iter(["🔌 查看平台适配器配置", "⬅️ 返回"])
    monkeypatch.setattr(cli.questionary, "select", lambda *args, **kwargs: _Answer(next(selections)))
    monkeypatch.setattr(cli.questionary, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")

    cli.config_menu()  # 不应抛异常
