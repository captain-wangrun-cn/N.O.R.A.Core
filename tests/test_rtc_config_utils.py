"""rtc/config_utils 测试：static_token 首启随机生成 + 最大通话时长字段。"""

import json

import pytest

from rtc import config_utils


class TestStaticToken:
    def test_generates_when_empty(self, tmp_path, monkeypatch):
        """static_token 为空：随机生成、写回 config.json、二次启动沿用。"""
        monkeypatch.setattr(config_utils, "CONFIG_PATH", tmp_path / "config.json")
        cfg = config_utils.load_config()
        assert cfg["static_token"] == ""

        config_utils.ensure_static_token(cfg)
        token = cfg["static_token"]
        assert token.startswith("rtc-") and len(token) > len("rtc-")

        # 写回了磁盘
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["static_token"] == token

        # 再次启动：load 出已写入的 token，ensure 不覆盖
        cfg2 = config_utils.load_config()
        config_utils.ensure_static_token(cfg2)
        assert cfg2["static_token"] == token

    def test_keeps_existing(self):
        """已有 token 原样保留（且不写盘）。"""
        cfg = {"static_token": "my-token"}
        config_utils.ensure_static_token(cfg)
        assert cfg["static_token"] == "my-token"

    def test_generated_tokens_unique(self):
        a = config_utils._generate_static_token()
        b = config_utils._generate_static_token()
        assert a != b


class TestMaxCallSeconds:
    def test_new_field(self):
        assert config_utils.get_max_call_seconds(
            {"max_call_duration_seconds": 600}) == 600

    def test_legacy_field_compat(self):
        """旧字段 max_call_seconds 兼容（历史部署的 config.json）。"""
        assert config_utils.get_max_call_seconds({"max_call_seconds": 900}) == 900
        # 新字段优先
        assert config_utils.get_max_call_seconds(
            {"max_call_seconds": 900, "max_call_duration_seconds": 100}) == 100

    def test_default(self):
        assert config_utils.get_max_call_seconds({}) == 1800
        assert config_utils.get_max_call_seconds({"max_call_duration_seconds": "x"}) == 1800

    def test_load_migrates_legacy_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_utils, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text(
            json.dumps({"max_call_seconds": 777}), encoding="utf-8")
        cfg = config_utils.load_config()
        assert cfg["max_call_duration_seconds"] == 777

    def test_no_daily_budget_in_defaults(self):
        """daily_budget_usd 已移除（多余字段）。"""
        assert "daily_budget_usd" not in config_utils.DEFAULTS
