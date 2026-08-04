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

    @staticmethod
    def save_config(data):
        import yaml

        with open("config.yml", "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


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

    selections = iter(["🔄 重新运行完整配置向导", "⬅️ 返回"])
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


def test_edit_single_item_only_changes_that_item(monkeypatch, tmp_path):
    """单独修改工作区：只改该分区，其他配置原样保留。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text(
        "workspace:\n  root_path: /old\nllm:\n  provider: gemini\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)

    # 选择「📍 工作区路径」
    monkeypatch.setattr(
        cli.questionary, "select",
        lambda *args, **kwargs: _Answer(("workspace", cli.StepWorkspace)),
    )

    def _set_workspace(self):
        self.state["workspace"] = {"root_path": "/new"}
        return True

    monkeypatch.setattr(cli.StepWorkspace, "run", _set_workspace)
    monkeypatch.setattr(cli.questionary, "print", lambda *args, **kwargs: None)

    cli._edit_single_item()

    import yaml

    with open(tmp_path / "config.yml", "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)
    # 工作区已修改
    assert saved["workspace"]["root_path"] == "/new"
    # 其他配置原样保留
    assert saved["llm"]["provider"] == "gemini"
    # 单项编辑不留备份文件
    assert not (tmp_path / "config.yml.wizard-backup").exists()


def test_edit_single_item_no_config_file(monkeypatch, tmp_path):
    """无配置文件时单项编辑给出提示，不崩溃。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)

    monkeypatch.setattr(
        cli.questionary, "select",
        lambda *args, **kwargs: _Answer(("workspace", cli.StepWorkspace)),
    )
    messages = []
    monkeypatch.setattr(cli.questionary, "print", lambda text, style="": messages.append(text))

    cli._edit_single_item()
    assert any("未找到配置文件" in msg for msg in messages)


# --- 向导兜底：每步检查点保存 ---


def test_build_final_config_roundtrip():
    """_build_final_config 应与最终保存使用同一份组装逻辑。"""
    state = {
        "workspace": {"root_path": "~/ws"},
        "provider": "gemini",
        "providers": {"gemini": {"type": "gemini", "api_key": "k1"}},
        "model_providers": {"smart": "gemini"},
        "models": {"smart": "gemini-2.5-pro"},
        "memory": {"qdrant": {"host": "localhost", "port": 6333}},
        "cost_tracking": {"enabled": True},
        "security": {},
        "tavily": {"api_key": "tv1"},
        "model_prices": {"gemini-2.5-pro": {"input": 1.0, "output": 2.0}},
    }
    cfg = cli._build_final_config(state)
    assert cfg["workspace"]["root_path"] == "~/ws"
    assert cfg["llm"]["providers"]["gemini"]["api_key"] == "k1"
    assert cfg["llm"]["models"]["smart"] == "gemini-2.5-pro"
    assert cfg["memory"]["qdrant"]["port"] == 6333
    assert cfg["tavily"]["api_key"] == "tv1"
    # 价格写入 cost_tracking.custom_prices
    assert cfg["cost_tracking"]["custom_prices"]["gemini-2.5-pro"] == {"input": 1.0, "output": 2.0}
    # 兼容旧结构：legacy api_keys 从默认 provider 回填
    assert cfg["llm"]["api_keys"]["gemini"] == "k1"


def test_save_wizard_checkpoint_merges_into_existing(monkeypatch, tmp_path):
    """检查点保存应增量合并：保留原配置中向导未涉及的分区。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text(
        "workspace:\n  root_path: /old\nllm:\n  provider: gemini\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)

    state = {
        "workspace": {"root_path": "/new"},
        "provider": "gemini",
        "providers": {"gemini": {"type": "gemini", "api_key": "k1"}},
        "model_providers": {},
        "models": {},
        "memory": {},
        "cost_tracking": {"enabled": True},
        "security": {},
    }
    cli._save_wizard_checkpoint(state)

    import yaml

    with open(tmp_path / "config.yml", "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)
    assert saved["workspace"]["root_path"] == "/new"
    assert saved["llm"]["providers"]["gemini"]["api_key"] == "k1"


def test_run_wizard_checkpoint_saves_after_each_step(monkeypatch, tmp_path):
    """向导每完成一步都立即落盘；步骤中途失败时，已保存进度不丢失。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text(
        "llm:\n  provider: old\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)

    save_calls = []
    original_checkpoint = cli._save_wizard_checkpoint
    monkeypatch.setattr(
        cli, "_save_wizard_checkpoint",
        lambda state: (save_calls.append(dict(state)), original_checkpoint(state))[1],
    )

    steps = [cli.StepWorkspace, cli.StepProxy, cli.StepProvider, cli.StepAPIKeys]
    monkeypatch.setattr(cli, "WIZARD_STEPS", steps)
    monkeypatch.setattr(cli.questionary, "print", lambda *args, **kwargs: None)
    # 第 3 步（StepProvider）失败

    def _set_workspace(self):
        self.state["workspace"] = {"root_path": "~/.nora/workspace"}
        return True

    monkeypatch.setattr(cli.StepWorkspace, "run", _set_workspace)
    monkeypatch.setattr(cli.StepProxy, "run", lambda self: True)
    monkeypatch.setattr(cli.StepProvider, "run", lambda self: False)
    monkeypatch.setattr(cli.StepAPIKeys, "run", lambda self: True)

    result = cli.run_wizard()

    assert result is False
    # 第 1、2 步成功完成后各保存一次
    assert len(save_calls) == 2
    # 已保存的进度仍留在 config.yml（不因失败回滚丢失）
    import yaml

    with open(tmp_path / "config.yml", "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)
    assert saved["workspace"]["root_path"] == "~/.nora/workspace"


def test_run_wizard_success_cleans_backup(monkeypatch, tmp_path):
    """向导全部完成后确认保存：删除备份文件。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("llm: {}\n", encoding="utf-8")
    monkeypatch.setitem(cli.sys.modules, "config", _EmptyConfig)
    monkeypatch.setattr(cli.questionary, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.questionary, "confirm", lambda *args, **kwargs: _Answer(True))
    monkeypatch.setattr(cli, "WIZARD_STEPS", [cli.StepWorkspace])

    def _set_workspace(self):
        self.state["workspace"] = {"root_path": "~/ws"}
        return True

    monkeypatch.setattr(cli.StepWorkspace, "run", _set_workspace)

    result = cli.run_wizard()
    assert result is True
    # 备份文件已清理
    assert not (tmp_path / "config.yml.wizard-backup").exists()
