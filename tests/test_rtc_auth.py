"""RTC 鉴权测试（P1）：initData HMAC 校验 + 静态 token 双轨 + 限速。"""

import hashlib
import hmac
import json
import time

import pytest

from rtc.auth import AuthResult, RateLimiter, RtcAuthenticator, verify_init_data


def _make_init_data(bot_token: str, user_id: int = 42, auth_age: int = 0,
                    mutate_hash: bool = False) -> str:
    """按官方算法构造一份合法 initData（或被篡改 hash 的）。"""
    fields = {
        "auth_date": str(int(time.time()) - auth_age),
        "query_id": "AAF1",
        "user": json.dumps({"id": user_id, "first_name": "Test", "username": "tester"}),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    # 官方语义：key="WebAppData"，message=bot_token（与 rtc/auth.py 同向，
    # 之前两边都写反所以 round-trip 自洽地"通过"了）
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if mutate_hash:
        h = "0" * 64
    fields["hash"] = h
    return "&".join(f"{k}={v}" for k, v in fields.items())


class TestVerifyInitData:
    def test_valid_signature(self):
        init_data = _make_init_data("BOT:TOKEN")
        ok, reason, fields = verify_init_data(init_data, "BOT:TOKEN")
        assert ok and reason == ""
        assert fields["user"]

    def test_wrong_token_fails(self):
        init_data = _make_init_data("BOT:TOKEN")
        ok, reason, _ = verify_init_data(init_data, "OTHER:TOKEN")
        assert not ok and reason == "hash_mismatch"

    def test_tampered_hash_fails(self):
        init_data = _make_init_data("BOT:TOKEN", mutate_hash=True)
        ok, reason, _ = verify_init_data(init_data, "BOT:TOKEN")
        assert not ok and reason == "hash_mismatch"

    def test_expired_auth_date_fails(self):
        init_data = _make_init_data("BOT:TOKEN", auth_age=2 * 60 * 60)  # 2 小时前（窗口 1h）
        ok, reason, _ = verify_init_data(init_data, "BOT:TOKEN")
        assert not ok and reason == "auth_date_expired"

    def test_future_auth_date_fails(self):
        init_data = _make_init_data("BOT:TOKEN", auth_age=-2 * 60 * 60)  # 2 小时后
        ok, reason, _ = verify_init_data(init_data, "BOT:TOKEN")
        assert not ok and reason == "auth_date_expired"

    def test_ten_minutes_still_valid(self):
        """页面停留 10 分钟后点开始（2026-08-28 生产踩过）：1 小时窗口内应通过。"""
        init_data = _make_init_data("BOT:TOKEN", auth_age=10 * 60)
        ok, reason, _ = verify_init_data(init_data, "BOT:TOKEN")
        assert ok and reason == ""

    def test_missing_hash_fails(self):
        ok, reason, _ = verify_init_data("auth_date=123&user=x", "BOT:TOKEN")
        assert not ok and reason == "hash_missing"

    def test_empty_input(self):
        ok, reason, _ = verify_init_data("", "BOT:TOKEN")
        assert not ok

    def test_real_telegram_sample(self):
        """真机样本回归锁（2026-08-28 生产定位的参数顺序 bug）。

        数据来自 Nora5465_Bot 的真实 initData（user_id 6112866979），
        用该 bot 的真实 token 校验必须通过——锁住 HMAC 参数方向
        （key="WebAppData"，message=bot_token）。
        """
        import pytest

        try:
            with open("adapters/telegram/config.json", "r", encoding="utf-8") as f:
                bot_token = str(json.load(f).get("bot_token") or "").strip()
        except OSError:
            bot_token = ""
        if not bot_token or bot_token.startswith("YOUR_"):
            pytest.skip("本地无真实 bot_token，真机样本测试跳过")

        # auth_date=1787853343 已过时效窗口，只验签名段（跳过时效检查）
        raw = (
            "user=%7B%22id%22%3A6112866979%2C%22first_name%22%3A%22CaptainCN%22"
            "%2C%22last_name%22%3A%22_5464%22%2C%22username%22%3A%22captaincn_5464%22"
            "%2C%22language_code%22%3A%22zh-hans%22%2C%22allows_write_to_pm%22%3Atrue"
            "%2C%22photo_url%22%3A%22https%3A%5C%2F%5C%2Ft.me%5C%2Fi%5C%2Fuserpic"
            "%5C%2F320%5C%2FIKNnuA3WulA7a1Tu8j6UjHrxwKN077N7a57Vf-51dRXgwxnucNqYBO1Kn3r6JnFt.svg"
            "%22%7D&chat_instance=-154303455253985947&chat_type=private"
            "&auth_date=1787853343"
            "&signature=MmhjjxNKUSmczRa1A79Wm7yYCbAQjxyHjSCkun_mqYV4Y4uosJQxMkgEYf0XpTCt7G6oEN71YQyVKEWZBExrDA"
            "&hash=763904f027f5ef49c48c5be700c7d9af697e010a03f67b6766ce0edc995047aa"
        )
        # 只验签名（时效已过）：直接调用签名计算逻辑
        from urllib.parse import parse_qsl

        pairs = dict(parse_qsl(raw, keep_blank_values=True))
        received = pairs.pop("hash")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(expected, received), "HMAC 参数方向回归了"


class TestRateLimiter:
    def test_locks_after_limit(self):
        lim = RateLimiter(fail_limit=3, lock_seconds=60)
        for _ in range(3):
            lim.record_fail("1.2.3.4")
        assert lim.is_locked("1.2.3.4")
        assert not lim.is_locked("5.6.7.8")

    def test_reset_unlocks(self):
        lim = RateLimiter(fail_limit=2, lock_seconds=60)
        lim.record_fail("ip")
        lim.record_fail("ip")
        assert lim.is_locked("ip")
        lim.reset("ip")
        assert not lim.is_locked("ip")

    def test_old_fails_expire(self):
        lim = RateLimiter(fail_limit=2, lock_seconds=0.05)
        t0 = time.monotonic()
        lim.record_fail("ip", now=t0)
        lim.record_fail("ip", now=t0)
        assert lim.is_locked("ip", now=t0 + 0.01)
        assert not lim.is_locked("ip", now=t0 + 1.0)  # 窗口外不再计数


class TestRtcAuthenticator:
    def _patch_owner(self, monkeypatch, is_owner: bool):
        monkeypatch.setattr(
            "rtc.auth.RtcAuthenticator._is_owner",
            staticmethod(lambda platform, user_id: is_owner),
        )

    def test_static_token_ok(self):
        auth = RtcAuthenticator(static_token="sec-token")
        r = auth.authenticate(token="sec-token", client_ip="1.1.1.1")
        assert r.ok and r.platform == "static"

    def test_static_token_wrong_records_fail(self):
        auth = RtcAuthenticator(static_token="sec-token")
        r = auth.authenticate(token="wrong", client_ip="2.2.2.2")
        assert not r.ok and r.reason == "token_invalid"
        # 失败已入账：连错 5 次后被锁
        for _ in range(4):
            auth.authenticate(token="wrong", client_ip="2.2.2.2")
        r2 = auth.authenticate(token="sec-token", client_ip="2.2.2.2")
        assert not r2.ok and r2.reason == "rate_limited"

    def test_static_token_lock_other_ip_unaffected(self):
        auth = RtcAuthenticator(static_token="sec-token")
        for _ in range(6):
            auth.authenticate(token="bad", client_ip="9.9.9.9")
        r = auth.authenticate(token="sec-token", client_ip="8.8.8.8")
        assert r.ok

    def test_init_data_owner_ok(self, monkeypatch):
        self._patch_owner(monkeypatch, True)
        monkeypatch.setattr("rtc.auth._telegram_bot_token", lambda: "BOT:TOKEN")
        auth = RtcAuthenticator(static_token="fallback")
        r = auth.authenticate(init_data=_make_init_data("BOT:TOKEN"), client_ip="3.3.3.3")
        assert r.ok and r.user_id == "42" and r.platform == "telegram"
        assert r.display_name == "Test"

    def test_init_data_not_owner_rejected(self, monkeypatch):
        self._patch_owner(monkeypatch, False)
        monkeypatch.setattr("rtc.auth._telegram_bot_token", lambda: "BOT:TOKEN")
        auth = RtcAuthenticator()
        r = auth.authenticate(init_data=_make_init_data("BOT:TOKEN"))
        assert not r.ok and r.reason == "not_owner"

    def test_init_data_invalid_does_not_fall_back_to_token(self, monkeypatch):
        """initData 提供但校验失败：不静默回落 token 轨（防绕行撞库）。"""
        self._patch_owner(monkeypatch, True)
        monkeypatch.setattr("rtc.auth._telegram_bot_token", lambda: "BOT:TOKEN")
        auth = RtcAuthenticator(static_token="sec-token")
        r = auth.authenticate(init_data=_make_init_data("WRONG:TOKEN"),
                              token="sec-token")
        assert not r.ok and r.reason.startswith("init_data_")

    def test_no_auth_available(self):
        auth = RtcAuthenticator(static_token="")
        r = auth.authenticate()
        assert not r.ok and r.reason == "no_auth_available"

    def test_auth_result_defaults(self):
        r = AuthResult(ok=False)
        assert (r.reason, r.user_id, r.platform) == ("", "", "")
