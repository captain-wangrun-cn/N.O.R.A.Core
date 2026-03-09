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
  ├─ 在线/忙碌 → proactive 延迟重试一次（+5min）/ alarm 仍触发
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
| `SEMI_ONLINE` | `semi_online` | AI 空闲待命 | ✅ 正常触发 |
| `ONLINE` | `online` | AI 活跃对话中 / 处理消息 | ❌ 忽略 proactive / ✅ alarm 仍触发 |

> 注: `OFFLINE` 状态已在 2026-03-09 移除。两态模型更简洁：要么在对话中 (ONLINE)，要么空闲等消息 (SEMI_ONLINE)。

状态转换:
- 收到用户消息 → `ONLINE`
- 消息处理完毕 → 保持 `ONLINE`，启动对话延续定时器
- 对话延续检测判定为 `END` 或追话超限 → `SEMI_ONLINE`
- 用户回来（发新消息） → 取消延续定时器，回到 `ONLINE`

### 2.1 对话延续机制 (Conversation Follow-up)

当 AI 处于 `ONLINE` 状态时，如果用户超过 2 分钟没有发消息，会启动对话延续检测：

```
消息处理完毕
  ↓
保持 ONLINE + 启动对话延续定时器
  ↓ (等待 120s)
fast_llm 分析上下文 → 判断用户为什么停下来
  ├─ FOLLOWUP → 正常模型生成追话（1-2句自然追问）→ 写入聊天记录 → 60s 后再检测
  ├─ WAIT → 暂时不追话 → 60s 后再检测
  └─ END → 生成友好结束消息 → 写入聊天记录 → 进入 SEMI_ONLINE
```

配置参数（`NoraController` 属性）：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `FOLLOWUP_INITIAL_DELAY` | 120s | 首次检测等待时间（2分钟） |
| `FOLLOWUP_RECHECK_INTERVAL` | 60s | 每次复查间隔（1分钟） |
| `FOLLOWUP_MAX_COUNT` | 3 | 最大连续追话次数 |

关键设计:
- **所有追话消息和结束消息都会写入 MessageHistory + session history**，保持上下文连续性
- 用户任何时候发消息都会**立即取消**延续定时器并重置追话计数
- fast 模型仅用于轻量判断（FOLLOWUP/WAIT/END），生成内容用正常模型

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
**注意**: proactive 在在线/忙碌时会延迟 5 分钟重试一次，而不是直接丢弃。

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
| `followup_detect_system` | 对话延续检测 — system prompt (fast 模型) |
| `followup_detect_user` | 对话延续检测 — user prompt |
| `followup_message_system` | 追话内容生成 — system prompt |
| `followup_message_user` | 追话内容生成 — user prompt |
| `wrapup_message_system` | 结束对话消息 — system prompt |
| `wrapup_message_user` | 结束对话消息 — user prompt |

### 3.6 手动重新生成指令

通过 adapter 命令手动触发当天计划重建：

- `/regenerate_proactive`：覆盖重建（默认）
- `/regenerate_proactive replace`：覆盖重建
- `/regenerate_proactive append`：保留原计划并追加

### 3.7 查看今日计划指令

通过 adapter 命令查看今天尚未触发的主动消息计划：

- `/schedule_today`

返回内容包含：
- 触发时间（HH:MM）
- reason（若为空则显示“无缘由”）
- 今日未触发计划总数

返回结构示例：

```json
{
  "success": true,
  "skipped": false,
  "date": "2026-03-09",
  "count": 4,
  "total_today": 4,
  "message": "今日计划已重建: 4 个触发点"
}
```

---

## 4. 集成点

### 4.1 Controller (`core/controller.py`)

- `__init__`: 初始化 `ProactiveScheduler`，注入 LLM 回调
- `handle_new_message`: 用户发消息时设置 `ONLINE`
- `_generate_response`: 开始时 `busy=True`，结束时 `idle`
- `_send_proactive_message`: 主动消息生成时会注入最近会话上下文（MessageHistory + session history 兜底）；发送成功后同时写入数据库与 in-memory `session["history"]`
- `start_scheduler()`: adapter ready 后启动后台循环

### 4.2 ToolManager (`brain/tools.py`)

- 接受 `scheduler` 参数
- 当 scheduler 可用时注册 `set_alarm`、`list_alarms`、`cancel_alarm`

### 4.5 Adapter 命令 (`adapters/telegram/main.py`)

- 新增 `/regenerate_proactive` 指令
- 新增 `/schedule_today` 指令
- 指令透传到 `NoraController.handle_new_message()`
- 由 controller 调用 `scheduler.regenerate_today_plan()` 执行重建

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
