"""主人识别注册表（最小实现）。

单主人个人实例：解决"在多平台、群聊里认出谁是主人"，而非多租户账号系统。

策略（对话拍板）：
  - 每平台第一个私聊者自动绑定为主人。
  - config.yml -> owner.identities 可预声明主人身份，启动注入。
  - 群聊里只有匹配已绑定身份的人才算主人，未绑定的群成员一律访客。

持久化：workspace/data/owner_bindings.json
  {
    "owner_id": "owner:default",
    "bindings": [
      {"platform": "telegram", "user_id": "123", "display_name": "...", "bound_at": "..."}
    ]
  }
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.conversation_identity import DEFAULT_OWNER_ID, PRIVATE_CHAT_TYPE

logger = logging.getLogger(__name__)

_BINDINGS_FILE = "owner_bindings.json"


class OwnerRegistry:
    """主人绑定的加载/解析/持久化。线程安全（粗粒度锁）。"""

    def __init__(self, state_path: Optional[str] = None, owner_id: str = DEFAULT_OWNER_ID):
        self._lock = threading.RLock()
        self._owner_id = owner_id
        self._state_path = state_path  # None → 惰性解析 workspace data 目录
        self._bindings: List[Dict[str, Any]] = []
        self._loaded = False

    # --- 路径与持久化 ---
    def _resolve_state_path(self) -> Optional[str]:
        if self._state_path:
            return self._state_path
        try:
            from workspace_config import get_workspace_manager

            ws = get_workspace_manager()
            data_dir = ws.data_dir
            os.makedirs(data_dir, exist_ok=True)
            self._state_path = os.path.join(data_dir, _BINDINGS_FILE)
            return self._state_path
        except Exception as e:
            logger.warning(f"无法解析 owner_bindings 路径: {e}")
            return None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._bindings = self._read_from_disk()
            self._loaded = True

    def _read_from_disk(self) -> List[Dict[str, Any]]:
        path = self._resolve_state_path()
        if not path or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            bindings = data.get("bindings")
            if isinstance(bindings, list):
                return [b for b in bindings if isinstance(b, dict)]
        except Exception as e:
            logger.warning(f"读取 owner_bindings 失败: {e}")
        return []

    def _write_to_disk(self) -> bool:
        path = self._resolve_state_path()
        if not path:
            return False
        payload = {"owner_id": self._owner_id, "bindings": self._bindings}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"保存 owner_bindings 失败: {e}")
            return False

    # --- 查询/变更辅助 ---
    @staticmethod
    def _norm(value: Any) -> str:
        return str(value or "").strip()

    def _find(self, platform: str, user_id: str) -> Optional[Dict[str, Any]]:
        platform = self._norm(platform)
        user_id = self._norm(user_id)
        for b in self._bindings:
            if self._norm(b.get("platform")) == platform and self._norm(b.get("user_id")) == user_id:
                return b
        return None

    def is_bound(self, platform: str, user_id: str) -> bool:
        self._ensure_loaded()
        with self._lock:
            return self._find(platform, user_id) is not None

    def bind(
        self,
        platform: str,
        user_id: str,
        display_name: str = "",
        *,
        source: str = "auto",
        persist: bool = True,
    ) -> bool:
        """绑定一个主人身份。已存在则按需更新昵称。返回是否新增。"""
        platform = self._norm(platform)
        user_id = self._norm(user_id)
        if not platform or not user_id:
            return False
        self._ensure_loaded()
        with self._lock:
            existing = self._find(platform, user_id)
            if existing is not None:
                name = self._norm(display_name)
                if name and existing.get("display_name") != name:
                    existing["display_name"] = name
                    if persist:
                        self._write_to_disk()
                return False
            self._bindings.append(
                {
                    "platform": platform,
                    "user_id": user_id,
                    "display_name": self._norm(display_name),
                    "source": source,
                    "bound_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            if persist:
                self._write_to_disk()
            logger.info(f"已绑定主人身份: {platform}:{user_id} (source={source})")
            return True

    def has_binding_for_platform(self, platform: str) -> bool:
        platform = self._norm(platform)
        self._ensure_loaded()
        with self._lock:
            return any(self._norm(b.get("platform")) == platform for b in self._bindings)

    def resolve_is_owner(self, platform: str, user_id: str, chat_type: str) -> bool:
        """判断说话人是否主人。

        - 命中已绑定身份 → True
        - 私聊且该平台尚无任何绑定 → 自动绑定并 True（首个私聊者）
        - 群聊未绑定 → False（访客）
        - 私聊但该平台已绑定他人 → False
        """
        platform = self._norm(platform)
        user_id = self._norm(user_id)
        if not platform or not user_id:
            return False
        self._ensure_loaded()
        with self._lock:
            if self._find(platform, user_id) is not None:
                return True
            is_private = self._norm(chat_type or PRIVATE_CHAT_TYPE).lower() == PRIVATE_CHAT_TYPE
            if is_private and not self.has_binding_for_platform(platform):
                # 首个私聊者自动绑为主人
                self.bind(platform, user_id, source="auto")
                return True
            return False

    def seed_from_config(self, identities: Optional[List[Dict[str, Any]]]) -> int:
        """从 config 预声明的主人身份注入绑定。返回新增条数。"""
        if not identities:
            return 0
        added = 0
        for item in identities:
            if not isinstance(item, dict):
                continue
            platform = self._norm(item.get("platform"))
            user_id = self._norm(item.get("user_id"))
            display_name = self._norm(item.get("display_name"))
            if not platform or not user_id:
                continue
            if self.bind(platform, user_id, display_name, source="config"):
                added += 1
        return added

    def list_bindings(self) -> List[Dict[str, Any]]:
        self._ensure_loaded()
        with self._lock:
            return [dict(b) for b in self._bindings]


# --- 模块级单例 ---
_registry: Optional[OwnerRegistry] = None
_registry_lock = threading.Lock()


def get_owner_registry() -> OwnerRegistry:
    """获取全局主人注册表，首次访问时从 config 预声明身份。"""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is not None:
            return _registry
        registry = OwnerRegistry()
        try:
            import config

            owner_cfg = config.get_owner_config() or {}
            registry.seed_from_config(owner_cfg.get("identities"))
        except Exception as e:
            logger.debug(f"owner config 预声明注入跳过: {e}")
        _registry = registry
        return _registry


def reset_owner_registry(registry: Optional[OwnerRegistry] = None) -> None:
    """重置全局单例（测试用）。"""
    global _registry
    with _registry_lock:
        _registry = registry


def resolve_is_owner(platform: str, user_id: str, chat_type: str) -> bool:
    """模块级便捷函数，供 conversation_identity 作为 owner_resolver 注入。"""
    return get_owner_registry().resolve_is_owner(platform, user_id, chat_type)
