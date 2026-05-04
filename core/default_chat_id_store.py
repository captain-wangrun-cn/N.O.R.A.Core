"""default_chat_id 的本地持久化。"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

_DEFAULT_CHAT_ID_STATE_FILE = "default_chat_id.json"


def _get_state_file_path() -> str:
    ws = get_workspace_manager()
    state_dir = ws.data_dir
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, _DEFAULT_CHAT_ID_STATE_FILE)


def load_default_chat_id() -> str:
    """从 workspace/data/default_chat_id.json 读取持久化的 default_chat_id。"""
    path = _get_state_file_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        value = str(data.get("default_chat_id") or "").strip()
        return value
    except Exception as e:
        logger.warning(f"读取持久化 default_chat_id 失败: {e}")
        return ""


def save_default_chat_id(chat_id: Optional[str]) -> bool:
    """将 default_chat_id 持久化到 workspace/data/default_chat_id.json。"""
    value = str(chat_id or "").strip()
    if not value:
        return False
    path = _get_state_file_path()
    payload = {"default_chat_id": value}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"保存持久化 default_chat_id 失败: {e}")
        return False
