'''
Author: WR(captain-wangrun-cn)
Date: 2026-04-26 21:50:30
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-04-26 21:50:37
FilePath: \N.O.R.A.Core\tests\test_nora_preferences.py
'''
'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-26 21:50:30
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
import config
from core.scheduler import ScheduledEvent


def test_get_nora_preferences_defaults(monkeypatch):
    monkeypatch.setattr(config, "_config", {})
    prefs = config.get_nora_preferences()

    assert prefs["nora_followup_probability"] == 1.0
    assert prefs["proactive_message_probability"] == 1.0
    assert prefs["pause_between_splits_seconds"] == 1.0
    assert prefs["followup_skip_end_after"] == 3


def test_get_nora_preferences_merge_existing(monkeypatch):
    monkeypatch.setattr(
        config,
        "_config",
        {
            "nora_preferences": {
                "warmth_level": 0.9,
                "split_reply_probability": 0.25,
            }
        },
    )

    prefs = config.get_nora_preferences()
    assert prefs["warmth_level"] == 0.9
    assert prefs["split_reply_probability"] == 0.25
    assert prefs["assertiveness_level"] == 0.5


def test_scheduled_event_persists_message_kind():
    event = ScheduledEvent(
        trigger_time=__import__("datetime").datetime(2026, 4, 26, 8, 30),
        reason="早安提醒",
        event_type="proactive",
        message_kind="explicit",
        chat_id="123",
    )

    data = event.to_dict()
    assert data["message_kind"] == "explicit"
