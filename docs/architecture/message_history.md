# 消息历史管理系统设计文档

## 📋 概述

N.O.R.A.Core 的消息历史管理系统采用**混合存储策略**，通过智能压缩、分层总结和**对话分段**，在保持对话连续性的同时，有效控制存储空间和上下文长度。

## 🎯 设计目标

1. **保持对话连续性** - 让 AI 能够记住长期对话内容
2. **控制上下文长度** - 避免超出 LLM 的 token 限制
3. **保留重要信息** - 关键决策和信息不会丢失
4. **节省存储空间** - 自动清理冗余历史
5. **性能优化** - 快速检索和智能压缩
6. **对话分段** - 按 AI 在线/离线状态自动切割对话段落，便于回溯和上下文理解

## 🏗️ 架构设计

### 四层存储策略

```
┌─────────────────────────────────────────────────────────────┐
│ Level 0: 永久标记 (Pinned Messages)                         │
│  - 用户明确标记的重要消息                                    │
│  - 跨会话永久保留                                            │
│  - 始终出现在上下文中                                        │
│  - 示例: 重要决策、关键信息、用户偏好                        │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Level 1: 原始消息窗口 (Raw Message Window)                  │
│  - 默认保留最近 50 条消息                                    │
│  - 完整保留所有细节（用户消息、AI回复、媒体等）               │
│  - 直接用于构建 LLM 对话上下文                               │
│  - 查询速度最快                                              │
└─────────────────────────────────────────────────────────────┘
                          ↓ (回退路径：超过 50 条触发消息级压缩)
┌─────────────────────────────────────────────────────────────┐
│ Level 2: 压缩总结层 (Compressed Summaries)                  │
│  - 每 10 条原始消息总结为 1 条                               │
│  - 保留关键信息、决策和上下文                                │
│  - 删除冗余细节和重复内容                                    │
│  - 总结由专门的 LLM 模型生成                                 │
└─────────────────────────────────────────────────────────────┘
                          ↓ (超过 500 条触发归档)
┌─────────────────────────────────────────────────────────────┐
│ Level 3: 归档总结层 (Archive Summary)                       │
│  - 将多条一级总结合并为全局总结                              │
│  - 仅保留最重要的主题和背景                                  │
│  - 单条总结描述整个早期对话                                  │
│  - 作为对话的"背景故事"                                      │
└─────────────────────────────────────────────────────────────┘
```

### 数据流程

```
新消息到达
    ↓
添加到数据库 (messages 表)
    ↓
检查消息数量
    ↓
┌─────────────┬─────────────┬─────────────┐
│ < 200 条    │ 200-500 条  │ > 500 条    │
│ 不压缩      │ 触发一级压缩│ 触发归档     │
└─────────────┴─────────────┴─────────────┘
                    ↓              ↓
            一级总结生成      归档总结生成
                    ↓              ↓
            原始消息归档      一级总结删除
```

## � 对话分段系统 (Conversation Session Segmentation)

### 概念

对话分段是基于 AI 在线状态自动切割对话段落的机制。当 AI 从 `ONLINE`（活跃对话中）转为 `SEMI_ONLINE`（空闲待命）时，系统自动将这次在线期间的所有对话封装为一个**对话段落 (Session)**。

### 触发时机

```
用户发消息 → AI 进入 ONLINE 状态
    ↓
对话进行中（消息的 session_id = NULL，表示未分配）
    ↓
用户停止发言 2 分钟
    ↓
AI 追话 / 等待 / 判定结束
    ↓
对话延续循环判定 END 或追话超限
    ↓
_transition_to_semi_online() 触发
    ↓
close_session() — 将所有 session_id=NULL 的消息归入新 session
    ↓
异步生成段落摘要
    ↓
AI 进入 SEMI_ONLINE 状态
```

### 存储结构

- 每条消息新增 `session_id` 字段，指向 `conversation_sessions` 表
- `session_id = NULL` 表示消息属于当前活跃对话（尚未封闭）
- 对话封闭时批量更新 `session_id`

### 上下文构建增强

在构建 LLM 上下文时，如果原始消息跨越不同的对话段落，系统会自动插入段落分隔标记：

```
[📋 对话段落分隔 — 以上为之前的对话段落 上段对话摘要: xxx，以下为新的对话段落]
```

这帮助 LLM 理解对话的时间结构和主题切换。

## �📊 数据库设计

### messages 表 (原始消息)

| 字段        | 类型                | 说明                            |
| ----------- | ------------------- | ------------------------------- |
| id          | INTEGER PRIMARY KEY | 自增主键                        |
| platform    | TEXT                | 平台标识 (telegram, discord 等)，**来源地点标签** |
| chat_id     | TEXT                | 聊天 ID（= `storage_id`，兼容期来源标签）        |
| user_id     | TEXT                | 用户 ID                         |
| role        | TEXT                | 'user', 'assistant', 'system'   |
| content     | TEXT                | 消息内容                        |
| timestamp   | REAL                | 时间戳                          |
| metadata    | TEXT                | JSON 格式额外信息               |
| is_pinned   | INTEGER             | 是否永久标记 (0/1)              |
| is_archived | INTEGER             | 是否已归档 (0/1)                |
| session_id  | INTEGER             | 所属对话段落 ID (NULL=当前活跃) |
| memory_scope_id | TEXT            | **共享上下文主键**（跨平台接力）：历史读取按它分区；缺省 `relationship:owner:default` |
| place_scope_id  | TEXT            | 来源地点 `{platform}:{chat_id}`，用于回查某平台/窗口原始上下文 |
| actor_display_name | TEXT         | 说话人昵称，注入上下文时打来源标签 `[telegram:123 / 张三]` |

> **主键语义（跨平台接力后）**：读取共享历史的主键是 `memory_scope_id`，不再是 `(platform, chat_id)`。
> `platform`/`chat_id`/`storage_id` 退化为**来源地点标签**与兼容期 fallback —— 旧行迁移时 `memory_scope_id`
> 统一补 `relationship:owner:default`、`place_scope_id` 由旧 `platform/chat_id` 生成。详见
> [跨平台接力与五层身份模型](./cross-platform-relay.md)。

### summaries 表 (压缩总结)

| 字段             | 类型                | 说明                   |
| ---------------- | ------------------- | ---------------------- |
| id               | INTEGER PRIMARY KEY | 自增主键               |
| platform         | TEXT                | 平台标识               |
| chat_id          | TEXT                | 聊天 ID                |
| level            | INTEGER             | 1=一级总结, 3=归档总结 |
| start_message_id | INTEGER             | 总结的起始消息 ID      |
| end_message_id   | INTEGER             | 总结的结束消息 ID      |
| summary_text     | TEXT                | 总结内容               |
| message_count    | INTEGER             | 总结了多少条消息       |
| timestamp        | REAL                | 时间戳                 |

### conversation_sessions 表 (对话段落)

| 字段          | 类型                | 说明                                           |
| ------------- | ------------------- | ---------------------------------------------- |
| id            | INTEGER PRIMARY KEY | 自增主键 (即 session_id)                       |
| platform      | TEXT                | 平台标识                                       |
| chat_id       | TEXT                | 聊天 ID                                        |
| started_at    | REAL                | 本段对话起始时间戳                             |
| ended_at      | REAL                | 本段对话结束时间戳                             |
| message_count | INTEGER             | 本段消息总数                                   |
| summary       | TEXT                | 本段对话摘要 (异步生成，可能为 NULL)           |
| trigger_type  | TEXT                | 触发来源: 'user', 'proactive', 'alarm'         |
| metadata      | TEXT                | JSON 格式额外信息                              |
| memory_scope_id | TEXT              | 共享对话作用域：`close_session` 按它关闭**连续对话**，而非按单平台孤立关闭 |

### compression_retry_queue 表（压缩失败重试队列）

| 字段            | 类型                | 说明 |
| --------------- | ------------------- | ---- |
| id              | INTEGER PRIMARY KEY | 自增主键 |
| platform        | TEXT                | 平台标识 |
| chat_id         | TEXT                | 聊天 ID |
| task_type       | TEXT                | 任务类型：`compress` / `archive` / `session_summary` / `context_refresh` |
| payload         | TEXT                | JSON 参数；例如 `session_summary` 会记录 `session_id` |
| attempts        | INTEGER             | 当前已尝试次数 |
| next_retry_at   | REAL                | 下一次补偿执行时间戳 |
| last_error      | TEXT                | 最近一次失败原因 |
| status          | TEXT                | `pending` / `dead` |
| created_at      | TIMESTAMP           | 创建时间 |
| updated_at      | TIMESTAMP           | 最近更新时间 |

## 🔄 压缩算法

### 一级压缩 (Level 1 Compression，兼容回退路径)

**触发条件**: 原始消息数 > `compress_window` (默认 50)

**压缩流程**:

1. 选取最早的 N 条未归档消息 (N = `compress_ratio`, 默认 10)
2. 构建总结提示词，要求保留关键信息、重要决策和上下文
3. 调用 `summary` 模型生成总结
4. 保存总结到 `summaries` 表 (level=1)
5. 标记原始消息为已归档 (`is_archived=1`)

### 归档压缩 (Archive Compression)

**触发条件**: 原始消息数 > `archive_threshold` (默认 500)

**压缩流程**:

1. 获取所有一级总结 (level=1)
2. 构建归档总结提示词，要求保留最重要的主题、决策和背景
3. 调用 `summary` 模型生成全局总结
4. 保存归档总结到 `summaries` 表 (level=3)
5. 删除已归档的一级总结

## ♻️ 压缩失败补偿机制（方案 B）

### 目标

避免“压缩请求失败后只能等用户再说一句，才有机会顺带重试”的问题。

现在的策略是：**失败任务持久化落库 + 后台 worker 周期补偿**。

### 覆盖的失败任务类型

1. `compress`：旧消息级一级压缩失败
2. `archive`：旧消息级归档总结失败
3. `session_summary`：对话段摘要生成失败
4. `context_refresh`：段级滑动压缩刷新失败

### 失败时会发生什么

当上述任务任一失败时：

1. 在 `compression_retry_queue` 中插入或更新一条记录
2. `attempts += 1`
3. 根据指数退避计算 `next_retry_at`
4. 若达到最大尝试次数，则标记为 `dead`

### 成功时会发生什么

对应任务成功后，会自动删除该条重试记录（出队）。

### 后台 worker 行为

- `MessageHistory` 在有事件循环时会启动后台 worker
- worker 按固定间隔扫描 `compression_retry_queue`
- 所有 `status='pending'` 且 `next_retry_at <= now` 的任务都会被重新执行
- 因为重试记录在 `message_history.db` 中持久化，所以**重启后仍可继续补偿**

### 退避策略

默认：

- 基础延迟：60 秒
- 退避方式：指数退避
- 最大尝试次数：8 次
- 扫描间隔：300 秒

### 设计取舍

- 优点：失败不会丢，重启后能续跑，不依赖“下一条消息碰巧触发”
- 缺点：增加一张表和一个后台任务，且 `dead` 任务需要人工关注日志或后续补充管理入口

## 🔍 上下文构建逻辑

获取上下文时的消息优先级和顺序:

1. **永久标记的消息** — 带 📌 标记，始终出现
2. **归档总结** (如果存在) — 带 📚 标记，描述早期对话
3. **一级总结** (如果存在) — 带 💬 标记，近期摘要
4. **原始消息** (最近的) — 完整保留的最新对话

所有消息按时间排序后返回给 LLM。

## ⚙️ 配置参数

在 `config.yml` 中的 `memory.message_history` 部分配置:

| 参数                | 默认值 | 说明                   |
| ------------------- | ------ | ---------------------- |
| `raw_window`        | 50     | 完整保留的原始消息数量 |
| `compress_window`   | 50     | 超过此数量触发一级压缩（消息数量阈值） |
| `compress_ratio`    | 10     | 每N条消息压缩为1条总结 |
| `archive_threshold` | 500    | 超过此数量创建归档总结 |
| `long_message_threshold` | 1200 | 段级压缩中判定“过长”的字符阈值 |

代码中还存在以下内部默认值（当前未暴露到 `config.yml`）：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `retry_base_delay_seconds` | `60` | 重试基础延迟 |
| `retry_max_attempts` | `8` | 单任务最大重试次数 |
| `retry_scan_interval_seconds` | `300` | 后台扫描待补偿任务的间隔 |

同时需要在 `llm.models` 下配置 `summary` 模型（推荐使用快速模型以降低成本）。

### 参数调优建议

- **对话频繁的场景** (如客服机器人): 减小各窗口值，加快压缩频率
- **长期记忆场景** (如个人助手): 增大各窗口值，保留更多细节
- **资源受限场景**: 全面缩小参数，降低 LLM 调用频率

## 🎭 总结模型选择

推荐使用快速、低成本的模型作为总结模型，原因:

1. **总结任务相对简单** — 不需要复杂推理
2. **调用频繁** — 每 N 条消息触发一次
3. **成本考虑** — 快速模型更经济
4. **响应速度** — 不阻塞主对话流程

## 📈 性能特性

### 存储效率

假设平均每条消息 200 字符:

| 阶段     | 消息数 | 原始大小 | 压缩后大小 | 压缩率 |
| -------- | ------ | -------- | ---------- | ------ |
| 原始     | 50     | 10 KB    | 10 KB      | 100%   |
| 一级压缩 | 200    | 40 KB    | ~15 KB     | 37.5%  |
| 归档压缩 | 500    | 100 KB   | ~5 KB      | 5%     |

### 查询性能

- 获取上下文: **< 10ms** (SQLite 索引优化)
- 压缩操作: **异步执行**，不阻塞主流程
- 归档操作: **异步执行**，不影响用户体验

## 📝 最佳实践

### DO ✅

- 为关键信息使用 `pin_message()` 永久保留
- 定期查看统计信息，关注压缩效果
- 根据使用场景调整参数
- 在测试环境验证压缩效果

### DON'T ❌

- 不要过度依赖总结（可能丢失细节）
- 不要设置过小的 `raw_window`
- 不要频繁清除历史
- 不要在生产环境直接修改数据库

## 🆘 故障排查

### 问题: 总结质量差

- 检查 `summary` 模型配置是否正确
- 增大 `compress_ratio` 减少压缩频率
- 总结提示词在 `memory/message_history.py` 中可调整

### 问题: 上下文过长

- 减小 `raw_window`
- 降低 `compress_window`
- 增加 `compress_ratio`

### 问题: 重要信息丢失

- 使用 `pin_message()` 标记重要消息
- 增大 `raw_window`
- 检查总结提示词是否强调保留关键信息

### 问题: 压缩一直没补成功

- 检查 `compression_retry_queue` 中是否已有对应任务且状态变为 `dead`
- 检查 `summary` 模型是否持续不可用
- 检查后台 worker 是否已在运行（需有事件循环）
- 若是 `context_refresh`，同时检查 `context_compression.db` 是否可写

---

**版本**: 1.0.0  
**最后更新**: 2025-02-08
