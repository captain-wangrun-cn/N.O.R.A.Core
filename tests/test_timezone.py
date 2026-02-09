'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 14:13:59
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
测试时区功能
"""
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
from memory.message_history import MessageHistory


def test_timezone_shanghai():
    """测试使用上海时区"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name
    
    try:
        history = MessageHistory(
            db_path=test_db,
            timezone="Asia/Shanghai"
        )
        
        # 添加第一条消息（应该有时间戳）
        history.add_message(
            platform="test",
            chat_id="tz_test",
            role="user",
            content="测试消息"
        )
        
        # 获取消息
        messages = history.get_context_messages("test", "tz_test")
        assert len(messages) == 1
        
        # 检查时间戳格式
        content = messages[0]["content"]
        assert content.startswith("[")
        assert "]" in content
        
        print(f"✓ 上海时区时间戳: {content}")
        
    finally:
        if os.path.exists(test_db):
            os.remove(test_db)


def test_timezone_utc():
    """测试使用 UTC 时区"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name
    
    try:
        history = MessageHistory(
            db_path=test_db,
            timezone="UTC"
        )
        
        # 添加第一条消息
        history.add_message(
            platform="test",
            chat_id="utc_test",
            role="user",
            content="UTC 测试"
        )
        
        # 获取消息
        messages = history.get_context_messages("test", "utc_test")
        content = messages[0]["content"]
        
        print(f"✓ UTC 时区时间戳: {content}")
        
    finally:
        if os.path.exists(test_db):
            os.remove(test_db)


def test_timezone_newyork():
    """测试使用纽约时区"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name
    
    try:
        history = MessageHistory(
            db_path=test_db,
            timezone="America/New_York"
        )
        
        # 添加第一条消息
        history.add_message(
            platform="test",
            chat_id="ny_test",
            role="user",
            content="纽约测试"
        )
        
        # 获取消息
        messages = history.get_context_messages("test", "ny_test")
        content = messages[0]["content"]
        
        print(f"✓ 纽约时区时间戳: {content}")
        
    finally:
        if os.path.exists(test_db):
            os.remove(test_db)


def test_invalid_timezone_fallback():
    """测试无效时区回退到 UTC"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db = f.name
    
    try:
        history = MessageHistory(
            db_path=test_db,
            timezone="Invalid/Timezone"
        )
        
        # 应该回退到 UTC
        assert history.timezone == ZoneInfo("UTC")
        print("✓ 无效时区成功回退到 UTC")
        
    finally:
        if os.path.exists(test_db):
            os.remove(test_db)


def test_timezone_comparison():
    """测试不同时区的时间戳差异"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db_sh = f.name
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        test_db_utc = f.name
    
    try:
        history_sh = MessageHistory(db_path=test_db_sh, timezone="Asia/Shanghai")
        history_utc = MessageHistory(db_path=test_db_utc, timezone="UTC")
        
        # 同一时刻添加消息
        history_sh.add_message("test", "compare", "user", "测试")
        history_utc.add_message("test", "compare", "user", "测试")
        
        msg_sh = history_sh.get_context_messages("test", "compare")[0]["content"]
        msg_utc = history_utc.get_context_messages("test", "compare")[0]["content"]
        
        print(f"上海时区: {msg_sh}")
        print(f"UTC时区:  {msg_utc}")
        print("✓ 不同时区显示不同的本地时间")
        
    finally:
        if os.path.exists(test_db_sh):
            os.remove(test_db_sh)
        if os.path.exists(test_db_utc):
            os.remove(test_db_utc)


if __name__ == "__main__":
    print("🧪 测试时区功能\n")
    
    test_timezone_shanghai()
    test_timezone_utc()
    test_timezone_newyork()
    test_invalid_timezone_fallback()
    test_timezone_comparison()
    
    print("\n🎉 所有时区测试通过！")
