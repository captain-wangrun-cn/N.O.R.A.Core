# 群聊独立监听架构

## 目标与边界

群聊监听把“认知范围”和“运行状态”拆开：

- **共享认知域**继续使用 `ConversationIdentity.memory_scope_id`，消息历史、RAG、压缩、段落摘要和既有后脑协调不按群拆分。
- **群运行域**按 `runtime_key = platform:platform_chat_id` 隔离，只保存 `ONLINE / SEMI_ONLINE`、待判断窗口、定时器和生成中断状态。
- **场景可见性**仍由 `place_scope_id`、平台、群 ID、说话者、消息 ID 和回复关系约束。共享背景不代表其它场景内容可以在当前群公开。

群模式持久化在 `data/group_presence_state.json`。只持久化模式，不持久化窗口和异步任务；重启后消息事实仍来自共享 `MessageHistory`。

## Adapter 入口

Telegram 与 OneBot v11 都先完成平台事件标准化，并明确提供：

- `explicit_bot_mention`
- `reply_to_bot`
- `platform_message_id(s)`
- sender 与 reply metadata

`SEMI_ONLINE` 只接受显式 @ 与回复 Nora 的消息，继续走原有 3 秒 `MessageAggregator`。`ONLINE` 接受所有群事件，绕过 sender aggregator，逐条直达 `NoraController.submit_group_message()`；Telegram 相册仍作为一个带多个平台消息 ID 的原子事件。

## 被动监听状态机

每个群维护最多 20 条的 `pending_window`。ONLINE 消息一到达就写入共享历史，窗口截断不会删除历史。

- 自上次判断后累计 3 条：立即调用 fast。
- 最后消息后 180 秒且窗口非空：调用 fast。
- `KEEP_LISTENING`：保留完整窗口，下一批继续合并判断。
- `REPLY`：提升完整快照到现有 smart/front-brain 流水线，并通过 `_message_saved=True` 防止重复入历史。
- `SEMI_ONLINE`：只切换当前群并清空其运行窗口。

fast 判断使用序号快照；判断期间新到的 suffix 不会因旧结果而丢失。解析失败、超时和异常分别保守回退到 `KEEP_LISTENING` 或 `KEEP_PENDING`。

## 生成中断

一个 smart 回复对应一个“逻辑生成周期”，由 generation token 防止已取消任务迟到提交结果。

- 生成期间普通消息每累计 3 条，fast 判断 `APPEND / KEEP_PENDING`。
- 每个逻辑周期最多一次普通 `APPEND` 中断；重启不重置额度。
- `KEEP_PENDING` 和额度用完后的消息回到同一 passive window，当前生成结束后继续判断。
- 只有 `explicit_bot_mention=True` 立即中断，不消耗普通额度；reply-only 仍按普通三条规则处理。
- @ replacement 合并旧输入、可靠回复目标、@ 消息及 cancellation/handoff 期间到达的消息，按平台与群本地消息 ID 去重并保持顺序。

后脑工具任务沿用现有共享 scope 队列；普通群聊插话不会任意取消后脑工具执行。

## 模型标记

- `[ONLINE]`：群聊场景只把当前群切到 ONLINE。
- `[SEMI_ONLINE]`：群聊场景只把当前群切到半在线；私聊保持既有全局/段落语义。
- 两者冲突时优先更保守的 `[SEMI_ONLINE]`。
- 标记始终在用户可见输出前剥离。

单个群进入半在线不等于关闭共享 `memory_scope_id` 的段落；只有该共享范围没有其它活跃 runtime 时，既有全局状态逻辑才允许闭段。

## 竞态与关闭

`GroupListenerManager` 为每群使用 `asyncio.Lock`，并通过快照序号、generation token 与 continuation token 处理 fast suffix、取消迟到和 replacement handoff。应用关闭时必须取消监听器持有的 idle/passive tasks，避免残留任务访问已关闭资源。

## 配置与排障

配置位于 `interaction.group_listener`：

- `enabled`
- `default_mode`
- `evaluation_batch_size`
- `idle_seconds`
- `max_pending_messages`
- `decision_timeout_seconds`

日志以 runtime key 记录模式转换、触发原因、窗口大小、generation token 和 fast action，不打印完整消息窗口。核心测试入口为 `tests/test_group_listener.py`，平台入口回归分别在 Telegram 与 OneBot adapter 测试中。
