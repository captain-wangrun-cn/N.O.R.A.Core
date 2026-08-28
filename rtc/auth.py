"""RTC 双轨鉴权（P1）。

两条轨（设计文档 docs/architecture/rtc.md §6），按序尝试：

1. **initData**（TG Mini App 入口，零输入）
   Telegram webview 注入的 initData，后端用 `adapters/telegram/config.json`
   的 bot_token 做 HMAC-SHA256 校验：
   secret = HMAC-SHA256(bot_token, "WebAppData")；对 initData 中除 hash 外的
   所有字段按 key 字典序拼 `k=v` 换行串，HMAC-SHA256(secret, data_check_string)
   与 hash 比对；另校验 auth_date 时效（默认 ±5 分钟，可配）。
   校验通过后从 user 字段解析 user_id → owner_registry 判定 owner。
2. **静态 token**（桌面浏览器 / 非 TG 入口）
   `rtc/config.json` 的 static_token。失败限速：同 IP 连续失败 5 次锁 10 分钟。

绝不信客户端自报的 user id——身份只能来自签名校验或静态 token 本身。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)

# initData 时效窗口（秒）：TG 侧 initData 在页面打开时生成一次，用户可能在
# 页面里停留很久才点开始通话——5 分钟太紧会误伤（实测踩过）。签名本身防伪，
# 时效只防重放，个人 bot 场景放宽到 1 小时
INIT_DATA_MAX_AGE_SECONDS = 60 * 60

# 静态 token 失败限速：连续失败 N 次锁 M 秒
TOKEN_FAIL_LIMIT = 5
TOKEN_LOCK_SECONDS = 10 * 60


@dataclass
class AuthResult:
    """一次鉴权尝试的结果。ok=False 时 reason 给前端提示。"""

    ok: bool
    reason: str = ""
    # 身份（ok=True 时必有）：TG 入口是 telegram user_id，静态 token 轨固定 owner
    user_id: str = ""
    platform: str = ""
    display_name: str = ""


class RateLimiter:
    """按 key（这里是 IP）的失败计数锁。够用就好，不做持久化。"""

    def __init__(self, fail_limit: int = TOKEN_FAIL_LIMIT,
                 lock_seconds: int = TOKEN_LOCK_SECONDS):
        self.fail_limit = fail_limit
        self.lock_seconds = lock_seconds
        self._fails: dict[str, list[float]] = {}

    def is_locked(self, key: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        fails = [t for t in self._fails.get(key, []) if now - t < self.lock_seconds]
        self._fails[key] = fails
        return len(fails) >= self.fail_limit

    def record_fail(self, key: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.monotonic()
        self._fails.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


def _telegram_bot_token() -> str:
    """读 adapters/telegram/config.json 的 bot_token。读不到返回空串（该轨禁用）。"""
    try:
        from pathlib import Path

        token_path = Path(__file__).resolve().parent.parent / "adapters" / "telegram" / "config.json"
        if not token_path.exists():
            return ""
        with open(token_path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("bot_token") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 telegram bot_token 失败，initData 轨禁用: %s", e)
        return ""


def verify_init_data(init_data: str, bot_token: str,
                     max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS,
                     now: Optional[float] = None) -> tuple[bool, str, dict]:
    """校验 Telegram initData 签名与时效。

    返回 (ok, reason, fields)：ok 时 fields 是 initData 解析出的字段 dict
    （含 user）。按官方算法：
      secret        = HMAC-SHA256(bot_token, "WebAppData")
      data_check    = "\n".join(f"{k}={v}" for k,v in sorted(fields) if k != "hash")
      expected_hash = HMAC-SHA256(secret, data_check) 的 hex
    """
    if not init_data or not bot_token:
        return False, "init_data_missing", {}

    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields = dict(pairs)
    received_hash = fields.pop("hash", "")
    if not received_hash:
        return False, "hash_missing", {}
    if not fields:
        return False, "empty_data", {}

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    # 官方语义：secret = HMAC-SHA256(data=bot_token, key="WebAppData")。
    # 注意 Python hmac.new(key, msg) 是 key 在前——这里 key 是常量
    # "WebAppData"、message 是 bot_token（实测：参数反了必 hash_mismatch，
    # 2026-08-28 真机 initData 样本定位）
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    # hmac.compare_digest 防时序侧信道
    if not hmac.compare_digest(expected, received_hash):
        return False, "hash_mismatch", {}

    # auth_date 时效
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except (TypeError, ValueError):
        return False, "auth_date_invalid", {}
    now = now if now is not None else time.time()
    if auth_date <= 0 or abs(now - auth_date) > max_age_seconds:
        return False, "auth_date_expired", {}

    return True, "", fields


def _parse_init_data_user(fields: dict) -> tuple[str, str]:
    """从 initData 的 user 字段解析 (user_id, display_name)。"""
    try:
        user = json.loads(fields.get("user", "{}"))
        user_id = str(user.get("id") or "")
        first = str(user.get("first_name") or "")
        last = str(user.get("last_name") or "")
        name = (first + " " + last).strip() or str(user.get("username") or "")
        return user_id, name
    except (json.JSONDecodeError, TypeError):
        return "", ""


class RtcAuthenticator:
    """双轨鉴权器。server.py 对每个 WS 连接调用 authenticate()。"""

    def __init__(self, static_token: str = "",
                 token_limiter: Optional[RateLimiter] = None):
        self.static_token = static_token
        self.limiter = token_limiter or RateLimiter()

    def authenticate(self, *, init_data: str = "", token: str = "",
                     client_ip: str = "") -> AuthResult:
        """按序尝试 initData → 静态 token。失败给 reason，绝不抛异常。"""
        # 轨 1：initData（只在提供了才尝试；bot_token 未配置时该轨不可用）
        if init_data:
            bot_token = _telegram_bot_token()
            if bot_token:
                ok, reason, fields = verify_init_data(init_data, bot_token)
                if ok:
                    user_id, display_name = _parse_init_data_user(fields)
                    if not user_id:
                        return AuthResult(False, "user_parse_failed")
                    if not self._is_owner("telegram", user_id):
                        return AuthResult(False, "not_owner", user_id, "telegram", display_name)
                    return AuthResult(True, "", user_id, "telegram", display_name)
                # initData 有但校验失败：不静默回落 token 轨（防拿无效 initData 的
                # 攻击者绕去撞静态 token），直接报失败
                return AuthResult(False, f"init_data_{reason}")
            logger.warning("initData 提供但 telegram bot_token 未配置，该轨禁用")

        # 轨 2：静态 token
        if self.static_token:
            if self.limiter.is_locked(client_ip or "unknown"):
                return AuthResult(False, "rate_limited")
            if token and hmac.compare_digest(token, self.static_token):
                self.limiter.reset(client_ip or "unknown")
                # 静态 token 是 owner 手工配置的，持有即 owner
                return AuthResult(True, "", "owner", "static", "owner")
            self.limiter.record_fail(client_ip or "unknown")
            return AuthResult(False, "token_invalid")

        return AuthResult(False, "no_auth_available")

    @staticmethod
    def _is_owner(platform: str, user_id: str) -> bool:
        """TG 入口判定 owner。owner 解析器未注入时保守拒绝（MVP owner-only）。"""
        from core.owner_registry import resolve_is_owner

        return resolve_is_owner(platform, user_id, chat_type="private")
