# 主动消息调度系统 (Proactive Scheduler)

> 文件位置: `core/scheduler.py`  
> 主类: `ProactiveScheduler`  
> 依赖: `APScheduler` (AsyncIOScheduler)

---

## 1. 设计概述

主动消息调度系统让 Nora 能够在合适的时间主动向用户发送关怀、问候、提醒等消息，而不仅仅是被动回复。

### 核心流程

```
启动时注册 CronTrigger (每天 00:00)
  ↓
凌晨触发 → 读取 SCHEDULE.md + SOUL.md + USER.md + MEMORY.md
  ↓
LLM 生成今日触发计划 [{"time": "08:00", "reason": "早安"}, {"time": "12:30"}, ...]
  ↓                    （reason 可选）
每个触发点注册独立的 DateTrigger Job → 精准到时回调
  ↓
到达触发时间 → 检查在线状态
  ├─ 在线/忙碌 → 跳过（proactive）/ 仍触发（alarm）
  └─ 离线/半在线 → LLM 生成主动消息 → 发送
```

### 调度引擎

使用 **APScheduler** (`AsyncIOScheduler`) 替代手动轮询:
- **CronTrigger**: 每日凌晨 00:00 生成当日计划
- **DateTrigger**: 每个触发点/闹钟使用精准时间触发，无需轮询
- 闹钟重启后自动恢复为 DateTrigger Job

---

## 2. 状态变量

| 状态 | 值 | 含义 | 主动消息行为 |
|------|---|------|------------|
| `OFFLINE` | `offline` | 用户离线 | ✅ 正常触发 |
| `SEMI_ONLINE` | `semi_online` | 半在线（处理完消息后） | ✅ 正常触发 |
| `ONLINE` | `online` | 用户在线（正在对话） | ❌ 忽略 proactive / ✅ alarm 仍触发 |

状态转换:
- 用户发消息 → `ONLINE`
- 消息处理完毕 → `SEMI_ONLINE`
- （暂无超时自动 → `OFFLINE` 的逻辑，预留给未来实现）

---

## 3. 组件

### 3.1 SCHEDULE.md

存储在 workspace 根目录，首次运行自动从仓库复制。包含:
- 作息习惯（起床、午餐、晚餐、就寝时间）
- 固定日程（周几做什么）
- 主动消息偏好
- 特殊日期（生日、纪念日等）

### 3.2 每日计划生成

凌晨 00:00 由 CronTrigger 触发，调用 `_generate_daily_plan_via_llm()`：
- Prompt 模板: `brain/templates/schedule.jinja` (block: `daily_plan_system`, `daily_plan_user`)
- 输入: SCHEDULE.md + SOUL.md + USER.md + MEMORY.md + 当前日期
- 输出: JSON 数组 `[{"time": "HH:MM", "reason": "..."}, {"time": "HH:MM"}, ...]`
- `reason` 字段可选，LLM 可以只返回 `time`
- 每个时间点注册为独立的 DateTrigger Job
- 缓存到 `cache/daily_plan.json`（重启不丢失）

### 3.3 动态闹钟 (Alarm Tools)

AI 可通过内置工具实时设置闹钟:

| 工具 | 说明 |
|------|------|
| `set_alarm` | 设置定时/倒计时闹钟，reason 可选 |
| `list_alarms` | 列出所有活跃闹钟 |
| `cancel_alarm` | 取消指定闹钟 |

闹钟持久化到 `cache/alarms.json`，系统重启后自动恢复为 DateTrigger Job。

### 3.4 触发逻辑

每个事件由 APScheduler DateTrigger 精准触发（替代旧版 30s 轮询）。

跳过条件（仅对 proactive 类型）:
- AI 在线 (`ONLINE`)
- 正在生成消息 (`is_generating`)
- 后端忙碌 (`is_backend_busy`)

**注意**: alarm 类型不受在线状态影响，始终触发。

### 3.5 Prompt 模板

所有 prompt 集中在 `brain/templates/schedule.jinja`：

| Block | 用途 |
|-------|------|
| `daily_plan_system` | 每日计划生成 — system prompt |
| `daily_plan_user` | 每日计划生成 — user prompt |
| `alarm_system` | 闹钟触发消息生成 — system prompt |
| `alarm_user` | 闹钟触发消息生成 — user prompt |
| `proactive_system` | 主动消息生成 — system prompt |
| `proactive_user` | 主动消息生成 — user prompt |

---

## 4. 集成点

### 4.1 Controller (`core/controller.py`)

- `__init__`: 初始化 `ProactiveScheduler`，注入 LLM 回调
- `handle_new_message`: 用户发消息时设置 `ONLINE`
- `_generate_response`: 开始时 `busy=True`，结束时 `idle`
- `start_scheduler()`: adapter ready 后启动后台循环

### 4.2 ToolManager (`brain/tools.py`)

- 接受 `scheduler` 参数
- 当 scheduler 可用时注册 `set_alarm`、`list_alarms`、`cancel_alarm`

### 4.3 Prompts (`brain/templates/schedule.jinja`)

- 所有调度相关 prompt 集中在 Jinja2 模板中，通过 `render_template()` 渲染
- `brain/prompts.py` 中 `SCHEDULE.md` 加入首次复制列表
- `load_identity_context()` 注入 `<schedule>` 块到 system prompt

### 4.4 main.py

- 在 adapter `on_ready` 钩子中调用 `controller.start_scheduler()`

---

## 5. 缓存文件

| 文件 | 路径 | 说明 |
|------|------|------|
| `alarms.json` | `{cache_dir}/alarms.json` | 动态闹钟持久化 |
| `daily_plan.json` | `{cache_dir}/daily_plan.json` | 今日触发计划缓存 |

---

## 6. 配置

在 `config.yml` 中:

```yaml
schedule:
  enabled: true  # 启用/禁用主动消息调度
```

时区跟随 `memory.message_history.timezone` 配置。
