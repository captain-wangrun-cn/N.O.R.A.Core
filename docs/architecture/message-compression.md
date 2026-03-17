# 消息压缩 (Message Compression)

## 概述

当前压缩系统采用**双轨策略**：

1. **主路径（推荐）**：按对话段（Session）进行滑动压缩，产物写入独立上下文库。
2. **回退路径（兼容）**：历史的消息级压缩（`summaries` 表）继续保留，以便旧流程与统计兼容。

二者共同目标：在不丢失关键上下文的前提下，降低传给 LLM 的上下文体积与成本。

---

## 核心原则

- 压缩单位优先是**消息段**（`conversation_sessions` + 当前活跃段）。
- 所有自动摘要均优先使用 `summary` 模型。
- 原文与压缩上下文分库保存，重启后可恢复。
- 最近对话优先保真，远期对话逐级摘要。

---

## 数据层设计

### 1) 原始历史库（主库）

文件：`workspace/data/memory/message_history.db`

主要表：
- `messages`：原始消息（含 `session_id`）
- `conversation_sessions`：封闭对话段
- `summaries`：兼容旧消息级压缩的摘要

### 2) 消息镜像库（独立）

文件：`workspace/data/memory/message_log.db`

主要表：
- `raw_messages`：用户/AI 原文镜像（完整保留）

### 3) 上下文压缩库（独立）

文件：`workspace/data/memory/context_compression.db`

主要表：
- `context_segments`：按槽位保存滑动窗口段摘要/原文段

---

## 按段滑动压缩策略（主路径）

按“最近到更早”的段位（slot）处理：

1. **slot 1~3（最新三段）**
    - 默认完整保留。
    - 若段内容超过 `long_message_threshold`（字符阈值），该段单独摘要。

2. **slot 4~6**
    - 每段分别单独摘要。

3. **slot 7~10**
    - 合并为一组生成一个摘要。

4. **新段到来时**
    - 重新计算滑动窗口；由于使用段级 `source_key` 缓存，未变化段不会重复调用总结模型。

> 注：当前通过段级键缓存实现“只重压缩受影响窗口”的效果。

---

## 触发机制

### A. 段压缩刷新（主路径）

每次 `add_message()` 后异步触发 `ContextCompressor.refresh_context()`：

- 从 `message_history.db` 读取：
  - 当前活跃段（`session_id IS NULL`）
  - 最近封闭段（`conversation_sessions`）
- 计算 slot 1~10 的压缩结果
- 写入 `context_compression.db`

### B. 消息级压缩检查（回退路径）

`_check_and_compress()` 仍保留：

- 当未归档消息数 > `compress_window` 时触发一级压缩。
- 当消息数 > `archive_threshold` 时触发归档总结。

这一路径主要用于兼容旧逻辑与历史数据，不再是推荐上下文主来源。

---

## 上下文读取优先级

`MessageHistory.get_context_messages()` 顺序：

1. `Pinned` 永久消息
2. 归档总结（`level=3`）
3. **优先尝试 `context_compression.db` 的段压缩结果**
4. 若无段压缩结果，回退到旧 `summaries` + 原始消息窗口

---

## 配置项

配置位置：`config.yml` → `memory.message_history`

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `raw_window` | `50` | 回退路径中原始消息窗口大小 |
| `compress_window` | `50` | **消息数量阈值**（旧消息级压缩触发线） |
| `compress_ratio` | `10` | 回退路径一级压缩比例 |
| `archive_threshold` | `500` | 回退路径归档阈值 |
| `long_message_threshold` | `1200` | 段级“过长提前压缩”的**字符阈值** |
| `mirror_db_path` | `null` | 镜像库路径（空则使用默认） |
| `context_db_path` | `null` | 压缩上下文库路径（空则使用默认） |

> 重要：`compress_window` 是“消息条数”，不是 token 数。

---

## 模型要求

- 段压缩与回退压缩都使用 `summary` 模型。
- 若 `summary` 调用失败，系统会使用回退摘要（截断文本）以保证主流程可用。

---

## 相关实现文件

- `memory/context_store.py`：段压缩与独立上下文库存取
- `memory/message_history.py`：主库写入、压缩触发与上下文组装
- `core/scheduler_mixin.py`：`ONLINE → SEMI_ONLINE` 时封段（`close_session`）
- `docs/architecture/conversation-segmentation.md`：段切分机制说明

---

## 已知限制

- 长段阈值当前按字符长度计算，尚非 tokenizer 精确 token。
- 消息级回退压缩仍存在，后续可视情况进一步简化为纯段压缩。

---

**最后更新**：2026-03-17
