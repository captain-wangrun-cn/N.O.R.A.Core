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

_STATE_FILE = "private_presence_state.json"
_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.RLock()
# 超过这个时间（秒）的持久化记录在重启后视为过期，重置为 SEMI_ONLINE
_RESTART_EXPIRE_SECONDS = 6 * 3600  # 6 小时


class PrivatePresence(str, Enum):
    """Per-scope private chat presence state.

    - ONLINE: 该 scope 的私聊处于活跃对话中（用户正在聊天或后脑正在生成）
    - SEMI_ONLINE: 该 scope 的私聊空闲待命
    """
    ONLINE = "online"
    SEMI_ONLINE = "semi_online"


def _state_path() -> str:
    data_dir = get_workspace_manager().data_dir
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, _STATE_FILE)


def _read_state() -> Dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        return {"version": _SCHEMA_VERSION, "scopes": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
        scopes = data.get("scopes") if isinstance(data, dict) else None
        if not isinstance(scopes, dict):
            scopes = {}
        return {"version": _SCHEMA_VERSION, "scopes": scopes}
    except Exception as exc:
        logger.warning("读取私聊在线状态失败，按半在线处理: %s", exc)
        return {"version": _SCHEMA_VERSION, "scopes": {}}


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
        logger.warning("保存私聊在线状态失败: %s", exc)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


class PrivatePresenceStore:
    """Persist per-scope private-chat presence state.

    Keyed by ``memory_scope_id`` so that the same person chatting across
    multiple platforms (Telegram + Web + OneBot private) shares one state.
    """

    def __init__(self, default_mode: PrivatePresence = PrivatePresence.SEMI_ONLINE):
        self.default_mode = default_mode

    def get(self, memory_scope_id: str) -> PrivatePresence:
        record = (_read_state().get("scopes") or {}).get(str(memory_scope_id), {})
        raw_mode = record.get("mode") if isinstance(record, dict) else None
        try:
            return PrivatePresence(str(raw_mode))
        except (TypeError, ValueError):
            return self.default_mode

    def set(
        self,
        memory_scope_id: str,
        mode: PrivatePresence,
        *,
        reason: str = "",
    ) -> bool:
        mode = PrivatePresence(mode)
        old = self.get(memory_scope_id)
        if old == mode:
            return True
        with _WRITE_LOCK:
            state = _read_state()
            scopes = dict(state.get("scopes") or {})
            scopes[str(memory_scope_id)] = {
                "mode": mode.value,
                "reason": reason or "",
                "updated_at": time.time(),
            }
            state = {"version": _SCHEMA_VERSION, "scopes": scopes}
            written = _atomic_write(state)
        if written:
            logger.info(
                "[PRIVATE PRESENCE] scope=%s %s -> %s reason=%s",
                memory_scope_id,
                old.value,
                mode.value,
                reason or "unspecified",
            )
        return written

    def all(self) -> Dict[str, PrivatePresence]:
        result: Dict[str, PrivatePresence] = {}
        for scope_id, record in (_read_state().get("scopes") or {}).items():
            raw_mode = record.get("mode") if isinstance(record, dict) else None
            try:
                result[str(scope_id)] = PrivatePresence(str(raw_mode))
            except (TypeError, ValueError):
                result[str(scope_id)] = self.default_mode
        return result

    def expire_stale_entries(self) -> int:
        """Reset entries that were persisted more than _RESTART_EXPIRE_SECONDS ago.

        Called once at startup.  Returns the number of entries expired.
        """
        now = time.time()
        expired = 0
        with _WRITE_LOCK:
            state = _read_state()
            scopes = dict(state.get("scopes") or {})
            changed = False
            for scope_id, record in list(scopes.items()):
                if not isinstance(record, dict):
                    continue
                updated_at = float(record.get("updated_at", 0))
                if record.get("mode") == PrivatePresence.ONLINE.value:
                    if now - updated_at > _RESTART_EXPIRE_SECONDS:
                        scopes[scope_id] = {
                            "mode": PrivatePresence.SEMI_ONLINE.value,
                            "reason": "startup_expire",
                            "updated_at": now,
                        }
                        expired += 1
                        changed = True
            if changed:
                _atomic_write({"version": _SCHEMA_VERSION, "scopes": scopes})
        if expired:
            logger.info("启动过期清理: %d 个私聊 ONLINE 记录已重置为 SEMI_ONLINE", expired)
        return expired
