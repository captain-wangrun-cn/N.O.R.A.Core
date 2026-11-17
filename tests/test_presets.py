"""预设资产与持久化，全部使用临时工作区。"""

from dataclasses import FrozenInstanceError

import pytest

import config
from core import preset_manager as pm


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_active_preset_name", lambda: "none")
    return pm.PresetManager(tmp_path)


def _write(manager, filename="sample.yml", text="name: 示例\nhead_prompt: 保持简洁\n"):
    (manager.directory / filename).write_text(text, encoding="utf-8")
    manager.reload_presets()


def test_empty_directory_is_pure(manager):
    assert manager.directory.is_dir()
    assert list(manager.directory.iterdir()) == []
    assert manager.get_active_preset() is None
    assert manager.list_presets() == []


def test_parse_and_persist_roundtrip(manager):
    _write(manager, text="""name: 示例
description: 风格
scopes: [forebrain]
head_prompt: 简洁
depth_injection:
  depth: 2
  role: user
  content: 保持角色
parameters:
  temperature: 0
  top_p: 0.95
""")
    assert manager.set_active_preset("sample.yml")
    preset = manager.get_active_preset()
    assert preset.filename == "sample"
    assert preset.scopes == ("forebrain",)
    assert preset.depth_injection == pm.DepthInjection(2, "user", "保持角色")
    assert dict(preset.parameters) == {"temperature": 0.0, "top_p": 0.95}
    assert manager.list_presets()[0].active
    restored = pm.PresetManager(manager.directory.parent)
    assert restored.get_active_preset() == preset
    with pytest.raises(FrozenInstanceError):
        preset.head_prompt = "changed"
    with pytest.raises(TypeError):
        preset.parameters["temperature"] = 1


def test_marker_priority_and_off_survives_restart(manager, monkeypatch):
    _write(manager)
    monkeypatch.setattr(pm, "get_active_preset_name", lambda: "sample")
    manager.reload_presets()
    assert manager.get_active_preset() is not None
    assert manager.clear_active_preset()
    assert pm.PresetManager(manager.directory.parent).get_active_preset() is None
    (manager.directory / ".active").write_text("../sample", encoding="utf-8")
    manager.reload_presets()
    assert manager.get_active_preset() is None


def test_cli_activation_is_visible_to_running_manager(manager):
    _write(manager)
    cli_manager = pm.PresetManager(manager.directory.parent)
    assert cli_manager.set_active_preset("sample")
    assert manager.get_active_preset().filename == "sample"
    assert cli_manager.clear_active_preset()
    assert manager.get_active_preset() is None


def test_unknown_name_preserves_current(manager):
    _write(manager)
    assert manager.set_active_preset("sample")
    for name in ["missing", "../sample", "x/sample", "x\\sample", ".active", "C:sample", "sample\nother"]:
        assert not manager.set_active_preset(name)
        assert manager.active_name == "sample"
    assert manager.set_active_preset(" sample.yaml ")


def test_hot_reload_and_deleted_active(manager):
    _write(manager)
    manager.set_active_preset("sample")
    path = manager.directory / "sample.yml"
    path.write_text("head_prompt: 新内容\n", encoding="utf-8")
    assert manager.get_active_preset().head_prompt == "保持简洁"
    manager.reload_presets()
    assert manager.get_active_preset().head_prompt == "新内容"
    path.unlink()
    manager.reload_presets()
    assert manager.active_name == "sample"
    assert manager.get_active_preset() is None
    _write(manager)
    assert manager.get_active_preset() is not None


@pytest.mark.parametrize("text", [
    "- invalid", "name: [invalid]", "enabled: true", "scopes: forebrain", "scopes: [summary]",
    "head_prompt: 123", "depth_injection: invalid", "depth_injection: {depth: -1}",
    "depth_injection: {depth: true}", "depth_injection: {depth: 1.5}",
    "depth_injection: {role: assistant}", "depth_injection: {content: []}",
    "parameters: []", "parameters: {temperature: .nan}", "parameters: {temperature: .inf}",
    "parameters: {temperature: true}", "parameters: {temperature: 3}",
    "parameters: {top_p: -0.1}", "parameters: {top_p: 1.1}",
    "parameters: {unexpected: 1}", "bad: [",
])
def test_invalid_files_are_skipped(manager, text):
    _write(manager, "bad.yaml", text)
    assert manager.list_presets() == []
    assert not manager.set_active_preset("bad")


def test_duplicate_extensions_are_ambiguous(manager):
    _write(manager)
    _write(manager, "sample.yaml")
    assert manager.list_presets() == []
    assert not manager.set_active_preset("sample")


def test_write_failure_does_not_activate(manager, monkeypatch):
    _write(manager)
    def fail(*args):
        raise OSError("test failure")
    monkeypatch.setattr(pm.os, "replace", fail)
    assert not manager.set_active_preset("sample")
    assert manager.get_active_preset() is None
    assert not list(manager.directory.glob(".active-*"))


@pytest.mark.parametrize("value, expected", [({}, "none"), ({"preset": None}, "none"),
    ({"preset": []}, "none"), ({"preset": {"active": " example "}}, "example"),
    ({"preset": {"active": 123}}, "none"), ({"preset": {"active": ""}}, "none")])
def test_config_helper(monkeypatch, value, expected):
    monkeypatch.setattr(config, "_config", value)
    assert config.get_active_preset_name() == expected
