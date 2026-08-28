"""RTC 邀请管理（P1）。

设计文档 docs/architecture/rtc.md §10：Nora 无法真正"拨打"，邀请的真实语义是
**发一条带 WebApp 按钮的消息**，通话在用户点下按钮那一刻开始。

本模块只负责邀请文案/URL/TTL 状态；发送动作由调用方（telegram adapter 的
/rtc 命令 / P4 的 [RTC] 前脑标记）执行。同一用户同时只挂一个有效邀请。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Invitation:
    user_id: str
    platform: str = "telegram"
    created_at: float = field(default_factory=time.monotonic)
    ttl_seconds: int = 300
    reason: str = ""  # Nora 主动邀约时的理由（/rtc 命令为空）

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl_seconds

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.ttl_seconds - (time.monotonic() - self.created_at)))


class InvitationStore:
    """同一用户同时只挂一个有效邀请；Nora 侧邀约冷却去重。"""

    def __init__(self, default_ttl: int = 300, invite_cooldown: int = 600):
        self.default_ttl = default_ttl
        self.invite_cooldown = invite_cooldown
        self._active: dict[str, Invitation] = {}  # key = platform:user_id
        self._last_nora_invite: dict[str, float] = {}

    def _key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    def issue(self, user_id: str, platform: str = "telegram",
              reason: str = "", ttl: int | None = None) -> Invitation:
        """发一张新邀请（覆盖旧的同键邀请）。"""
        inv = Invitation(
            user_id=user_id, platform=platform,
            ttl_seconds=ttl if ttl is not None else self.default_ttl,
            reason=reason,
        )
        self._active[self._key(platform, user_id)] = inv
        return inv

    def validate(self, user_id: str, platform: str = "telegram") -> tuple[bool, str]:
        """校验邀请是否仍有效（点按钮/连 WS 时用）。返回 (ok, 原因)。"""
        inv = self._active.get(self._key(platform, user_id))
        if inv is None:
            return False, "no_invitation"
        if inv.expired:
            self._active.pop(self._key(platform, user_id), None)
            return False, "expired"
        return True, ""

    def consume(self, user_id: str, platform: str = "telegram") -> None:
        """开始通话后撤销邀请（防一张邀请开多路）。"""
        self._active.pop(self._key(platform, user_id), None)

    # ---- Nora 主动邀约的冷却（P4 用；/rtc 命令不受冷却限制）

    def nora_invite_cooled_down(self, user_id: str, platform: str = "telegram") -> bool:
        """Nora 侧冷却是否已过（防止反复邀约变骚扰）。"""
        last = self._last_nora_invite.get(self._key(platform, user_id))
        if last is None:
            return True
        return (time.monotonic() - last) > self.invite_cooldown

    def mark_nora_invited(self, user_id: str, platform: str = "telegram") -> None:
        self._last_nora_invite[self._key(platform, user_id)] = time.monotonic()


def build_call_url(public_url: str, backend: str = "") -> str:
    """生成 Mini App 按钮 URL：public_url 基础上可带 ?backend= 参数。

    public_url 形如 https://host/rtc/；backend 只在中央托管镜像场景需要，
    同源托管（默认）不带。
    """
    base = public_url.rstrip("/")
    if backend:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}backend={backend}"
    return base


def invitation_text(reason: str = "", remaining_seconds: int = 300) -> str:
    """邀请消息正文。reason 非空 = Nora 主动邀约。"""
    minutes = max(1, remaining_seconds // 60)
    if reason:
        return f"{reason}\n\n（点下面的按钮开始通话，{minutes} 分钟内有效）"
    return f"📞 点击下面的按钮，和 Nora 开始语音通话。\n（邀请 {minutes} 分钟内有效）"
