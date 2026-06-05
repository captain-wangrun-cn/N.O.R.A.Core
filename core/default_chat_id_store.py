"""default_chat_id 的本地持久化。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from core.conversation_identity import build_identity_from_target, conversation_target_dict
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

_DEFAULT_CHAT_ID_STATE_FILE = "default_chat_id.json"


def _get_state_file_path() -> str:
    ws = get_workspace_manager()
    state_dir = ws.data_dir
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, _DEFAULT_CHAT_ID_STATE_FILE)


def load_default_chat_target() -> Dict[str, Any]:
    """读取默认会话目标。兼容旧 default_chat_id 字段。"""
    path = _get_state_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        target = data.get("default_conversation_target")
        if isinstance(target, dict):
            identity = build_identity_from_target(target)
            return conversation_target_dict(identity)
        legacy = str(data.get("default_chat_id") or "").strip()
        if legacy:
            identity = build_identity_from_target({"runtime_key": legacy, "chat_type": "private"})
            return conversation_target_dict(identity)
        return {}
    except Exception as e:
        logger.warning(f"读取持久化 default_chat_id 失败: {e}")
        return {}


def load_default_chat_id() -> str:
    """从 workspace/data/default_chat_id.json 读取持久化的 default_chat_id。"""
    target = load_default_chat_target()
    return str(target.get("runtime_key") or target.get("default_chat_id") or "").strip()


def save_default_chat_id(chat_id: Optional[str]) -> bool:
    """将 default_chat_id 持久化到 workspace/data/default_chat_id.json。"""
    value = str(chat_id or "").strip()
    if not value:
        return False
    identity = build_identity_from_target({"runtime_key": value, "chat_type": "private"})
    return save_default_chat_target(conversation_target_dict(identity))


def save_default_chat_target(target: Dict[str, Any]) -> bool:
    """将完整默认会话目标持久化。"""
    identity = build_identity_from_target(target)
    payload = {
        "default_chat_id": identity.runtime_key,
        "default_conversation_target": conversation_target_dict(identity),
    }
    path = _get_state_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"保存持久化 default_chat_id 失败: {e}")
        return False
