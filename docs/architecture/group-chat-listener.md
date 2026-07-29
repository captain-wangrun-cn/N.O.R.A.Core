# 群聊独立监听架构

## 目标与边界

群聊监听把“认知范围”和“运行状态”拆开：

- **共享认知域**继续使用 `ConversationIdentity.memory_scope_id`，消息历史、RAG、压缩、段落摘要和既有后脑协调不按群拆分。
- **群运行域**以 `runtime_key = platform:platform_chat_id` 识别各群，但全局最多一个群为 `ONLINE`；新群进入 ONLINE 时自动接管并将旧活跃群切回 SEMI_ONLINE。各群仍分别保存待判断窗口、定时器和生成中断状态。
- **场景可见性**仍由 `place_scope_id`、平台、群 ID、说话者、消息 ID 和回复关系约束。共享背景不代表其它场景内容可以在当前群公开。

群模式持久化在 `data/group_presence_state.json`。只持久化模式，不持久化窗口和异步任务；重启后消息事实仍来自共享 `MessageHistory`。未登记或非法状态固定按 `SEMI_ONLINE` 处理；启动时先过期清理超过 6 小时未更新的 ONLINE 记录（`expire_stale_entries`），再对遗留的多个 ONLINE 记录只保留 `updated_at` 最新的一项（`normalize_single_online`）。**没有过期清理时，一个几天前进 ONLINE 的群在重启后仍是 ONLINE，两个 adapter 会继续对它整群放行，表现为「没被 @ 也在监听」。** 写入记录始终带 `platform` / `platform_chat_id`（缺省时由 runtime_key 拆分补齐）。

## Adapter 入口

Telegram 与 OneBot v11 都先完成平台事件标准化，并明确提供：

- `explicit_bot_mention`
- `reply_to_bot`
- `platform_message_id(s)`
- sender 与 reply metadata

`SEMI_ONLINE` 只接受显式 @ 与回复 Nora 的消息，继续走原有 3 秒 `MessageAggregator`。`ONLINE` 接受所有群事件，绕过 sender aggregator，逐条直达 `NoraController.submit_group_message()`；Telegram 相册仍作为一个带多个平台消息 ID 的原子事件。

## 被动监听状态机

每个群维护最多 20 条的 `pending_window`。ONLINE 消息一到达就写入共享历史，窗口截断不会删除历史。

- 自上次判断后累计 `evaluation_batch_size` 条（默认 2）：立即调用 fast。
- 最后消息后 90 秒且窗口非空：调用 fast。
- 最后消息后 90 秒且**窗口为空**：不调用 fast，直接按硬超时判定是否降级（见下）。此前这条分支直接 return，导致「被 @ → 回复 → 群彻底安静」后再无任何评估，ONLINE 永久挂着。
- `KEEP_LISTENING`：保留完整窗口，下一批继续合并判断，并累加 `consecutive_keep_listening`。
- `REPLY`：提升完整快照到现有 smart/front-brain 流水线，并通过 `_message_saved=True` 防止重复入历史。除定向消息和明确求助外，Nora 能对当前话题作出及时、相关且非重复的自然贡献时也可选择；价值或时机不明确时继续监听。
- `SEMI_ONLINE`：只切换当前群并清空其运行窗口。窗口内存在**近期**定向事件（`directed_veto_seconds` 内的 @/回复 Nora）时一票否决；超过该时效的滞留定向事件不再否决——生成期到达的 `reply_to_bot` 会经 `finish_generation` 落入 pending window，无时效的否决会把降级卡到它被 `max_pending` 挤出为止。

fast 判断使用序号快照；判断期间新到的 suffix 不会因旧结果而丢失。单轮 SEMI_ONLINE 结论遇到并发 suffix 时保留窗口并立即重判；**连续 `semi_online_vote_threshold` 轮（默认 2）都投 SEMI_ONLINE 时按当前窗口末尾强制降级**，避免活跃群里正确结论被反复静默作废。解析失败、超时和异常分别保守回退到 `KEEP_LISTENING` 或 `KEEP_PENDING`。

分类器 user prompt 除窗口计数外，还注入监听时长与互动信号（`listening_stats`）：已监听秒数、距最近一次 @/回复 Nora 的秒数、距最近一条群消息的秒数、连续 KEEP_LISTENING 轮数，以及两个参考阈值。缺少这些信号时模型无从判断「已经听了几个小时」，SEMI_ONLINE 的主动性会明显不足。

## 硬超时降级（看门狗）

`_watchdog_loop` 每 `watchdog_interval_seconds` 巡检所有持久化为 ONLINE 的群，不经过模型、也不要求 pending window 非空。由 `controller.start_triggers()` 在事件循环就绪后通过 `group_listener.start()` 启动；`receive` / `set_mode(ONLINE)` 也会懒启动它。命中任一条件即强制 `SEMI_ONLINE`：

- `max_online_seconds`：单次 ONLINE 的绝对上限（默认 6 小时）。
- `no_interaction_timeout_seconds`：多久没有 @/回复 Nora，也没有 Nora 自己的生成（默认 30 分钟）。
- `idle_exit_seconds`：群里多久没有任何新消息（默认 20 分钟）。

生成进行中或定向 handoff 未结束时跳过该群。进程重启后运行时字段为 0，用持久化 `updated_at` 兜底，避免把「刚重启」误判成「刚进入 ONLINE、从未互动」。同一套判定也被空窗口 idle 分支复用（`_demote_reason`）。

## 生成中断

一个 smart 回复对应一个“逻辑生成周期”，由 generation token 防止已取消任务迟到提交结果。

- 生成期间普通消息每累计 `evaluation_batch_size` 条（默认 2），fast 判断 `APPEND / KEEP_PENDING`。
- 每个逻辑周期最多一次普通 `APPEND` 中断；重启不重置额度。
- `KEEP_PENDING` 和额度用完后的消息回到同一 passive window，当前生成结束后继续判断。
- 只有 `explicit_bot_mention=True` 立即中断，不消耗普通额度；reply-only 仍按配置的普通批次规则处理。显式 @ 直达或中断时会一并取当前 pending 窗口中紧邻的最多 5 条消息作为话题上下文，并从 pending 中移除以避免后续重复处理。
- @ replacement 合并旧输入、可靠回复目标、@ 消息及 cancellation/handoff 期间到达的消息，按平台与群本地消息 ID 去重并保持顺序。

后脑工具任务沿用现有共享 scope 队列；普通群聊插话不会任意取消后脑工具执行。

## 模型标记

- `[ONLINE]`：群聊场景把当前群切到 ONLINE；若已有其它 ONLINE 群，则当前群接管监听，旧群立即回到 SEMI_ONLINE。
- `[SEMI_ONLINE]`：群聊场景只把当前群切到半在线；私聊保持既有全局/段落语义。
- 两者冲突时优先更保守的 `[SEMI_ONLINE]`。
- 标记始终在用户可见输出前剥离。

单个群进入半在线不等于关闭共享 `memory_scope_id` 的段落；只有该共享范围没有其它活跃 runtime 时，既有全局状态逻辑才允许闭段。

## 竞态与关闭

`GroupListenerManager` 为每群使用 `asyncio.Lock`，并通过快照序号、generation token 与 continuation token 处理 fast suffix、取消迟到和 replacement handoff。应用关闭时必须取消监听器持有的 idle/passive tasks，避免残留任务访问已关闭资源。

## 配置与排障

配置位于 `interaction.group_listener`：

- `enabled`
- `evaluation_batch_size`（默认 2，同时控制 passive 与生成期普通 append 的 fast 批次）
- `idle_seconds`
- `max_pending_messages`
- `decision_timeout_seconds`
- `watchdog_interval_seconds`（默认 30）
- `no_interaction_timeout_seconds`（默认 1800，0 关闭）
- `idle_exit_seconds`（默认 1200，0 关闭）
- `max_online_seconds`（默认 21600，0 关闭）
- `directed_veto_seconds`（默认 300，0 表示永久否决）
- `semi_online_vote_threshold`（默认 2）

日志以 runtime key 记录模式转换、触发原因、窗口大小、generation token 和 fast action，不打印完整消息窗口。排障 ONLINE 不退出时看这几条：`群监听 fast=... keep_streak=N semi_votes=N`、`群监听拒绝近期定向窗口的 SEMI_ONLINE 决策`、`群监听空窗口静默降级`、`群监听看门狗强制降级`、`启动过期清理: N 个群 ONLINE 记录已重置`。核心测试入口为 `tests/test_group_listener.py`，平台入口回归分别在 Telegram 与 OneBot adapter 测试中。
