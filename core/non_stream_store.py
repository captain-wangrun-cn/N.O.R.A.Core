"""per-runtime_key 输出模式（流式 / 非流式）持久化。

`/set_stream` 过去只写 controller 内存里的 `non_stream_flags`，进程重启即丢：
用户开了非流式、重启后又悄悄变回流式，而且不报任何错，只能靠"输出怎么又变回去了"
察觉。这里把它落成 JSON。

键用 runtime_key（`<platform>:<chat_id>`）而不是 memory_scope_id：非流式描述的是
"这段回复怎么送到这个端点"，同一个人在不同平台的投递约束本来就不同，不该跨平台共享。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

_STATE_FILE = "non_stream_state.json"
_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.RLock()


def _state_path() -> str:
    data_dir = get_workspace_manager().data_dir
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, _STATE_FILE)


def _read_state() -> Dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        return {"version": _SCHEMA_VERSION, "flags": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle) or {}
        flags = data.get("flags") if isinstance(data, dict) else None
        if not isinstance(flags, dict):
            flags = {}
        return {"version": _SCHEMA_VERSION, "flags": flags}
    except Exception as exc:
        logger.warning("读取输出模式设置失败，按默认处理: %s", exc)
        return {"version": _SCHEMA_VERSION, "flags": {}}


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
        logger.warning("保存输出模式设置失败: %s", exc)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


def load_non_stream_flags() -> Dict[str, bool]:
    """读取所有已持久化的 per-runtime_key 输出模式。读不到时返回空 dict。"""
    flags = _read_state().get("flags") or {}
    result: Dict[str, bool] = {}
    for runtime_key, value in flags.items():
        if isinstance(value, bool):
            result[str(runtime_key)] = value
    return result


def get_non_stream_flag(runtime_key: str) -> Optional[bool]:
    """取单个设置。返回 None 表示"没设置过"，区别于 False（明确设成流式）。"""
    value = (_read_state().get("flags") or {}).get(str(runtime_key))
    return value if isinstance(value, bool) else None


def set_non_stream_flag(runtime_key: str, enabled: bool) -> bool:
    """写入单个设置。写盘失败只记日志并返回 False，不抛——

    调用方是命令链路，不该因为写盘失败就把整个 /set_stream 打断（内存里的开关
    已经生效，当次会话照常可用，只是这次设置不跨重启）。
    """
    enabled = bool(enabled)
    with _WRITE_LOCK:
        state = _read_state()
        flags = dict(state.get("flags") or {})
        flags[str(runtime_key)] = enabled
        return _atomic_write({"version": _SCHEMA_VERSION, "flags": flags})
