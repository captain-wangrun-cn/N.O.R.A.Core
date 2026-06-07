"""最近活跃投递端的本地持久化（跨平台接力 Phase 4）。

主动消息（proactive / trigger）默认投递到「用户最后活跃的私聊端」，
不可用时回退 default_chat_id。**只记录私聊端**：群聊活跃不更新投递目标，
落实「绝不自动发进群」的决策（见 plans/nora-cross-platform-relay.md 第二部分决策 2）。

持久化文件：workspace/data/delivery_target_state.json
  {
    "last_active_runtime_key": "telegram:123",
    "last_active_platform": "telegram",
    "last_active_platform_chat_id": "123",
    "last_active_at": 1717650000.0,
    "memory_scope_id": "relationship:owner:default"
  }
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

from core.conversation_identity import (
    PRIVATE_CHAT_TYPE,
    build_identity_from_target,
    normalize_chat_type,
)
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

_DELIVERY_TARGET_STATE_FILE = "delivery_target_state.json"


def _get_state_file_path() -> str:
    ws = get_workspace_manager()
    state_dir = ws.data_dir
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, _DELIVERY_TARGET_STATE_FILE)


def update_last_active_target(target: Dict[str, Any]) -> bool:
    """更新最近活跃投递端。**仅私聊端**会被记录；群聊一律忽略。

    返回是否实际写入（群聊或缺字段时返回 False，不改动文件）。
    """
    chat_type = normalize_chat_type(target.get("chat_type"))
    if chat_type != PRIVATE_CHAT_TYPE:
        # 群聊活跃不改变投递目标（仅私聊投递）。
        return False

    identity = build_identity_from_target(target)
    runtime_key = identity.runtime_key.strip()
    if not runtime_key:
        return False

    payload = {
        "last_active_runtime_key": runtime_key,
        "last_active_platform": identity.platform,
        "last_active_platform_chat_id": identity.platform_chat_id,
        "last_active_at": time.time(),
        "memory_scope_id": identity.memory_scope_id,
    }
    path = _get_state_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"保存最近活跃投递端失败: {e}")
        return False


def load_last_active_target() -> Dict[str, Any]:
    """读取最近活跃投递端。无记录或读取失败返回 {}。"""
    path = _get_state_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.warning(f"读取最近活跃投递端失败: {e}")
        return {}


def load_last_active_runtime_key() -> str:
    """读取最近活跃私聊端的 runtime_key（无则空串）。"""
    target = load_last_active_target()
    return str(target.get("last_active_runtime_key") or "").strip()


def resolve_delivery_runtime_key(fallback: str = "") -> str:
    """解析主动消息投递目标：优先最近活跃私聊端，不可用则回退 fallback。"""
    return load_last_active_runtime_key() or str(fallback or "").strip()
