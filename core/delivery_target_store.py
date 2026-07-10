"""最近活跃投递端 / 活跃平台·场景的本地持久化（跨平台接力 Phase 4 + 多平台路由）。

两条独立语义共用一个状态文件：

1. **主动投递端（仅私聊）**：proactive / trigger 默认投递到「用户最后活跃的私聊端」，
   不可用时回退 default_chat_id。**只记录私聊端**，落实「绝不自动发进群」的决策
   （见 plans/nora-cross-platform-relay.md 第二部分决策 2）。对应
   ``update_last_active_target`` / ``resolve_delivery_runtime_key``。

2. **活跃平台 + 活跃场景（含群聊）**：最新收到消息的平台/场景成为「活跃平台/活跃场景」，
   群聊也记录（但不自动推送）。用于 Nora 输出标记 ``[platform:X][scene:Y]`` 缺省时的兜底，
   以及跨平台重定向时补齐目标。对应 ``record_active_scene`` /
   ``load_active_platform`` / ``load_platform_scene`` / ``resolve_route_target``。

持久化文件：workspace/data/delivery_target_state.json
  {
    // —— 仅私聊：主动投递端（legacy 顶层键，保持兼容） ——
    "last_active_runtime_key": "telegram:123",
    "last_active_platform": "telegram",
    "last_active_platform_chat_id": "123",
    "last_active_at": 1717650000.0,
    "memory_scope_id": "relationship:owner:default",

    // —— 任意场景（含群）：活跃平台 + 活跃场景 ——
    "active": {"runtime_key": "onebotv11:-100", "platform": "onebotv11",
               "platform_chat_id": "-100", "chat_type": "group", "at": ...},
    // —— 每平台最近活跃场景 ——
    "platform_scenes": {
      "telegram":  {"runtime_key": "telegram:123", "platform_chat_id": "123",
                    "chat_type": "private", "at": ...},
      "onebotv11": {"runtime_key": "onebotv11:-100", "platform_chat_id": "-100",
                    "chat_type": "group", "at": ...}
    }
  }
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from core.conversation_identity import (
    PRIVATE_CHAT_TYPE,
    build_identity_from_target,
    normalize_chat_type,
)
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

_DELIVERY_TARGET_STATE_FILE = "delivery_target_state.json"
_MAX_KNOWN_SCENES = 100


def _get_state_file_path() -> str:
    ws = get_workspace_manager()
    state_dir = ws.data_dir
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, _DELIVERY_TARGET_STATE_FILE)


def _read_state() -> Dict[str, Any]:
    path = _get_state_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"读取活跃投递端状态失败: {e}")
        return {}


def _merge_state(updates: Dict[str, Any]) -> bool:
    """读取-合并-写回，避免不同语义互相覆盖。返回是否写入成功。"""
    state = _read_state()
    state.update(updates)
    path = _get_state_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"保存活跃投递端状态失败: {e}")
        return False


# ---------------------------------------------------------------------------
# 语义一：主动投递端（仅私聊）
# ---------------------------------------------------------------------------

def update_last_active_target(target: Dict[str, Any]) -> bool:
    """更新最近活跃投递端。**仅私聊端**会被记录；群聊一律忽略。

    返回是否实际写入（群聊或缺字段时返回 False，不改动私聊投递键）。
    """
    chat_type = normalize_chat_type(target.get("chat_type"))
    if chat_type != PRIVATE_CHAT_TYPE:
        # 群聊活跃不改变主动投递目标（仅私聊投递）。
        return False

    identity = build_identity_from_target(target)
    runtime_key = identity.runtime_key.strip()
    if not runtime_key:
        return False

    return _merge_state({
        "last_active_runtime_key": runtime_key,
        "last_active_platform": identity.platform,
        "last_active_platform_chat_id": identity.platform_chat_id,
        "last_active_at": time.time(),
        "memory_scope_id": identity.memory_scope_id,
    })


def load_last_active_target() -> Dict[str, Any]:
    """读取最近活跃投递端。无记录或读取失败返回 {}。"""
    return _read_state()


def load_last_active_runtime_key() -> str:
    """读取最近活跃私聊端的 runtime_key（无则空串）。"""
    target = load_last_active_target()
    return str(target.get("last_active_runtime_key") or "").strip()


def resolve_delivery_runtime_key(fallback: str = "") -> str:
    """解析主动消息投递目标：优先最近活跃私聊端，不可用则回退 fallback。"""
    return load_last_active_runtime_key() or str(fallback or "").strip()


# ---------------------------------------------------------------------------
# 语义二：活跃平台 + 活跃场景（含群聊）
# ---------------------------------------------------------------------------

def record_active_scene(target: Dict[str, Any]) -> bool:
    """记录最新收到消息的平台/场景为活跃平台/活跃场景（私聊与群聊都记）。

    同时：若为私聊，顺带更新主动投递端（语义一）。
    群聊只更新活跃场景，不改动私聊投递键（绝不自动发进群）。
    """
    identity = build_identity_from_target(target)
    runtime_key = identity.runtime_key.strip()
    if not runtime_key:
        return False

    now = time.time()
    scene = {
        "runtime_key": runtime_key,
        "platform": identity.platform,
        "platform_chat_id": identity.platform_chat_id,
        "chat_type": identity.chat_type,
        "at": now,
    }
    state = _read_state()
    platform_scenes = dict(state.get("platform_scenes") or {})
    platform_scenes[identity.platform] = scene
    known_scenes = dict(state.get("known_scenes") or {})
    known_scenes[runtime_key] = scene
    if len(known_scenes) > _MAX_KNOWN_SCENES:
        ordered = sorted(
            known_scenes.items(),
            key=lambda item: float(item[1].get("at", 0.0)) if isinstance(item[1], dict) else 0.0,
            reverse=True,
        )
        known_scenes = dict(ordered[:_MAX_KNOWN_SCENES])
    updates: Dict[str, Any] = {
        "active": scene,
        "platform_scenes": platform_scenes,
        "known_scenes": known_scenes,
    }
    # 私聊：顺带刷新主动投递端。
    if identity.chat_type == PRIVATE_CHAT_TYPE:
        updates.update({
            "last_active_runtime_key": runtime_key,
            "last_active_platform": identity.platform,
            "last_active_platform_chat_id": identity.platform_chat_id,
            "last_active_at": now,
            "memory_scope_id": identity.memory_scope_id,
        })
    return _merge_state(updates)


def load_active_target() -> Dict[str, Any]:
    """读取活跃场景（任意类型，含群）。无则 {}。"""
    state = _read_state()
    active = state.get("active")
    return dict(active) if isinstance(active, dict) else {}


def load_active_platform() -> str:
    """读取活跃平台（最新收到消息的平台）。无则空串。"""
    return str(load_active_target().get("platform") or "").strip()


def load_platform_scene(platform: str) -> Dict[str, Any]:
    """读取某平台最近活跃场景。无则 {}。"""
    platform = str(platform or "").strip()
    if not platform:
        return {}
    state = _read_state()
    scenes = state.get("platform_scenes") or {}
    scene = scenes.get(platform) if isinstance(scenes, dict) else None
    return dict(scene) if isinstance(scene, dict) else {}


def load_known_scenes() -> list[Dict[str, Any]]:
    """列出实际见过的场景；兼容旧版仅有 platform_scenes 的状态文件。"""
    state = _read_state()
    scenes = state.get("known_scenes")
    if not isinstance(scenes, dict) or not scenes:
        scenes = state.get("platform_scenes") or {}
    if not isinstance(scenes, dict):
        return []
    out = [
        dict(scene)
        for scene in scenes.values()
        if isinstance(scene, dict) and str(scene.get("platform_chat_id") or "").strip()
    ]
    return sorted(out, key=lambda scene: float(scene.get("at", 0.0)), reverse=True)


def _known_scene(platform: str, platform_chat_id: str) -> Dict[str, Any]:
    """查找已实际记录的平台场景。"""
    platform = str(platform or "").strip()
    platform_chat_id = str(platform_chat_id or "").strip()
    for scene in load_known_scenes():
        if (
            str(scene.get("platform") or "").strip() == platform
            and str(scene.get("platform_chat_id") or "").strip() == platform_chat_id
        ):
            return scene
    return {}


def resolve_route_target(
    route: Optional[Dict[str, Any]] = None,
    current_target: Optional[Dict[str, Any]] = None,
    chat_type_lookup: Optional[Callable[[str, str], str]] = None,
) -> Dict[str, Any]:
    """把 Nora 输出的 [platform:X][scene:type:id] 标记解析为具体投递目标。

    缺省规则（用户要求）：某个标记缺失则取「活跃的」——
      - 平台缺失 → 当前活跃平台（本轮入站平台，回退持久化活跃平台）。
      - 场景缺失 → 该平台的活跃场景（当前场景 / 该平台最近场景）。

    参数：
      route: {"platform": str|None, "scene_id": str|None, "scene_type": str|None}
      current_target: 本轮入站 identity 的 conversation_target_dict（就地回复兜底）。
      chat_type_lookup: (platform, platform_chat_id) -> chat_type，
                        用于解析显式 scene_id 的群/私聊（通常查 controller 身份缓存）。
    返回：{"runtime_key", "platform", "platform_chat_id", "chat_type", "redirected": bool}
    """
    route = route or {}
    current = current_target or {}

    cur_platform = str(current.get("platform") or "").strip()
    cur_chat_id = str(current.get("platform_chat_id") or current.get("chat_id") or "").strip()
    cur_chat_type = normalize_chat_type(current.get("chat_type"))

    route_platform = str(route.get("platform") or "").strip()
    route_scene_id = str(route.get("scene_id") or "").strip()
    route_scene_type = str(route.get("scene_type") or "").strip()

    # —— 平台 ——
    platform = route_platform or cur_platform or load_active_platform()

    # —— 场景 ——
    policy = "default_current"
    if route_scene_id:
        platform_chat_id = route_scene_id
        known_scene = _known_scene(platform, platform_chat_id)
        if route_scene_type:
            chat_type = normalize_chat_type(route_scene_type)
            policy = "applied"
        elif known_scene:
            chat_type = normalize_chat_type(known_scene.get("chat_type"))
            policy = "applied"
        else:
            return {
                "runtime_key": "",
                "platform": platform,
                "platform_chat_id": platform_chat_id,
                "chat_type": "",
                "redirected": False,
                "policy": "rejected_unknown_scene",
            }
    else:
        # 无任何标记始终就地回复；仅显式换平台时才使用该平台活跃场景。
        if not route_platform and cur_chat_id:
            platform, platform_chat_id, chat_type = cur_platform, cur_chat_id, cur_chat_type
        elif platform == cur_platform and cur_chat_id:
            platform_chat_id, chat_type = cur_chat_id, cur_chat_type
            policy = "applied"
        else:
            scene = load_platform_scene(platform)
            if scene.get("platform_chat_id"):
                platform_chat_id = str(scene["platform_chat_id"])
                chat_type = normalize_chat_type(scene.get("chat_type"))
                policy = "applied"
            else:
                return {
                    "runtime_key": "",
                    "platform": platform,
                    "platform_chat_id": "",
                    "chat_type": "",
                    "redirected": False,
                    "policy": "rejected_unknown_scene",
                }

    runtime_key = f"{platform}:{platform_chat_id}" if platform_chat_id else ""
    redirected = bool(runtime_key) and runtime_key != str(current.get("runtime_key") or "").strip()
    return {
        "runtime_key": runtime_key,
        "platform": platform,
        "platform_chat_id": platform_chat_id,
        "chat_type": chat_type,
        "redirected": redirected,
        "policy": policy,
    }
