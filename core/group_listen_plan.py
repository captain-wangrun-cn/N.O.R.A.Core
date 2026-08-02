"""core/group_listen_plan.py — 群聊定时 ONLINE 监听计划（Nora 可自行维护的 JSON）。

与私聊主动消息的对应关系：

- 私聊：``SCHEDULE.md``（作息，人类维护）→ 凌晨 00:00 生成 ``cache/daily_plan.json``
  → 到点主动发一条消息。
- 群聊：``workspace/GROUP_LISTEN.json`` 里的 ``groups`` / ``preferences``（Nora 自己维护）
  → 凌晨 00:00 在同一个文件里生成当日 ``entries``（时间 + 平台 + 群号）
  → 到点把该群切到 ONLINE 开始监听（只是开始听，不主动发言）。

设计要点：

1. 单文件：候选群池、偏好、当日计划、已触发标记都在同一个 JSON 里，
   Nora 可以直接用文件工具读写维护。
2. 一个时间点只触发一个群：按 ``HH:MM`` 去重，且每次 tick 最多激活一个群。
3. 触发时若该群已经是 ONLINE，直接跳过并标记 fired，不做轮询重试
   （与私聊 proactive 在目标在线时的处理一致）。
4. 由每分钟 tick 轮询文件驱动，而不是预注册 DateTrigger —— 这样 Nora
   中途改文件可以立刻生效，不需要额外的 reload 步骤。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

PLAN_FILE_NAME = "GROUP_LISTEN.json"
_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.RLock()
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_README = (
    "Nora 的群聊定时监听计划。groups / preferences 由 Nora 自己维护；"
    "entries 每天 00:00 依据它们自动生成，也可以手工增删。"
    "一个 time 只能对应一个群；到点时该群若已是 ONLINE 则跳过。"
)

def plan_file_path() -> str:
    """计划文件绝对路径（workspace 根目录下，Nora 用文件工具即可访问）。"""
    root = get_workspace_manager().root
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, PLAN_FILE_NAME)


def _default_state() -> Dict[str, Any]:
    return {
        "version": _SCHEMA_VERSION,
        "readme": _README,
        "enabled": True,
        "preferences": {
            "max_entries_per_day": 3,
            "earliest_time": "09:00",
            "latest_time": "23:30",
            "notes": "",
        },
        "groups": [],
        "generated_date": "",
        "entries": [],
    }


def normalize_time(value: Any) -> str:
    """把时间规范成 ``HH:MM``；不合法返回空串。"""
    raw = str(value or "").strip()
    match = _TIME_RE.match(raw)
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def read_plan() -> Dict[str, Any]:
    """读取计划文件；缺失/损坏时返回默认结构（不写盘）。"""
    path = plan_file_path()
    if not os.path.isfile(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("读取群聊监听计划失败，按空计划处理: %s", exc)
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()

    state = _default_state()
    state["enabled"] = bool(data.get("enabled", True))
    if isinstance(data.get("preferences"), dict):
        state["preferences"].update(data["preferences"])
    state["groups"] = [g for g in (data.get("groups") or []) if isinstance(g, dict)]
    state["generated_date"] = str(data.get("generated_date") or "")
    state["entries"] = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
    if data.get("readme"):
        state["readme"] = str(data["readme"])
    return state


def write_plan(state: Dict[str, Any]) -> bool:
    """原子写回计划文件。"""
    path = plan_file_path()
    temp_path = f"{path}.tmp"
    payload = dict(state)
    payload["version"] = _SCHEMA_VERSION
    payload.setdefault("readme", _README)
    with _WRITE_LOCK:
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return True
        except Exception as exc:
            logger.warning("保存群聊监听计划失败: %s", exc)
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return False


def ensure_plan_file() -> str:
    """确保计划文件存在（首次启动时生成骨架），返回路径。"""
    path = plan_file_path()
    if not os.path.isfile(path):
        write_plan(_default_state())
        logger.info("已创建群聊定时监听计划文件: %s", path)
    return path


def candidate_groups(state: Dict[str, Any]) -> List[Dict[str, str]]:
    """候选群池：过滤出 platform + group_id 齐全且未被禁用的项。"""
    out: List[Dict[str, str]] = []
    seen = set()
    for item in state.get("groups") or []:
        platform = str(item.get("platform") or "").strip()
        group_id = str(item.get("group_id") or item.get("chat_id") or "").strip()
        if not platform or not group_id:
            continue
        if item.get("enabled") is False:
            continue
        key = f"{platform}:{group_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "platform": platform,
            "group_id": group_id,
            "name": str(item.get("name") or "").strip(),
            "note": str(item.get("note") or item.get("reason") or "").strip(),
        })
    return out


def normalize_entries(
    raw_entries: List[Dict[str, Any]],
    *,
    known_groups: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """规范化 entries：校验时间/平台/群号，按时间去重（一个时间点只留一个群）。"""
    known_lookup: Dict[str, Dict[str, str]] = {}
    for group in known_groups or []:
        known_lookup[f"{group['platform']}:{group['group_id']}"] = group

    normalized: List[Dict[str, Any]] = []
    used_times = set()
    for item in raw_entries or []:
        if not isinstance(item, dict):
            continue
        time_str = normalize_time(item.get("time"))
        if not time_str or time_str in used_times:
            continue
        platform = str(item.get("platform") or "").strip()
        group_id = str(item.get("group_id") or item.get("chat_id") or "").strip()
        if not platform or not group_id:
            continue
        used_times.add(time_str)
        known = known_lookup.get(f"{platform}:{group_id}", {})
        normalized.append({
            "time": time_str,
            "platform": platform,
            "group_id": group_id,
            "name": str(item.get("name") or known.get("name") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "fired": bool(item.get("fired", False)),
            "result": str(item.get("result") or "").strip(),
        })
    normalized.sort(key=lambda entry: entry["time"])
    return normalized


def runtime_key_for(entry: Dict[str, Any]) -> str:
    platform = str(entry.get("platform") or "").strip()
    group_id = str(entry.get("group_id") or entry.get("chat_id") or "").strip()
    if not platform or not group_id:
        return ""
    return f"{platform}:{group_id}"


def due_entry(
    state: Dict[str, Any],
    now: datetime,
    *,
    grace_minutes: int = 10,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """找出当前应触发的条目（最多一个）。

    返回 ``(index, entry)``；无可触发条目时返回 ``(-1, None)``。
    条目在 ``time`` 到达后的 ``grace_minutes`` 分钟内仍可补触发（覆盖 tick 抖动/短暂重启），
    超过则视为过期，由调用方标记 fired 但不激活。
    """
    if not state.get("enabled", True):
        return -1, None
    today = now.strftime("%Y-%m-%d")
    if state.get("generated_date") and state["generated_date"] != today:
        return -1, None

    now_minutes = now.hour * 60 + now.minute
    best_index = -1
    best_minutes = -1
    for index, entry in enumerate(state.get("entries") or []):
        if entry.get("fired"):
            continue
        time_str = normalize_time(entry.get("time"))
        if not time_str:
            continue
        hour, minute = time_str.split(":")
        entry_minutes = int(hour) * 60 + int(minute)
        if entry_minutes > now_minutes:
            continue
        if now_minutes - entry_minutes > max(0, int(grace_minutes)):
            continue
        # 同时有多个到点条目时取最晚的那个（更贴近当下）
        if entry_minutes > best_minutes:
            best_minutes = entry_minutes
            best_index = index
    if best_index < 0:
        return -1, None
    return best_index, dict(state["entries"][best_index])


def mark_entry(index: int, *, result: str) -> bool:
    """把某条目标记为已触发，并写入结果。就地重读文件避免覆盖 Nora 的手工编辑。"""
    with _WRITE_LOCK:
        state = read_plan()
        entries = state.get("entries") or []
        if not (0 <= index < len(entries)):
            return False
        entries[index] = {**entries[index], "fired": True, "result": str(result or "")}
        state["entries"] = entries
        return write_plan(state)


def expire_stale_entries(now: datetime, *, grace_minutes: int = 10) -> int:
    """把早已过点却仍未触发的条目标记为 missed，避免它们一直卡在待触发状态。"""
    with _WRITE_LOCK:
        state = read_plan()
        today = now.strftime("%Y-%m-%d")
        if state.get("generated_date") and state["generated_date"] != today:
            return 0
        now_minutes = now.hour * 60 + now.minute
        entries = state.get("entries") or []
        changed = 0
        for index, entry in enumerate(entries):
            if entry.get("fired"):
                continue
            time_str = normalize_time(entry.get("time"))
            if not time_str:
                continue
            hour, minute = time_str.split(":")
            entry_minutes = int(hour) * 60 + int(minute)
            if now_minutes - entry_minutes <= max(0, int(grace_minutes)):
                continue
            entries[index] = {**entry, "fired": True, "result": "missed"}
            changed += 1
        if not changed:
            return 0
        state["entries"] = entries
        write_plan(state)
        return changed


def save_generated_entries(entries: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """写入当日生成的 entries（保留 groups / preferences 等 Nora 维护的字段）。"""
    with _WRITE_LOCK:
        state = read_plan()
        state["entries"] = normalize_entries(entries, known_groups=candidate_groups(state))
        state["generated_date"] = now.strftime("%Y-%m-%d")
        write_plan(state)
        return state

