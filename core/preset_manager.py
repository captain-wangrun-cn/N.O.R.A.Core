"""工作区预设：全局激活、显式重载，注入内容只在请求内存中存在。"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from config import get_active_preset_name
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)
PRESET_SCOPES = frozenset({"forebrain", "hindbrain"})


@dataclass(frozen=True)
class DepthInjection:
    depth: int = 1
    role: str = "system"
    content: str = ""


@dataclass(frozen=True)
class PresetData:
    filename: str
    name: str
    description: str = ""
    scopes: tuple[str, ...] = ("forebrain", "hindbrain")
    head_prompt: str = ""
    depth_injection: DepthInjection | None = None
    parameters: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class PresetSummary:
    filename: str
    name: str
    description: str
    active: bool


def _normalize_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("预设文件名必须是字符串")
    name = value.strip()
    if not name or name.lower() == "none":
        return "none"
    if name.startswith(".") or any(char in name for char in '/\\:\x00\n\r'):
        raise ValueError("预设文件名不能包含路径或控制字符")
    if name.lower().endswith((".yml", ".yaml")):
        name = name.rsplit(".", 1)[0]
    if not name or name.lower() == "none":
        raise ValueError("预设文件名不能使用保留名称 none")
    return name


def _text(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    return value.strip()


def _parse_preset(filename: str, data: object) -> PresetData:
    if not isinstance(data, dict):
        raise ValueError("预设必须是 YAML 对象")
    if "enabled" in data:
        raise ValueError("预设不支持 enabled；请通过 /preset use 或 off 管理")
    scopes = data.get("scopes", ["forebrain", "hindbrain"])
    if not isinstance(scopes, list) or any(not isinstance(s, str) or s not in PRESET_SCOPES for s in scopes):
        raise ValueError("scopes 只能包含 forebrain / hindbrain")
    injection = None
    raw = data.get("depth_injection")
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("depth_injection 必须是对象")
        depth = raw.get("depth", 1)
        role = raw.get("role", "system")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError("depth 必须是非负整数")
        if role not in ("system", "user"):
            raise ValueError("注入 role 只能是 system / user")
        injection = DepthInjection(depth, role, _text(raw, "content"))
    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict) or parameters.keys() - {"temperature", "top_p"}:
        raise ValueError("parameters 只支持 temperature / top_p")
    normalized = {}
    for key, value in parameters.items():
        upper = 2 if key == "temperature" else 1
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= upper:
            raise ValueError(f"{key} 必须是 0 到 {upper} 之间的有限数字")
        normalized[key] = float(value)
    return PresetData(
        filename=filename,
        name=_text(data, "name", filename) or filename,
        description=_text(data, "description"),
        scopes=tuple(dict.fromkeys(scopes)),
        head_prompt=_text(data, "head_prompt"),
        depth_injection=injection,
        parameters=MappingProxyType(normalized),
    )


class PresetManager:
    """一个工作区共用一个预设；重载或切换失败不假报成功。"""

    def __init__(self, workspace_path: str | Path | None = None):
        root = Path(workspace_path) if workspace_path is not None else Path(get_workspace_manager().root)
        self.directory = root.expanduser().resolve() / "presets"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._presets: dict[str, PresetData] = {}
        self._active_name = "none"
        self._reload_token = None
        self.reload_presets(notify=False)

    def _read_reload_token(self) -> str | None:
        marker = self.directory / ".reload"
        try:
            return marker.read_text(encoding="utf-8") if marker.is_file() and marker.resolve().parent == self.directory else None
        except OSError:
            return None

    def _refresh_if_requested(self) -> None:
        if self._read_reload_token() != self._reload_token:
            self.reload_presets(notify=False)

    def _read_active_name(self) -> str:
        marker = self.directory / ".active"
        try:
            if marker.exists() and marker.resolve().parent != self.directory.resolve():
                raise ValueError("激活标记不能链接到目录外")
            raw_name = marker.read_text(encoding="utf-8-sig") if marker.exists() else get_active_preset_name()
            return _normalize_name(raw_name)
        except (OSError, ValueError) as exc:
            logger.warning("读取全局预设状态失败，停用注入: %s", exc)
            return "none"

    @property
    def active_name(self) -> str:
        with self._lock:
            # CLI 是另一进程：每次请求读轻量标记，不能只依赖进程内缓存。
            self._active_name = self._read_active_name()
            return self._active_name

    def reload_presets(self, *, notify: bool = True) -> None:
        with self._lock:
            presets = {}
            duplicates = set()
            for path in sorted(self.directory.iterdir()):
                if path.suffix.lower() not in (".yml", ".yaml") or not path.is_file():
                    continue
                try:
                    if path.resolve().parent != self.directory.resolve():
                        raise ValueError("预设不能链接到目录外")
                    name = _normalize_name(path.name)
                    if name in presets or name in duplicates:
                        presets.pop(name, None)
                        duplicates.add(name)
                        raise ValueError("存在同名 yml/yaml 预设，已忽略全部同名项")
                    with path.open(encoding="utf-8-sig") as handle:
                        presets[name] = _parse_preset(name, yaml.safe_load(handle))
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    logger.warning("预设 %s 加载失败: %s", path.name, exc)
            self._presets = presets
            self._active_name = self._read_active_name()
            self._reload_token = self._read_reload_token()
            if notify:
                token = uuid.uuid4().hex
                if not self._write_marker(".reload", token):
                    raise OSError("无法通知其它进程重载预设")
                self._reload_token = token + "\n"
            if self._active_name != "none" and self._active_name not in presets:
                logger.warning("激活的预设 %s 不存在或无效，暂不注入", self._active_name)

    def get_active_preset(self) -> PresetData | None:
        with self._lock:
            self._refresh_if_requested()
            return self._presets.get(self.active_name)

    def list_presets(self) -> list[PresetSummary]:
        with self._lock:
            self._refresh_if_requested()
            active_name = self.active_name
            return [PresetSummary(p.filename, p.name, p.description, p.filename == active_name)
                    for p in self._presets.values()]

    def set_active_preset(self, filename: str) -> bool:
        try:
            name = _normalize_name(filename)
        except ValueError:
            return False
        with self._lock:
            self._refresh_if_requested()
            if name != "none" and name not in self._presets:
                return False
            if not self._write_marker(".active", name):
                return False
            self._active_name = name
            return True

    def _write_marker(self, filename: str, value: str) -> bool:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=f"{filename}-", delete=False) as handle:
                temp_path = handle.name
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.directory / filename)
            return True
        except OSError as exc:
            logger.warning("保存全局预设状态失败: %s", exc)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return False

    def clear_active_preset(self) -> bool:
        return self.set_active_preset("none")


_manager: PresetManager | None = None
_MANAGER_LOCK = threading.RLock()


def get_preset_manager() -> PresetManager:
    """惰性获取单例，切换工作区时不沿用旧目录的预设。"""
    global _manager
    root = Path(get_workspace_manager().root).resolve()
    with _MANAGER_LOCK:
        if _manager is None or _manager.directory.parent != root:
            _manager = PresetManager(root)
        return _manager
