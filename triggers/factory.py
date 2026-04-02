from __future__ import annotations

from typing import Any, Dict, List

from .base import BaseTrigger
from .email import EmailTrigger


def build_triggers(trigger_cfg: Dict[str, Any], default_chat_id: str = "") -> List[BaseTrigger]:
    """根据配置构建触发器实例列表。"""
    result: List[BaseTrigger] = []
    email_cfg = (trigger_cfg or {}).get("email", {}) or {}
    if email_cfg.get("enabled", False):
        result.append(EmailTrigger(email_cfg, default_chat_id=default_chat_id))
    return result
