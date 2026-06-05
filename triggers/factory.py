from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseTrigger
from .email import EmailTrigger


def build_triggers(
    trigger_cfg: Dict[str, Any],
    default_chat_id: str = "",
    default_chat_target: Optional[Dict[str, Any]] = None,
) -> List[BaseTrigger]:
    """根据配置构建触发器实例列表。"""
    result: List[BaseTrigger] = []
    email_cfg = (trigger_cfg or {}).get("email", {}) or {}
    if email_cfg.get("enabled", False):
        result.append(
            EmailTrigger(
                email_cfg,
                default_chat_id=default_chat_id,
                default_chat_target=default_chat_target,
            )
        )
    return result
