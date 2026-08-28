"""RTC 邀请管理测试（P1）：TTL / 去重 / URL 构建。"""

import time

from rtc.invitation import (
    InvitationStore, build_call_url, invitation_text,
)


class TestInvitationStore:
    def test_issue_and_validate(self):
        store = InvitationStore(default_ttl=60)
        inv = store.issue("42")
        assert inv.ttl_seconds == 60
        ok, reason = store.validate("42")
        assert ok and reason == ""

    def test_expired(self):
        store = InvitationStore(default_ttl=0.05)
        store.issue("42", ttl=0.05)
        time.sleep(0.1)
        ok, reason = store.validate("42")
        assert not ok and reason == "expired"

    def test_consume(self):
        store = InvitationStore()
        store.issue("42")
        store.consume("42")
        ok, reason = store.validate("42")
        assert not ok and reason == "no_invitation"

    def test_new_invite_overwrites_old(self):
        store = InvitationStore(default_ttl=0.05)
        store.issue("42", ttl=0.05)
        time.sleep(0.1)
        store.issue("42", ttl=60)  # 新邀请覆盖旧的
        ok, _ = store.validate("42")
        assert ok

    def test_nora_cooldown(self):
        store = InvitationStore(invite_cooldown=0.05)
        assert store.nora_invite_cooled_down("42")
        store.mark_nora_invited("42")
        assert not store.nora_invite_cooled_down("42")
        time.sleep(0.1)
        assert store.nora_invite_cooled_down("42")


class TestUrlAndText:
    def test_build_call_url_plain(self):
        assert build_call_url("https://h/rtc/") == "https://h/rtc"

    def test_build_call_url_with_backend(self):
        url = build_call_url("https://h/rtc/", backend="https://b")
        assert url == "https://h/rtc?backend=https://b"
        # 已带 query 的不再用 ?
        url2 = build_call_url("https://h/rtc?x=1", backend="https://b")
        assert url2 == "https://h/rtc?x=1&backend=https://b"

    def test_invitation_text_default(self):
        text = invitation_text(remaining_seconds=300)
        assert "通话" in text and "5 分钟" in text

    def test_invitation_text_with_reason(self):
        text = invitation_text(reason="想你了，想听你声音")
        assert "想你了" in text
