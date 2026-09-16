import asyncio
import sqlite3
from pathlib import Path

import pytest
from memory.message_history import (
    MessageHistory,
    get_default_message_history_db,
    DEFAULT_MEMORY_SCOPE_ID,
)


def _make_history(tmp_path: Path, **overrides) -> MessageHistory:
    """构造一个完全隔离在临时目录的 MessageHistory（不触碰生产库）。

    默认 compress_window 调高、消息短，避免触发摘要 LLM 调用。
    """
    params = dict(raw_window=80, compress_window=9999, archive_threshold=99999)
    params.update(overrides)
    return MessageHistory(
        db_path=str(tmp_path / "history.db"),
        mirror_db_path=str(tmp_path / "mirror.db"),
        context_db_path=str(tmp_path / "context.db"),
        **params,
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


# ---------------------------------------------------------------------------
# 摘要块合并（2026-09-15 生产 400 回归）
# ---------------------------------------------------------------------------

def _insert_summaries(history, rows):
    """往 summaries 表插 level<3 的摘要。rows: [(summary_text, message_count, ts), ...]"""
    conn = sqlite3.connect(str(history.db_path))
    cur = conn.cursor()
    for i, (text, count, ts) in enumerate(rows):
        cur.execute(
            """
            INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                                   summary_text, message_count, timestamp, memory_scope_id, place_scope_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("web", "sess_b", 1, i + 1, i + 1, text, count, ts, SCOPE, "web:sess_b"),
        )
    conn.commit()
    conn.close()


def test_summaries_merged_into_single_message(tmp_path):
    """300 条摘要必须合并成**一条**消息。

    历史上这里逐条 append，上下文里会多出几百条独立消息；生产实测载荷
    322 条消息里 315 条是这些摘要。
    """
    history = _make_history(tmp_path)
    _insert_summaries(history, [(f"摘要正文{i}", 10, float(i)) for i in range(300)])

    msgs = history.get_compressed_context_messages("web", "sess_b", memory_scope_id=SCOPE)
    summary_msgs = [m for m in msgs if "摘要正文" in str(m.get("content", ""))]

    assert len(summary_msgs) == 1
    blob = summary_msgs[0]["content"]
    # 内容不能丢：首尾都要在
    assert "摘要正文0" in blob
    assert "摘要正文299" in blob


def test_summary_message_role_is_user_not_system(tmp_path):
    """摘要块的角色必须是 user。

    摘要会被网关映射成上游 provider 的 system 消息；上下文中间夹几百条
    system 会让 Google 侧直接 400 INVALID_ARGUMENT（实测 315 条 system → 400，
    原样改成 user 后 → 200）。
    """
    history = _make_history(tmp_path)
    _insert_summaries(history, [(f"摘要{i}", 5, float(i)) for i in range(60)])

    msgs = history.get_compressed_context_messages("web", "sess_b", memory_scope_id=SCOPE)
    summary_msgs = [m for m in msgs if "摘要0" in str(m.get("content", ""))]

    assert len(summary_msgs) == 1
    assert summary_msgs[0]["role"] == "user"

    # 顺带锁住总量：不管摘要多少条，system 消息数都不该随之增长
    n_system = sum(1 for m in msgs if m.get("role") == "system")
    assert n_system <= 1, f"摘要不应产生 system 消息，实际 {n_system} 条"


def test_get_context_messages_merges_summaries_too(tmp_path):
    """get_context_messages 走的是另一条分支，同样必须合并（两处曾各写一份）。"""
    history = _make_history(tmp_path)
    _insert_summaries(history, [(f"另一处摘要{i}", 3, float(i)) for i in range(120)])

    msgs = history.get_context_messages("web", "sess_b", memory_scope_id=SCOPE)
    summary_msgs = [m for m in msgs if "另一处摘要" in str(m.get("content", ""))]

    assert len(summary_msgs) == 1
    assert summary_msgs[0]["role"] == "user"
    assert "另一处摘要0" in summary_msgs[0]["content"]
    assert "另一处摘要119" in summary_msgs[0]["content"]


def test_no_summaries_means_no_placeholder_message(tmp_path):
    """没有摘要时不能凭空插一条空消息。"""
    history = _make_history(tmp_path)
    history.add_message(platform="web", chat_id="sess_b", role="user", content="只有原文",
                        memory_scope_id=SCOPE, place_scope_id="web:sess_b")

    for msgs in (
        history.get_compressed_context_messages("web", "sess_b", memory_scope_id=SCOPE),
        history.get_context_messages("web", "sess_b", memory_scope_id=SCOPE),
    ):
        assert not any("历史摘要" in str(m.get("content", "")) for m in msgs)


def test_summary_message_survives_bad_timestamp(tmp_path):
    """时间戳异常不能把整块摘要吞掉。

    summaries.timestamp 有 NOT NULL 约束，所以"真实可能"的坏值是存进去的
    非数值字符串，而不是 NULL。
    """
    history = _make_history(tmp_path)
    _insert_summaries(history, [("正文甲", 1, "not-a-number"), ("正文乙", 1, 42.0)])

    msgs = history.get_compressed_context_messages("web", "sess_b", memory_scope_id=SCOPE)
    blob = "\n".join(str(m.get("content", "")) for m in msgs)

    assert "正文甲" in blob
    assert "正文乙" in blob
    assert "未知时间" in blob


# ---------------------------------------------------------------------------
# 归档链（二级压缩）：触发判据 / 滚动合并 / 去重自愈
# ---------------------------------------------------------------------------


class _RecordingSummarizer:
    """记录最后一次提示词，用来断言归档到底喂了什么给 LLM。"""

    def __init__(self, text: str = "归档正文"):
        self.text = text
        self.last_user_prompt = None

    async def chat(self, system_prompt, user_prompt, history):
        self.last_user_prompt = user_prompt
        return self.text


def _insert_summary_row(
    history,
    *,
    level: int,
    text: str,
    count: int,
    ts,
    start,
    end,
    platform: str = "web",
    chat_id: str = "sess_b",
    scope: str = SCOPE,
):
    conn = sqlite3.connect(str(history.db_path))
    conn.execute(
        """
        INSERT INTO summaries (platform, chat_id, level, start_message_id, end_message_id,
                               summary_text, message_count, timestamp, memory_scope_id, place_scope_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (platform, chat_id, level, start, end, text, count, ts, scope, f"{platform}:{chat_id}"),
    )
    conn.commit()
    conn.close()


def _read_summary_rows(history):
    conn = sqlite3.connect(str(history.db_path))
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT level, summary_text, message_count, start_message_id, end_message_id "
            "FROM summaries ORDER BY level, id"
        ).fetchall()
    ]
    conn.close()
    return rows


def _run_check_and_compress(history, platform: str = "web", chat_id: str = "sess_b"):
    """跑一次压缩检查（含结束重试 worker）。

    这里用 asyncio.run 而不是 pytest.mark.asyncio：本仓库的 venv 没装
    pytest-asyncio，async 测试在环境里根本不会被执行。
    """

    async def _run():
        try:
            await history._check_and_compress(platform, chat_id)
        finally:
            await history.stop_retry_worker()

    asyncio.run(_run())


def test_archive_trigger_counts_summarized_messages(tmp_path):
    """归档判据必须是"已经被一级总结消化的消息总量"，不是"未归档消息数"。

    用未归档消息数做判据时，压缩每次排掉 compress_ratio 条、把这个数压回
    compress_window 附近（生产实测稳定在 41~51），archive_threshold(500)
    永远够不到——归档从未执行过，库里攒了 308 条一级总结、0 条归档。
    """
    history = _make_history(tmp_path, compress_window=50, archive_threshold=500)
    history._summarizer = _RecordingSummarizer()  # type: ignore[assignment]

    # 关键是：一条未归档消息都没有（count=0），但已总结的消息量远超阈值
    for i in range(60):
        _insert_summary_row(
            history, level=1, text=f"一级{i}", count=10, ts=float(i),
            start=i * 10 + 1, end=i * 10 + 10,
        )

    _run_check_and_compress(history)

    rows = _read_summary_rows(history)
    assert [r["level"] for r in rows] == [3]
    assert rows[0]["summary_text"] == "归档正文"
    assert rows[0]["message_count"] == 600
    # 归档真跑通了：没有落进重试队列（INSERT 绑定错会走到这里）
    assert history.get_retry_queue_status() == []


def test_archive_heals_duplicate_ranges_in_existing_db(tmp_path):
    """已有库里的并发重复摘要，跑一次归档就自愈：同区间只喂一条给 LLM，重复行删除。

    生产实测 308 条一级总结只对应 217 个不同区间，区间 155-294 堆了 4 条
    （正文各不相同，是独立的 LLM 调用而非复制）。
    """
    history = _make_history(tmp_path, archive_threshold=10)
    summarizer = _RecordingSummarizer()
    history._summarizer = summarizer  # type: ignore[assignment]

    for text in ("短甲", "重复区间里最长的这一条正文", "短乙", "中等长度正文"):
        _insert_summary_row(history, level=1, text=text, count=10, ts=1.0, start=155, end=294)
    _insert_summary_row(history, level=1, text="另一区间", count=10, ts=2.0, start=295, end=310)

    _run_check_and_compress(history)

    prompt = summarizer.last_user_prompt
    assert "重复区间里最长的这一条正文" in prompt
    assert "短甲" not in prompt
    assert "短乙" not in prompt
    assert "中等长度正文" not in prompt
    assert "另一区间" in prompt

    # 4 条重复行一并删掉，只留归档；message_count 按**去重后**的段累计（2 段 × 10）
    rows = _read_summary_rows(history)
    assert [r["level"] for r in rows] == [3]
    assert rows[0]["message_count"] == 20


def test_archive_absorbs_previous_archive(tmp_path):
    """归档是滚动合并：上一轮归档要并进新的一条，而不是各留一条。

    读取侧每个作用域只认一条归档，若每轮另起一条，旧归档就成了永远读不到、
    也删不掉的孤儿。
    """
    history = _make_history(tmp_path, archive_threshold=5)
    summarizer = _RecordingSummarizer()
    history._summarizer = summarizer  # type: ignore[assignment]

    _insert_summary_row(history, level=3, text="上一轮归档", count=1000, ts=1.0, start=1, end=1000)
    _insert_summary_row(history, level=1, text="新增一级", count=10, ts=2.0, start=1001, end=1010)

    _run_check_and_compress(history)

    assert "上一轮归档" in summarizer.last_user_prompt
    assert "新增一级" in summarizer.last_user_prompt

    rows = _read_summary_rows(history)
    assert [r["level"] for r in rows] == [3]
    assert rows[0]["message_count"] == 1010
    assert rows[0]["start_message_id"] == 1
    assert rows[0]["end_message_id"] == 1010


def test_all_archives_are_read_not_just_the_newest(tmp_path):
    """读取侧不能只取最新一条归档。

    原实现是 `ORDER BY timestamp DESC LIMIT 1`；跨平台共享作用域下同一
    memory_scope 会有多个 (platform, chat_id) 分区、各有各的归档，
    LIMIT 1 会让其余的归档永远读不到。
    """
    history = _make_history(tmp_path)
    _insert_summary_row(history, level=3, text="地点A归档", count=100, ts=1.0, start=1, end=100)
    _insert_summary_row(history, level=3, text="地点B归档", count=200, ts=2.0, start=101, end=300)

    msgs = history.get_compressed_context_messages("web", "sess_b", memory_scope_id=SCOPE)
    archive_msgs = [m for m in msgs if "归档" in str(m.get("content", ""))]

    assert len(archive_msgs) == 1  # 仍要合并成一条
    assert archive_msgs[0]["role"] == "system"
    assert "地点A归档" in archive_msgs[0]["content"]
    assert "地点B归档" in archive_msgs[0]["content"]


def test_duplicate_ranges_deduped_in_context(tmp_path):
    """同一区间的重复摘要，读上下文时就该只出现一条。"""
    history = _make_history(tmp_path)
    for text in ("重复区间短正文", "重复区间里最长的这一条正文"):
        _insert_summary_row(history, level=1, text=text, count=10, ts=1.0, start=5, end=20)

    msgs = history.get_compressed_context_messages("web", "sess_b", memory_scope_id=SCOPE)
    hits = [m for m in msgs if "重复区间" in str(m.get("content", ""))]

    assert len(hits) == 1
    assert "重复区间里最长的这一条正文" in hits[0]["content"]
    assert "重复区间短正文" not in hits[0]["content"]


# ---------------------------------------------------------------------------
# 并发闸门 / 归档标记精确性
# ---------------------------------------------------------------------------


class _SlowSummarizer:
    """在 chat 里主动让出事件循环，让并发任务真的交错。"""

    def __init__(self, text: str = "并发摘要"):
        self.text = text
        self.calls = 0

    async def chat(self, system_prompt, user_prompt, history):
        self.calls += 1
        for _ in range(5):
            await asyncio.sleep(0)
        return self.text


def test_concurrent_compression_does_not_duplicate_summaries(tmp_path):
    """并发的压缩任务不能对同一批消息各写一条摘要。

    `add_message` 每来一条消息就 `_launch_background(_check_and_compress)`，而那是
    裸 `loop.create_task`。没有闸门时多个 worker 会同时执行
    `SELECT ... LIMIT compress_ratio`，在任何 commit 之前取到同一批最早未归档消息，
    于是各写一条。生产实测：308 条一级总结只对应 217 个不同区间。
    """
    history = _make_history(tmp_path, compress_window=9999, compress_ratio=10)
    summarizer = _SlowSummarizer()
    history._summarizer = summarizer  # type: ignore[assignment]

    for i in range(20):
        history.add_message("web", "sess_b", "user", f"消息{i}")

    async def _run():
        try:
            await asyncio.gather(
                history._perform_compression("web", "sess_b"),
                history._perform_compression("web", "sess_b"),
                history._perform_compression("web", "sess_b"),
            )
        finally:
            await history.stop_retry_worker()

    asyncio.run(_run())

    assert summarizer.calls == 1, "同一批消息只应触发一次总结调用"
    assert len(_read_summary_rows(history)) == 1


def test_compression_marks_only_selected_ids(tmp_path):
    """标记已归档必须按"实际选中的 id"，不能按 start~end 区间。

    原实现是 `WHERE id >= start AND id <= end`，它只在"id 顺序 == 时间顺序"时才等价于
    精确标记。一旦时间戳乱序（平台乱序投递 / 带历史时间戳回填），区间内**没被总结**的
    消息会被一并标成已归档，从此不再进上下文——静默丢记忆。
    """
    history = _make_history(tmp_path, compress_ratio=10)
    history._summarizer = _RecordingSummarizer("压缩正文")  # type: ignore[assignment]

    ids = [history.add_message("web", "sess_b", "user", f"消息{i}") for i in range(30)]

    # id 1-5 与 26-30 设成最早、6-25 设成最晚 —— 时间顺序与 id 顺序脱钩
    conn = sqlite3.connect(str(history.db_path))
    for idx, mid in enumerate(ids, start=1):
        ts = float(idx) if idx <= 5 or idx >= 26 else 1000.0 + idx
        conn.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (ts, mid))
    conn.commit()
    conn.close()

    async def _run():
        try:
            await history._perform_compression("web", "sess_b")
        finally:
            await history.stop_retry_worker()

    asyncio.run(_run())

    conn = sqlite3.connect(str(history.db_path))
    archived = conn.execute("SELECT COUNT(*) FROM messages WHERE is_archived = 1").fetchone()[0]
    in_between = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE is_archived = 0 AND timestamp > 1000"
    ).fetchone()[0]
    conn.close()

    assert archived == 10, "只应标记真正被总结的那 10 条"
    assert in_between == 20, "区间内没被选中的消息必须保持未归档"

