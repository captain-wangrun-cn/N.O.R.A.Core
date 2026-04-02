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
