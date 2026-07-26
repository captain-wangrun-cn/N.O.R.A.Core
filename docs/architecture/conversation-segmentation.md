# 对话分段系统设计文档 (Conversation Session Segmentation)

> **目标**：当 AI 从 `ONLINE` 进入 `SEMI_ONLINE` 时，将该次在线期间的全部对话封装为一个“对话段落 (Session)”，并提供可检索、可摘要、可复用的段落结构。

---

## 1. 背景与动机

传统的消息历史是连续流，随着时间推移：
- 主题跳转频繁
- 上下文难以回溯
- LLM 难以判断“对话是否已结束”

因此引入**对话分段**，使对话在“逻辑结束点”自动封闭，形成明确的段落结构，增强长期记忆质量与上下文稳定性。

---

## 2. 核心概念

- **Session (对话段落)**：一次完整的在线交流周期
- **活跃对话**：`session_id = NULL` 的消息表示仍属于当前未封闭的会话
- **封闭对话**：进入 `SEMI_ONLINE` 时自动将未分配消息归入新 Session

---

## 3. 触发时机

```
用户发消息 → AI 进入 ONLINE
    ↓
对话进行中（消息 session_id = NULL）
    ↓
用户停止发言 2 分钟
    ↓
对话延续循环判定 END / 追话超限
    ↓
_transition_to_semi_online()
    ↓
close_session() → 归档为一个 Session
    ↓
AI 进入 SEMI_ONLINE
```

---

## 4. 数据库设计

### 4.1 messages 表新增字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `session_id` | INTEGER | 所属对话段落 ID（NULL 表示当前活跃对话） |

### 4.2 新增 conversation_sessions 表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PRIMARY KEY | Session ID |
| `platform` | TEXT | 平台标识 |
| `chat_id` | TEXT | 聊天 ID |
| `started_at` | REAL | 起始时间戳 |
| `ended_at` | REAL | 结束时间戳 |
| `message_count` | INTEGER | 段落消息数 |
| `summary` | TEXT | 段落摘要（异步生成） |
| `trigger_type` | TEXT | 触发来源：user / proactive / alarm |
| `metadata` | TEXT | JSON 格式扩展信息 |

---

## 5. 关键流程与方法

### 5.1 关闭对话段落

调用：

```python
MessageHistory.close_session(platform, chat_id, trigger_type="user")
```

行为：
1. 查找所有 `session_id IS NULL` 的消息
2. 创建 `conversation_sessions` 记录
3. 批量更新消息的 `session_id`
4. 异步生成段落摘要（使用 `summary` 模型）

### 5.2 上下文构建增强

`get_context_messages()` 在跨 Session 的消息之间插入分隔标记：

```
[📋 对话段落分隔 — 以上为之前的对话段落 上段对话摘要: xxx，以下为新的对话段落]
```

目的：帮助 LLM 理解对话的时间结构与主题切换。

---

## 6. 触发源与未来扩展

- 当前：`ONLINE → SEMI_ONLINE` 触发
- 未来可扩展：
  - `/end` 手动结束对话
  - 长时间无消息自动断段
  - 基于语义判定对话结束

---

## 7. 常见注意事项

- `summary` 模型不可用时，段落摘要会静默失败（仅日志记录）
- 旧数据库自动迁移：新增 `session_id` 字段
- `clear_chat_history` / `clear_all_history` 会删除所有 Session

---

## 8. 相关实现文件

- `memory/message_history.py` — 分段逻辑与摘要生成
- `core/controller.py` — 在 `_transition_to_semi_online` 触发 `close_session`
- `core/private_presence_store.py` — 私聊在线状态存储（`ONLINE` / `SEMI_ONLINE`）
- `docs/architecture/message_history.md` — 总体消息历史结构说明

---

**最后更新**：2026-07-26
