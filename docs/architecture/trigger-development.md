# Trigger 开发指南（Trigger Development Guide）

## 目标

本文面向扩展开发者，说明如何在 N.O.R.A Core 中新增一个外部 Trigger（例如 Calendar / Webhook / CRM 事件）。

> 架构说明请先看：`docs/architecture/trigger-system.md`

---

## 开发契约（最小可用）

实现一个 Trigger 时，至少满足以下契约：

1. **继承 `BaseTrigger`**（`triggers/base.py`）
2. 在 `run(event_handler)` 中启动监听逻辑
3. 产生命中事件时构造 `TriggerEvent`
4. 调用 `await self.dispatch_event(event)` 分发
5. 在 `triggers/factory.py` 中注册构建逻辑

---

## 目录与文件规范

建议结构：

```text
triggers/
  your_trigger/
    __init__.py
    main.py
    PROMPT.md           # 可选：规则说明
```

- `main.py`：核心触发逻辑
- `PROMPT.md`：该 Trigger 的业务规则文档/过滤规则文档（推荐）

---

## 生命周期与钩子

`BaseTrigger` 提供：

- 生命周期：`startup()` / `shutdown()`
- 启停钩子：`on_startup()` / `on_ready()` / `on_shutdown()`
- 事件钩子：`before_event(event)` / `after_event(event, result)`
- 健康检查：`health_check()`

推荐实践：

- 在 `run()` 里只负责启动任务，不做重逻辑。
- 阻塞 I/O（HTTP/SDK/DB）用 `asyncio.to_thread(...)` 包装。
- 在 `on_shutdown()` 中取消后台 task，避免悬挂协程。

---

## 事件模型

`TriggerEvent` 字段：

- `trigger_name`: 触发器名称（如 `email`）
- `source`: 外部来源（如 `imap` / `webhook`）
- `event_type`: 事件类型（建议命名：`trigger_xxx`）
- `reason`: 用于通知链路的事件描述
- `payload`: 原始结构化数据（用于追踪/后续扩展）
- `chat_id`: 可选，若为空由 `TriggerManager.default_chat_id` 兜底

---

## 模型调用（可选）

如果 Trigger 需要“轻量智能判定”（是否提醒、是否升级），可在 Trigger 内调用：

- `await self.call_model(model_alias="fast", ...)`

建议：

- 优先使用 `fast` 做分类筛选。
- 输出使用稳定结构（如 JSON），便于代码解析。
- 失败时采用保守策略（通常 `SKIP`）。

---

## 注册方式

在 `triggers/factory.py` 中根据配置创建实例：

- 读取 `triggers/config.yml -> <name>.*`
- `enabled=true` 时实例化并加入返回列表

> 示例：`triggers/config.example.yml`
> 兼容回退：当 `triggers/config.yml` 不存在时，系统会回退到 `config.yml -> triggers`。

控制器会统一注册到 `TriggerManager`，并在 adapter ready 后启动。

---

## 与通知链路的关系

Trigger **不直接发消息**，而是通过 manager 回调到：

- `NoraController._handle_trigger_notification(...)`
- 再复用 `SchedulerMixin._send_proactive_message(...)`

这样可以保证：

- 外部触发通知与主动消息体验一致
- 统一写入历史与 session
- 统一前脑/后脑路由行为

---

## 示例：Email Trigger

仓库内 `EmailTrigger` 是标准示例，展示了：

- IMAP 轮询拉取
- fast 模型二分类（`NOTIFY` / `SKIP`）
- `_seen_uids` 去重
- 命中后分发 `TriggerEvent`

你可以把它当模板，替换“数据源 + 过滤规则”即可实现新 Trigger。

---

## 调试建议

1. 先用 mock 数据验证 `TriggerEvent` 内容。
2. 再接真实外部源，观察日志是否有重复触发。
3. 保证 `reason` 对人可读，便于前脑生成自然通知。
4. 给解析函数写最小单测（happy path + fallback）。

---

## 常见坑

- 在模块顶层直接初始化重依赖，导致导入期失败。
- `dispatch_event` 前忘记设置 `chat_id` 或默认 chat_id。
- 输出格式不稳定（模型返回非 JSON）导致误判。
- 后台任务未取消，进程退出时出现 pending task 警告。
