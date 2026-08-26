'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-27 00:00:00
Copyright © WR（captain-wangrun-cn）All rights reserved

TTS provider 目录配置读取 — 仿 adapters/config_utils.py 的私有配置约定。

tts/ 目录同时是子系统和 provider 容器（与 adapters/ 同构）：每个 provider
一个文件夹 tts/<name>/，连接参数放各自目录的 config.json（gitignore），
提供 config.example.json 作模板。全局 config.yml 的 tts: 块只负责选哪个
provider，不放任何 key / endpoint。
'''
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger(__name__)

# tts/ 目录本身（provider 容器）
TTS_ROOT = Path(__file__).resolve().parent


def provider_dir(name: str) -> Path | None:
    """按名字找 provider 文件夹：tts/<name>/ 且其中有 provider.py。

    名字里不允许路径分隔符与隐藏目录前缀，防止目录穿越。
    """
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = TTS_ROOT / name
    if (candidate / "provider.py").is_file():
        return candidate
    return None


def list_provider_names() -> list[str]:
    """列出 tts/ 下所有合法 provider 文件夹名（供 CLI 选择与日志）。"""
    names: list[str] = []
    if not TTS_ROOT.is_dir():
        return names
    for item in sorted(TTS_ROOT.iterdir()):
        if not item.is_dir():
            continue
        if item.name.startswith(("_", ".")) or item.name == "__pycache__":
            continue
        if (item / "provider.py").is_file():
            names.append(item.name)
    return names


def provider_config_path(pdir: Path) -> Path:
    return pdir / "config.json"


def provider_config_example_path(pdir: Path) -> Path:
    return pdir / "config.example.json"


def read_provider_config(pdir: Path) -> dict[str, Any] | None:
    """读 <pdir>/config.json；不存在返回 None；坏 JSON 抛 ValueError。"""
    path = provider_config_path(pdir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def load_provider_config(pdir: Path, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """加载 provider 配置：config.json 覆盖 config.example.json 覆盖 defaults。

    中间留 example 合并这一层——example 里除了占位值还常常带说明性默认值
    （latency/format 等），缺 config.json 时按 example 起步比按空配置起步更能跑。
    """
    merged: dict[str, Any] = dict(defaults or {})
    example = provider_config_example_path(pdir)
    if example.exists():
        try:
            example_data = json.loads(example.read_text(encoding="utf-8"))
            if isinstance(example_data, dict):
                merged.update(example_data)
        except (json.JSONDecodeError, OSError):
            logger.warning("读取 %s 失败，忽略该层配置。", example)

    data = read_provider_config(pdir)
    if data is not None:
        merged.update(data)
    return merged
