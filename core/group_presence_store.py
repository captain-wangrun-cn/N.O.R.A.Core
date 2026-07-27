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
