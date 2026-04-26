'''
Author: WR(captain-wangrun-cn)
Date: 2026-04-26 21:51:02
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-04-26 21:51:11
FilePath: \N.O.R.A.Core\tests\test_nora_prefs_telegram.py
'''
'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-04-26 21:51:02
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
from adapters.telegram.main import TelegramAdapter


class _DummyAdapter:
    _format_nora_pref_value = TelegramAdapter._format_nora_pref_value
    _next_nora_pref_value = TelegramAdapter._next_nora_pref_value


def test_next_probability_pref_value_cycles():
    adapter = _DummyAdapter()
    assert adapter._next_nora_pref_value("warmth_level", 0.0) == 0.25
    assert adapter._next_nora_pref_value("warmth_level", 1.0) == 0.0


def test_next_pause_pref_value_cycles():
    adapter = _DummyAdapter()
    assert adapter._next_nora_pref_value("pause_between_splits_seconds", 0.3) == 0.6
    assert adapter._next_nora_pref_value("pause_between_splits_seconds", 2.2) == 0.3


def test_format_pause_pref_value():
    adapter = _DummyAdapter()
    assert adapter._format_nora_pref_value("pause_between_splits_seconds", 1.4) == "1.4s"
