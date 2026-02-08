'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 01:27:48
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
#!/usr/bin/env python3
"""
测试消息时间戳自动插入功能

测试场景:
1. 第一条消息 - 应该有时间戳
2. 5分钟内的消息 - 不应该有时间戳
3. 超过15分钟的消息 - 应该有时间戳
"""

import sys
import os
import time
from datetime import datetime, timedelta

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.message_history import MessageHistory


def test_timestamp_insertion():
    """测试时间戳自动插入"""
    
    # 使用临时数据库
    test_db = "memory/test_timestamp.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    print("🧪 开始测试时间戳自动插入功能\n")
    print("=" * 60)
    
    history = MessageHistory(
        db_path=test_db,
        raw_window=50,
        compress_window=200,
        compress_ratio=10,
        archive_threshold=500
    )
    
    platform = "telegram"
    chat_id = "test_chat_123"
    
    # 测试 1: 第一条消息应该有时间戳
    print("\n📝 测试 1: 第一条消息")
    msg_id_1 = history.add_message(
        platform=platform,
        chat_id=chat_id,
        role="user",
        content="你好，这是第一条消息"
    )
    
    # 读取消息验证
    messages = history.get_context_messages(platform, chat_id, limit=10, include_summaries=False)
    print(f"   消息内容: {messages[-1]['content']}")
    assert "[" in messages[-1]['content'] and "]" in messages[-1]['content'], "第一条消息应该包含时间戳"
    print("   ✅ 通过 - 包含时间戳")
    
    # 测试 2: 5分钟内的消息不应该有时间戳
    print("\n📝 测试 2: 5分钟内的消息（不应该有时间戳）")
    time.sleep(0.1)  # 短暂延迟
    msg_id_2 = history.add_message(
        platform=platform,
        chat_id=chat_id,
        role="user",
        content="这是5分钟内的消息"
    )
    
    messages = history.get_context_messages(platform, chat_id, limit=10, include_summaries=False)
    print(f"   消息内容: {messages[-1]['content']}")
    
    # 检查这条消息本身不应该以时间戳开头（因为没超过15分钟）
    content = messages[-1]['content']
    # 简单检查：如果是新时间戳，会是 [YYYY-MM-DD HH:MM] 格式
    if content.startswith("[") and "]" in content[:20]:
        print("   ❌ 失败 - 不应该包含时间戳")
    else:
        print("   ✅ 通过 - 不包含时间戳")
    
    # 测试 3: 模拟15分钟后的消息（手动修改时间戳）
    print("\n📝 测试 3: 模拟15分钟后的消息")
    print("   （通过直接修改数据库时间戳来模拟）")
    
    import sqlite3
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    
    # 将第一条消息（有时间戳标记的）的时间戳往前推16分钟
    sixteen_minutes_ago = (datetime.now() - timedelta(minutes=16)).timestamp()
    cursor.execute("""
        UPDATE messages 
        SET timestamp = ? 
        WHERE id = ?
    """, (sixteen_minutes_ago, msg_id_1))  # 修改第一条消息而不是第二条
    conn.commit()
    conn.close()
    
    # 现在添加新消息，应该有时间戳
    msg_id_3 = history.add_message(
        platform=platform,
        chat_id=chat_id,
        role="user",
        content="这是15分钟后的消息"
    )
    
    messages = history.get_context_messages(platform, chat_id, limit=10, include_summaries=False)
    print(f"   消息内容: {messages[-1]['content']}")
    
    content = messages[-1]['content']
    if content.startswith("[") and "]" in content[:20]:
        print("   ✅ 通过 - 包含时间戳")
    else:
        print("   ❌ 失败 - 应该包含时间戳")
    
    # 测试 4: 后续紧接着的消息不应该有时间戳
    print("\n📝 测试 4: 紧接着的消息（不应该有时间戳）")
    time.sleep(0.1)
    msg_id_4 = history.add_message(
        platform=platform,
        chat_id=chat_id,
        role="user",
        content="紧接着的第四条消息"
    )
    
    messages = history.get_context_messages(platform, chat_id, limit=10, include_summaries=False)
    print(f"   消息内容: {messages[-1]['content']}")
    
    content = messages[-1]['content']
    if content.startswith("[") and "]" in content[:20]:
        print("   ❌ 失败 - 不应该包含时间戳")
    else:
        print("   ✅ 通过 - 不包含时间戳")
    
    # 显示所有消息
    print("\n" + "=" * 60)
    print("📋 完整对话历史:")
    print("=" * 60)
    messages = history.get_context_messages(platform, chat_id, limit=100, include_summaries=False)
    for i, msg in enumerate(messages, 1):
        role_icon = "👤" if msg["role"] == "user" else "🤖"
        print(f"{i}. {role_icon} {msg['content']}")
    
    # 清理测试数据库
    print("\n" + "=" * 60)
    print("🧹 清理测试数据库...")
    if os.path.exists(test_db):
        os.remove(test_db)
        print(f"   已删除: {test_db}")
    
    print("\n✅ 测试完成！")
    return 0


def test_assistant_messages():
    """测试 assistant 消息不应该有时间戳"""
    print("\n" + "=" * 60)
    print("🧪 测试 assistant 消息（不应该自动添加时间戳）")
    print("=" * 60)
    
    test_db = "memory/test_assistant.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    history = MessageHistory(db_path=test_db)
    platform = "telegram"
    chat_id = "test_chat_456"
    
    # 添加 user 消息（有时间戳）
    history.add_message(platform, chat_id, "user", "用户问题")
    
    # 模拟16分钟后
    import sqlite3
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    sixteen_minutes_ago = (datetime.now() - timedelta(minutes=16)).timestamp()
    cursor.execute("UPDATE messages SET timestamp = ? WHERE role = 'user'", (sixteen_minutes_ago,))
    conn.commit()
    conn.close()
    
    # 添加 assistant 消息（不应该有时间戳）
    history.add_message(platform, chat_id, "assistant", "助手回复")
    
    messages = history.get_context_messages(platform, chat_id, limit=10, include_summaries=False)
    
    print("\n消息列表:")
    for msg in messages:
        print(f"  - [{msg['role']}] {msg['content']}")
    
    # 验证 assistant 消息没有时间戳
    assistant_msg = [m for m in messages if m['role'] == 'assistant'][0]
    if assistant_msg['content'].startswith("[") and "]" in assistant_msg['content'][:20]:
        print("\n❌ 失败 - assistant 消息不应该自动添加时间戳")
    else:
        print("\n✅ 通过 - assistant 消息正确地没有时间戳")
    
    # 清理
    if os.path.exists(test_db):
        os.remove(test_db)
    
    return 0


def main():
    """运行所有测试"""
    try:
        result1 = test_timestamp_insertion()
        result2 = test_assistant_messages()
        
        if result1 == 0 and result2 == 0:
            print("\n🎉 所有测试通过！时间戳自动插入功能正常工作。")
            return 0
        else:
            print("\n⚠️ 部分测试失败。")
            return 1
    except Exception as e:
        print(f"\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
