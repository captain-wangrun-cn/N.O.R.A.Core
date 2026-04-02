'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-02 22:12:47
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import pytest

from triggers.email.main import EmailTrigger


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"decision":"NOTIFY","reason":"验证码邮件"}', "NOTIFY"),
        ('{"decision":"SKIP","reason":"促销"}', "SKIP"),
        ("NOTIFY: 这是紧急变更通知", "NOTIFY"),
    ],
)
def test_parse_filter_decision(raw, expected):
    result = EmailTrigger._parse_filter_decision(raw)
    assert result["decision"] == expected


def test_parse_filter_decision_fallback_skip():
    result = EmailTrigger._parse_filter_decision("普通营销邮件，无需提醒")
    assert result["decision"] == "SKIP"


def test_build_review_key_prefers_uid():
    key = EmailTrigger._build_review_key(
        uid="12345",
        message_id="<abc@example.com>",
        sender="a@example.com",
        subject="hello",
        date_str="Fri, 03 Apr 2026 10:00:00 +0800",
        body_preview="content",
    )
    assert key == "uid:12345"


def test_build_review_key_fallback_is_stable():
    key1 = EmailTrigger._build_review_key(
        uid="",
        message_id="",
        sender="A@example.com",
        subject="Test Subject",
        date_str="Fri, 03 Apr 2026 10:00:00 +0800",
        body_preview="Same Body",
    )
    key2 = EmailTrigger._build_review_key(
        uid="",
        message_id="",
        sender="a@example.com",
        subject="test subject",
        date_str="fri, 03 apr 2026 10:00:00 +0800",
        body_preview="same body",
    )
    assert key1.startswith("fallback:")
    assert key1 == key2
