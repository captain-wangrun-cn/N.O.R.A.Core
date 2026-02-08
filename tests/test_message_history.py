import asyncio
from memory.message_history import MessageHistory


async def test_message_history():
    """测试消息历史管理"""
    
    # 1. 初始化历史管理器
    history = MessageHistory(
        db_path="memory/test_history.db",
        raw_window=10,           # 测试用小窗口
        compress_window=20,      # 测试用小阈值
        compress_ratio=5,        # 5条压缩为1条
        archive_threshold=50
    )
    
    print("=" * 60)
    print("消息历史管理系统测试")
    print("=" * 60)
    
    # 2. 清空测试数据
    history.clear_chat_history("telegram", "test_chat", keep_pinned=False)
    print("\n✅ 清空测试数据")
    
    # 3. 添加一些测试消息
    print("\n📝 添加 30 条测试消息...")
    for i in range(30):
        history.add_message(
            platform="telegram",
            chat_id="test_chat",
            role="user" if i % 2 == 0 else "assistant",
            content=f"这是第 {i+1} 条测试消息。",
            user_id="123456"
        )
    
    # 等待异步压缩完成
    await asyncio.sleep(2)
    
    # 4. 查看统计信息
    stats = history.get_statistics("telegram", "test_chat")
    print("\n📊 消息统计:")
    print(f"  - 原始消息: {stats['raw_messages']} 条")
    print(f"  - 已归档消息: {stats['archived_messages']} 条")
    print(f"  - 永久标记: {stats['pinned_messages']} 条")
    print(f"  - 一级总结: {stats['level1_summaries']} 条")
    print(f"  - 归档总结: {stats['archive_summaries']} 条")
    
    # 5. 标记重要消息
    print("\n📌 标记消息 #5 为永久保留")
    history.pin_message(5)
    
    # 6. 获取上下文消息
    context = history.get_context_messages("telegram", "test_chat", limit=15)
    print(f"\n💬 获取上下文消息: {len(context)} 条")
    print("\n消息列表:")
    for i, msg in enumerate(context, 1):
        role_icon = {"user": "👤", "assistant": "🤖", "system": "📋"}.get(msg["role"], "❓")
        content_preview = msg["content"][:50] + "..." if len(msg["content"]) > 50 else msg["content"]
        print(f"  {i}. {role_icon} {msg['role']:10s} | {content_preview}")
    
    # 7. 继续添加更多消息触发归档
    print("\n📝 添加更多消息以触发归档...")
    for i in range(30, 60):
        history.add_message(
            platform="telegram",
            chat_id="test_chat",
            role="user" if i % 2 == 0 else "assistant",
            content=f"这是第 {i+1} 条测试消息，测试归档功能。"
        )
    
    # 等待归档完成
    await asyncio.sleep(3)
    
    # 8. 再次查看统计
    stats = history.get_statistics("telegram", "test_chat")
    print("\n📊 最终统计:")
    print(f"  - 原始消息: {stats['raw_messages']} 条")
    print(f"  - 已归档消息: {stats['archived_messages']} 条")
    print(f"  - 永久标记: {stats['pinned_messages']} 条")
    print(f"  - 一级总结: {stats['level1_summaries']} 条")
    print(f"  - 归档总结: {stats['archive_summaries']} 条")
    
    # 9. 获取最终上下文
    final_context = history.get_context_messages("telegram", "test_chat")
    print(f"\n💬 最终上下文: {len(final_context)} 条消息")
    
    print("\n" + "=" * 60)
    print("✅ 测试完成！")
    print("=" * 60)


async def demo_integration():
    """演示如何在实际代码中集成"""
    
    print("\n" + "=" * 60)
    print("集成示例")
    print("=" * 60)
    
    # 在 Telegram 适配器中的使用
    history = MessageHistory()
    
    # 用户发送消息
    user_message = "帮我写一个Python函数"
    history.add_message(
        platform="telegram",
        chat_id="123456789",
        role="user",
        content=user_message,
        metadata={"message_id": "msg_001"}
    )
    
    # 获取对话上下文用于 LLM
    context = history.get_context_messages(
        platform="telegram",
        chat_id="123456789",
        limit=50  # 最多获取50条原始消息
    )
    
    # 转换为 LLM 格式
    llm_messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in context
    ]
    
    print(f"\n📤 发送给 LLM 的上下文: {len(llm_messages)} 条消息")
    
    # 模拟 LLM 回复
    assistant_reply = "这是一个示例 Python 函数..."
    history.add_message(
        platform="telegram",
        chat_id="123456789",
        role="assistant",
        content=assistant_reply
    )
    
    print("✅ 对话已保存到历史记录")
    
    # 查看统计
    stats = history.get_statistics("telegram", "123456789")
    print(f"\n📊 当前统计: {stats}")


if __name__ == "__main__":
    print("""
    ╔════════════════════════════════════════════════════════╗
    ║  N.O.R.A. Core - 消息历史管理系统测试                  ║
    ╚════════════════════════════════════════════════════════╝
    """)
    
    # 运行测试
    asyncio.run(test_message_history())
    
    # 运行集成示例
    asyncio.run(demo_integration())
    
    print("\n💡 提示:")
    print("  - 数据库位于: memory/message_history.db")
    print("  - 可以使用 SQLite 客户端查看数据")
    print("  - 压缩和归档是自动异步执行的")
    print("  - 重要消息可以通过 pin_message() 永久保留")
