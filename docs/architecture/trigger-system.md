# 外部触发系统（Trigger System）

## 目标

为 N.O.R.A Core 提供独立于 IM 适配器的“外部事件触发层”，用于接入邮件、日历、Webhook 等来源。

核心要求：

- 与 `adapters/` 解耦，单独目录 `triggers/`。
- 采用与 `BaseAdapter` 一致的继承与生命周期风格。
- 支持触发器内部临时调用模型（典型：`fast`）做轻量判定。
- 命中后复用主动消息发送链路，保证通知体验一致。

## 架构总览

```text
External Source (Email/...) 
        ↓
Trigger (BaseTrigger 子类)
        ↓  [before_event / after_event hooks]
TriggerManager
        ↓  notify_callback(chat_id, reason, event_type)
NoraController._handle_trigger_notification
        ↓
SchedulerMixin._send_proactive_message(...)
        ↓
Adapter.send_message(...)
```

## 目录结构

- `triggers/base.py`
  - `BaseTrigger`：生命周期、钩子、健康检查、事件分发、`call_model()`。
  - `TriggerEvent`：触发事件标准结构。
- `triggers/manager.py`
  - `TriggerManager`：注册触发器、绑定模型调用器、统一事件分发。
- `triggers/factory.py`
  - 根据配置构建触发器实例（当前内置 Email 示例）。
- `triggers/email/main.py`
  - `EmailTrigger`：示例 Trigger（IMAP 轮询 + fast 模型过滤）。
- `brain/templates/trigger_email.jinja`
  - 邮件过滤模板（规则 + 严格 JSON 输出）。

## 生命周期与钩子（与 Adapter 对齐）

`BaseTrigger` 提供：

- `startup()` / `shutdown()`
- `on_startup()` / `on_ready()` / `on_shutdown()`
- `before_event(event)` / `after_event(event, result)`
- `health_check()`

> 建议：所有外部 I/O 放在后台任务并通过 `asyncio.to_thread` 包装阻塞调用。

## 临时模型调用机制

`TriggerManager` 在注册时为 trigger 绑定 `model_caller`，触发器内可直接：

- `await self.call_model(model_alias="fast", system_prompt=..., user_prompt=...)`

设计好处：

- trigger 不依赖具体 provider，实现层解耦。
- 可按场景切换 `fast/smart/summary`。
- 便于在 trigger 层做“低成本预筛选”。

## Email Trigger（示例实现）

### 流程

1. 轮询 IMAP（支持 `UNSEEN` 过滤）。
2. 解析发件人、主题、时间、正文摘要。
3. 使用 `trigger_email.jinja` + `fast` 模型输出 JSON：
   - `{"decision":"NOTIFY|SKIP","reason":"..."}`
4. 若 `NOTIFY`，生成 `TriggerEvent(event_type="trigger_email")`。
5. 通过 manager 通知 controller，最终走主动消息链路发送。

### 去重策略

- 运行时维护 `_seen_uids`，避免同一邮件重复触发。

### 输出对齐

- 通知采用“自动消息”同链路，用户看到的是统一风格回复，不是系统硬编码文本。

> 说明：Email Trigger 只是一个参考实现，不是 Trigger 系统的唯一形态。
> 实际扩展请参考开发文档：`docs/architecture/trigger-development.md`。

## 配置

在 `triggers/config.yml` 中配置：

- `enabled`
- `default_chat_id`
- `email.*`（IMAP 参数、轮询周期、正文截断等）

> 示例文件：`triggers/config.example.yml`
> 兼容说明：若 `triggers/config.yml` 不存在，会回退读取 `config.yml -> triggers`。

## 扩展新 Trigger（通用流程）

1. 新建 `triggers/<name>/main.py`，继承 `BaseTrigger`。
2. 在 `run()` 启动监听循环或订阅。
3. 在命中时构造 `TriggerEvent` 并 `dispatch_event()`。
4. 若需模型过滤，使用 `call_model(...)`。
5. 在 `triggers/factory.py` 注册创建逻辑。

## 错误与降级策略

- 配置不完整：触发器静默跳过，不影响主对话链路。
- 模型判定异常：默认 `SKIP`（保守不打扰）。
- 无 `chat_id`：manager 丢弃事件并记录 warning。

## 已知限制

- 当前 Email 去重仅在进程内；重启后依赖 UNSEEN/服务端状态。
- 仅实现邮件触发器，其它来源（Webhook/Calendar）待扩展。
