import asyncio
import sqlite3
from pathlib import Path

import pytest
from memory.message_history import (
    MessageHistory,
    get_default_message_history_db,
    DEFAULT_MEMORY_SCOPE_ID,
)


def _make_history(tmp_path: Path) -> MessageHistory:
    """构造一个完全隔离在临时目录的 MessageHistory（不触碰生产库）。

    compress_window 调高、消息短，避免触发摘要 LLM 调用。
    """
    return MessageHistory(
        db_path=str(tmp_path / "history.db"),
        mirror_db_path=str(tmp_path / "mirror.db"),
        context_db_path=str(tmp_path / "context.db"),
        raw_window=80,
        compress_window=9999,
        archive_threshold=99999,
    )

async def _async_test_message_history():
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
    asyncio.run(_async_test_message_history())
    
    # 运行集成示例
    asyncio.run(demo_integration())
    
    print("\n💡 提示:")
    default_db = get_default_message_history_db()
    print(f"  - 数据库位于: {default_db}")
    print("  - 可以使用 SQLite 客户端查看数据")
    print("  - 压缩和归档是自动异步执行的")
    print("  - 重要消息可以通过 pin_message() 永久保留")


def test_message_history():
    """同步包装以便 pytest 无需异步插件即可运行。"""
    asyncio.run(_async_test_message_history())


# ======================================================================
# Phase 2 — 跨平台共享上下文 + 数据迁移
# ======================================================================

SCOPE = DEFAULT_MEMORY_SCOPE_ID


def test_cross_platform_shared_read(tmp_path):
    """A 平台写入 → B 平台用同一 memory_scope_id 读取应能看到。"""
    history = _make_history(tmp_path)

    # Telegram（私聊，storage_id=用户id）写一条
    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="我在 Telegram 说的话",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a", actor_display_name="主人",
    )
    # Web（不同平台/不同 chat_id）写一条，同一作用域
    history.add_message(
        platform="web", chat_id="sess_b", role="user", content="我在 Web 说的话",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b", actor_display_name="主人",
    )

    # 从 Web 端按共享作用域读 → 两条都应出现
    msgs = history.get_context_messages(
        "web", "sess_b", memory_scope_id=SCOPE, current_place_scope_id="web:sess_b",
    )
    contents = " ".join(m["content"] for m in msgs)
    assert "我在 Telegram 说的话" in contents
    assert "我在 Web 说的话" in contents

    # 对照：不传 scope 的旧式读取只看到本平台那条
    legacy = history.get_context_messages("web", "sess_b")
    legacy_contents = " ".join(m["content"] for m in legacy)
    assert "我在 Web 说的话" in legacy_contents
    assert "我在 Telegram 说的话" not in legacy_contents


def test_source_label_only_for_foreign_place(tmp_path):
    """共享读取时，仅对“非当前地点”的消息打来源标签，当前地点消息保持原样。"""
    history = _make_history(tmp_path)
    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="远端消息",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a", actor_display_name="张三",
    )
    history.add_message(
        platform="web", chat_id="sess_b", role="user", content="本地消息",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b", actor_display_name="主人",
    )

    msgs = history.get_context_messages(
        "web", "sess_b", memory_scope_id=SCOPE, current_place_scope_id="web:sess_b",
    )
    by_place = {m.get("place_scope_id"): m["content"] for m in msgs if m.get("place_scope_id")}
    # 远端（telegram）消息带来源标签
    assert "[来自 telegram:user_a / 张三]" in by_place["telegram:user_a"]
    # 本地（web，当前地点）消息不带来源标签
    assert "[来自" not in by_place["web:sess_b"]


def test_current_segment_context_messages_are_active_only_and_labeled(tmp_path):
    """当前段上下文只包含 session_id IS NULL 的未压缩消息，并保留跨地点来源标签。"""
    history = _make_history(tmp_path)
    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="旧段消息",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a", actor_display_name="张三",
    )
    session_id = history.close_session(
        platform="telegram", chat_id="user_a", trigger_type="user", memory_scope_id=SCOPE,
    )
    assert session_id is not None

    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="远端当前段",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a", actor_display_name="张三",
    )
    history.add_message(
        platform="web", chat_id="sess_b", role="assistant", content="本地当前段",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b", actor_display_name="主人",
    )

    msgs = history.get_current_segment_context_messages(
        "web", "sess_b", memory_scope_id=SCOPE, current_place_scope_id="web:sess_b",
    )

    contents = [m["content"] for m in msgs]
    assert len(msgs) == 2
    assert not any("旧段消息" in c for c in contents)
    assert "[来自 telegram:user_a / 张三]" in contents[0]
    assert "远端当前段" in contents[0]
    assert "[来自" not in contents[1]
    assert "本地当前段" in contents[1]


def test_close_session_refreshes_context_compressor(tmp_path):
    """关闭段落后应刷新滑动上下文，避免前脑下一轮读到过期 slot。"""
    history = _make_history(tmp_path)
    calls = []
    original_refresh = history._schedule_context_refresh

    def spy_refresh(platform, chat_id, memory_scope_id=None):
        calls.append((platform, chat_id, memory_scope_id))
        return original_refresh(platform, chat_id, memory_scope_id)

    history._schedule_context_refresh = spy_refresh

    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="上一段消息",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a",
    )
    calls.clear()

    session_id = history.close_session(
        platform="telegram", chat_id="user_a", trigger_type="user", memory_scope_id=SCOPE,
    )

    assert session_id is not None
    assert calls == [("telegram", "user_a", SCOPE)]


def test_compressed_context_excludes_active_segment_raw_window(tmp_path):
    """压缩上下文 helper 不应夹带当前活跃段原文或活跃段 slot 快照。"""
    history = _make_history(tmp_path)
    history.add_message(
        platform="web", chat_id="sess_b", role="user", content="当前活跃原文",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b",
    )

    conn = sqlite3.connect(str(history.db_path))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                               summary_text, message_count, timestamp, memory_scope_id, place_scope_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("web", "sess_b", 1, 1, 1, "历史摘要", 1, 10.0, SCOPE, "web:sess_b"),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(history.context_compressor.db_path))
    cur = conn.cursor()
    cur.execute("DELETE FROM context_segments WHERE platform = ? AND chat_id = ?", ("__scope__", SCOPE))
    cur.execute(
        """
        INSERT INTO context_segments
            (platform, chat_id, slot, segment_type, role, content, message_ids,
             source_timestamp, updated_at, memory_scope_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("__scope__", SCOPE, 1, "raw_recent_segment", "system", "当前活跃段快照", "[]", 20.0, 20.0, SCOPE),
    )
    cur.execute(
        """
        INSERT INTO context_segments
            (platform, chat_id, slot, segment_type, role, content, message_ids,
             source_timestamp, updated_at, memory_scope_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("__scope__", SCOPE, 2, "compressed_single_segment", "system", "历史压缩段", "[]", 5.0, 20.0, SCOPE),
    )
    conn.commit()
    conn.close()

    compressed = history.get_compressed_context_messages(
        "web", "sess_b", memory_scope_id=SCOPE, current_place_scope_id="web:sess_b",
    )
    contents = "\n".join(m["content"] for m in compressed)

    assert "历史摘要" in contents
    assert "历史压缩段" in contents
    assert "当前活跃原文" not in contents
    assert "当前活跃段快照" not in contents


def test_forebrain_context_is_compressed_plus_current_segment(tmp_path):
    """前脑上下文应由历史压缩部分和当前活跃段原文拼接而成。"""
    history = _make_history(tmp_path)
    history.add_message(
        platform="web", chat_id="sess_b", role="user", content="当前段消息",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b",
    )

    conn = sqlite3.connect(str(history.db_path))
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                               summary_text, message_count, timestamp, memory_scope_id, place_scope_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("web", "sess_b", 1, 1, 1, "前脑可见历史摘要", 1, 10.0, SCOPE, "web:sess_b"),
    )
    conn.commit()
    conn.close()

    msgs = history.get_forebrain_context_messages(
        "web", "sess_b", memory_scope_id=SCOPE, current_place_scope_id="web:sess_b",
    )
    contents = [m["content"] for m in msgs]

    assert any("前脑可见历史摘要" in c for c in contents)
    assert any("当前段消息" in c for c in contents)
    assert contents[-1].endswith("当前段消息")


def test_legacy_db_migration_backfills_scope(tmp_path):
    """旧库（无作用域列）启动迁移后，旧行应归入默认共享作用域、place 由 platform/chat_id 生成。"""
    db_path = tmp_path / "legacy.db"

    # 1) 手工建一个“旧版” messages 表（没有作用域列），插入一行旧数据
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL, chat_id TEXT NOT NULL, user_id TEXT,
            role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL,
            metadata TEXT, is_pinned INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0,
            session_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        "INSERT INTO messages (platform, chat_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("telegram", "old_chat", "user", "[2026-01-01 09:00] 旧的历史消息", 1735707600.0),
    )
    conn.commit()
    conn.close()

    # 2) 用 MessageHistory 打开（触发自动迁移）
    history = MessageHistory(
        db_path=str(db_path),
        mirror_db_path=str(tmp_path / "m.db"),
        context_db_path=str(tmp_path / "c.db"),
        compress_window=9999, archive_threshold=99999,
    )

    # 3) 旧行应已回填作用域
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT memory_scope_id, place_scope_id FROM messages WHERE chat_id='old_chat'").fetchone()
    conn.close()
    assert row["memory_scope_id"] == SCOPE
    assert row["place_scope_id"] == "telegram:old_chat"

    # 4) 旧历史能通过共享作用域读取到
    msgs = history.get_context_messages(
        "web", "new_sess", memory_scope_id=SCOPE, current_place_scope_id="web:new_sess",
    )
    assert any("旧的历史消息" in m["content"] for m in msgs)


def test_close_session_spans_scope(tmp_path):
    """按共享作用域关闭对话段落，应把所有平台的活跃消息纳入同一 session。"""
    history = _make_history(tmp_path)
    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="tg 消息",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a",
    )
    history.add_message(
        platform="web", chat_id="sess_b", role="assistant", content="web 回复",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b",
    )

    session_id = history.close_session(
        platform="telegram", chat_id="user_a", trigger_type="user", memory_scope_id=SCOPE,
    )
    assert session_id is not None

    # 两个平台的消息都应被打上同一个 session_id（即跨平台合并为一段连续对话）
    conn = sqlite3.connect(str(history.db_path))
    rows = conn.execute("SELECT platform, session_id FROM messages").fetchall()
    conn.close()
    assert all(r[1] == session_id for r in rows), rows
    assert {r[0] for r in rows} == {"telegram", "web"}

    # 关闭后当前活跃段（按 scope）应为空
    active = history.get_current_segment_messages("telegram", "user_a", memory_scope_id=SCOPE)
    assert active == []


def test_clear_chat_history_clears_whole_scope(tmp_path):
    """clear_chat_history 传入 scope 时清空整个共享作用域（所有平台）。"""
    history = _make_history(tmp_path)
    history.add_message(
        platform="telegram", chat_id="user_a", role="user", content="tg",
        memory_scope_id=SCOPE, place_scope_id="telegram:user_a",
    )
    history.add_message(
        platform="web", chat_id="sess_b", role="user", content="web",
        memory_scope_id=SCOPE, place_scope_id="web:sess_b",
    )

    history.clear_chat_history("telegram", "user_a", keep_pinned=False, memory_scope_id=SCOPE)

    conn = sqlite3.connect(str(history.db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM messages WHERE memory_scope_id=?", (SCOPE,)).fetchone()[0]
    conn.close()
    assert remaining == 0


def test_legacy_add_message_defaults_to_shared_scope(tmp_path):
    """不传作用域的旧式 add_message，应默认归入共享作用域，从而天然跨平台可读。"""
    history = _make_history(tmp_path)
    # 旧式调用（无 scope 参数）
    history.add_message(platform="telegram", chat_id="user_a", role="user", content="无scope旧式写入")

    conn = sqlite3.connect(str(history.db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT memory_scope_id, place_scope_id FROM messages").fetchone()
    conn.close()
    assert row["memory_scope_id"] == SCOPE
    assert row["place_scope_id"] == "telegram:user_a"

