from __future__ import annotations

import json
import logging
import os
import threading
import time
from enum import Enum
from typing import Any, Dict

from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

_STATE_FILE = "group_presence_state.json"
_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.RLock()
# 超过这个时间（秒）未更新的 ONLINE 记录，在重启时视为过期并重置为 SEMI_ONLINE。
# 与私聊 PrivatePresenceStore 的语义对齐：进程重启前挂着的 ONLINE 不应无限继承。
_RESTART_EXPIRE_SECONDS = 6 * 3600  # 6 小时


class GroupPresence(str, Enum):
    ONLINE = "online"
    SEMI_ONLINE = "semi_online"


def _state_path() -> str:
    data_dir = get_workspace_manager().data_dir
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, _STATE_FILE)


def _read_state() -> Dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        return {"version": _SCHEMA_VERSION, "groups": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, dict):
            groups = {}
        return {"version": _SCHEMA_VERSION, "groups": groups}
    except Exception as exc:
        logger.warning("读取群聊在线状态失败，按半在线处理: %s", exc)
        return {"version": _SCHEMA_VERSION, "groups": {}}


def _atomic_write(data: Dict[str, Any]) -> bool:
    path = _state_path()
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return True
    except Exception as exc:
        logger.warning("保存群聊在线状态失败: %s", exc)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


class GroupPresenceStore:
    """Persist one active group listener mode; message windows remain runtime state."""

    def get(self, runtime_key: str) -> GroupPresence:
        record = (_read_state().get("groups") or {}).get(str(runtime_key), {})
        raw_mode = record.get("mode") if isinstance(record, dict) else None
        try:
            return GroupPresence(str(raw_mode))
        except (TypeError, ValueError):
            return GroupPresence.SEMI_ONLINE

    def set(
        self,
        runtime_key: str,
        mode: GroupPresence,
        *,
        platform: str = "",
        platform_chat_id: str = "",
    ) -> bool:
        mode = GroupPresence(mode)
        with _WRITE_LOCK:
            state = _read_state()
            groups = dict(state.get("groups") or {})
            now = time.time()
            if mode == GroupPresence.ONLINE:
                for other_key, record in list(groups.items()):
                    if str(other_key) == str(runtime_key) or not isinstance(record, dict):
                        continue
                    if record.get("mode") == GroupPresence.ONLINE.value:
                        groups[str(other_key)] = {
                            **record,
                            "mode": GroupPresence.SEMI_ONLINE.value,
                            "updated_at": now,
                        }
            groups[str(runtime_key)] = {
                "mode": mode.value,
                "platform": str(platform or ""),
                "platform_chat_id": str(platform_chat_id or ""),
                "updated_at": now,
            }
            state = {"version": _SCHEMA_VERSION, "groups": groups}
            return _atomic_write(state)

    def normalize_single_online(self) -> list[str]:
        """Keep only the most recently updated ONLINE group in legacy state."""
        with _WRITE_LOCK:
            state = _read_state()
            groups = dict(state.get("groups") or {})
            online_records = []
            for runtime_key, record in groups.items():
                if not isinstance(record, dict) or record.get("mode") != GroupPresence.ONLINE.value:
                    continue
                try:
                    updated_at = float(record.get("updated_at") or 0)
                except (TypeError, ValueError):
                    updated_at = 0.0
                online_records.append((updated_at, str(runtime_key)))
            if len(online_records) <= 1:
                return []

            online_records.sort(key=lambda item: (item[0], item[1]), reverse=True)
            demoted = [runtime_key for _, runtime_key in online_records[1:]]
            now = time.time()
            for runtime_key in demoted:
                record = groups.get(runtime_key)
                if isinstance(record, dict):
                    groups[runtime_key] = {
                        **record,
                        "mode": GroupPresence.SEMI_ONLINE.value,
                        "updated_at": now,
                    }
            if not _atomic_write({"version": _SCHEMA_VERSION, "groups": groups}):
                return []
            return demoted

    def all(self) -> Dict[str, GroupPresence]:
        result: Dict[str, GroupPresence] = {}
        for runtime_key, record in (_read_state().get("groups") or {}).items():
            raw_mode = record.get("mode") if isinstance(record, dict) else None
            try:
                result[str(runtime_key)] = GroupPresence(str(raw_mode))
            except (TypeError, ValueError):
                result[str(runtime_key)] = GroupPresence.SEMI_ONLINE
        return result

    def updated_at(self, runtime_key: str) -> float:
        """Return when this group's mode was last written (0.0 when unknown)."""
        record = (_read_state().get("groups") or {}).get(str(runtime_key), {})
        if not isinstance(record, dict):
            return 0.0
        try:
            return float(record.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def expire_stale_entries(self) -> list[str]:
        """Reset ONLINE records not touched for _RESTART_EXPIRE_SECONDS.

        群 ONLINE 记录此前没有任何时间过期，一个几天前进入 ONLINE 的群在重启后
        依然 ONLINE，两个 adapter 就会继续对该群整群放行（绕过 @/回复门）。
        启动时调用一次，返回被降级的 runtime_key 列表。
        """
        now = time.time()
        demoted: list[str] = []
        with _WRITE_LOCK:
            state = _read_state()
            groups = dict(state.get("groups") or {})
            for runtime_key, record in list(groups.items()):
                if not isinstance(record, dict):
                    continue
                if record.get("mode") != GroupPresence.ONLINE.value:
                    continue
                try:
                    updated_at = float(record.get("updated_at") or 0.0)
                except (TypeError, ValueError):
                    updated_at = 0.0
                if now - updated_at <= _RESTART_EXPIRE_SECONDS:
                    continue
                groups[str(runtime_key)] = {
                    **record,
                    "mode": GroupPresence.SEMI_ONLINE.value,
                    "updated_at": now,
                }
                demoted.append(str(runtime_key))
            if not demoted:
                return []
            if not _atomic_write({"version": _SCHEMA_VERSION, "groups": groups}):
                return []
        logger.info(
            "启动过期清理: %d 个群 ONLINE 记录已重置为 SEMI_ONLINE (%s)",
            len(demoted),
            ", ".join(demoted),
        )
        return demoted
