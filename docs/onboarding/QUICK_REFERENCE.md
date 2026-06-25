# 📖 快速参考手册

> 接手项目后的速查表。不需要全部记住，需要时来查即可。

---

## 1. 消息处理流水线

```
用户发消息
  ↓
adapters/telegram/            收到原始消息（incoming/message 模块标准化事件）
  ↓
adapters/aggregator.py        聚合连续消息（3秒缓冲）
  ↓
core/controller.py            NoraController.handle_message()
  ├─ 忙碌？→ fast_llm 判定 stop/change/queue → 即时回复
  │            └─ 若前脑新判定需要后脑：先展示当前队列详情并二次确认，确认后再入队
  └─ 空闲？→ 进入生成循环：
       ├─ 加载持久化上下文（message_history.db）
       ├─ 拼接系统提示词（system.jinja + persona）
       ├─ llm.chat_stream() 流式生成
       │    ├─ 检测 [SPLIT] → 实时发送已完成段落
       │    └─ 检测 tool_call → 执行工具 → 继续循环
       └─ 循环结束 → 发送最终文本 → 保存到 message_history
```

---

## 2. 配置速查

| 配置项 | 位置 | 说明 |
|--------|------|------|
| Telegram Token | `adapters/telegram/config.json` → `bot_token` | 不写入全局 `config.yml` |
| OneBot v11 连接 | `adapters/onebotv11/config.json` | `websocket_url` / `access_token` / `enable_napcat_api` |
| LLM Provider | `config.yml` → `llm.provider` | `gemini` 或 `openai` |
| API Keys | `config.yml` → `llm.api_keys.*` | |
| 模型别名 | `config.yml` → `llm.models.*` | `smart` / `fast` / `coder` / `summary` |
| 消息历史 | `config.yml` → `memory.message_history.*` | `raw_window`, `compress_window` 等 |
| 成本跟踪 | `config.yml` → `cost_tracking.*` | `enabled`, `custom_prices` |
| 主动消息调度 | `config.yml` → `schedule.*` | `enabled` |
| 外部触发器 | `triggers/config.yml` | `enabled`、`default_chat_id`、`email.*` |
| 工作区路径 | `config.yml` → `workspace.root_path` | 技能/下载/数据的根目录 |
| CUSTOM 注入范围 | `config.yml` → `custom_injection.scopes` | `fast`/`smart`/`image`/`coder`/`none` |

---

## 3. 核心类/函数

### `core/controller.py`
- `NoraController` — 主控制器
  - `handle_new_message(context)` — 消息入口
  - `_generate_response(...)` — 生成循环（流式 + 工具）
  - `WorkerStatus` — 跟踪每个 chat 的工作状态
  - Scheduler 命令：`/regenerate_proactive`、`/schedule_today`

### `brain/llm.py`
- `get_llm_client(model_alias)` — 获取 LLM 客户端实例
- `chat_stream(system_prompt, user_prompt, history, tools)` — 流式对话

### `lexicon/manager.py`
- `LexiconManager` — 轻量词库管理器
  - `build_always_system_prompt_block()`：常加载词库注入 system prompt
  - `build_lazy_user_prompt_block(text)`：懒加载命中后注入 user prompt
  - `.dict` 支持 `@prompt:` 元数据（词库用途提示）

### `brain/tools.py`
- `ToolManager` — 工具注册、schema 生成、执行
  - 内置工具：`create_new_skill`, `execute_skill`, `execute_tool_plan`, `read_file`, `search`, `write_file`, `edit_file`, `list_dir`, `get_available_skills`, `exec_command`, `view_media`, `crop_image_for_llm`, `set_alarm`, `list_alarms`, `cancel_alarm`
  - `view_media`: 默认 `return_image=true`，输出 `[image: abs_path]` 或 `[video: abs_path]` MediaTag，后脑下一轮会读取真实图片/视频并临时使用 image/video 模型；可用 `question` 参数指定对媒体要问的问题，且 `question` 非空时会自动强制 `return_image=true`。可用 `type` 参数筛选（`image`/`video`，不设则全部）。只查元数据/标签/OCR 时可设 `return_image=false`。
  - IMAGE_TAGS / IMAGE_OCR / IMAGE_DESC 质量兜底：缺失标签或标签未使用英文逗号分隔时，会触发一次仅请求 `IMAGE_TAGS` 的补齐重试。
  - 详细说明：`docs/architecture/tools.md`

### `core/scheduler.py`
- `ProactiveScheduler` — APScheduler 驱动主动消息调度
- `AIPresence` — 全局 AI 在线状态（ONLINE / SEMI_ONLINE）
- 手动重建接口：`regenerate_today_plan(clear_existing=True)`
- 每日总结：每日 00:00 触发 `_on_daily_summary_trigger`，调用 `daily_summary_callback` 生成上一日摘要

### `memory/message_history.py`
- `MessageHistory` — 消息持久化
  - `add_message(platform, chat_id, role, content, ...)` — 添加消息（写入时间戳、触发压缩）
  - `get_context_messages(platform, chat_id, ...)` — 获取上下文（Pinned → 归档总结 → 压缩总结 → 原始消息，跨段落自动插入分隔标记）
  - `close_session(platform, chat_id, ...)` — 关闭当前对话段落（AI 进入 SEMI_ONLINE 时由 controller 调用）
  - `get_recent_sessions(platform, chat_id, ...)` — 获取最近的对话段落列表
  - `get_session_messages(platform, chat_id, session_id)` — 获取指定段落的所有消息
  - `_perform_compression(...)` — 一级压缩（N 条 → 1 条总结）
  - `_create_archive_summary(...)` — 归档（合并所有一级总结）
  - `_generate_session_summary(...)` — 异步为已关闭段落生成摘要

### `adapters/base.py`
- `BaseAdapter` — 平台适配器基类
  - `AdapterEvent` / `AdapterMessage` / `MessageSegment`：轻量事件与消息段标准层
  - `load_metadata()`：读取每个 adapter 的 `metadata.json`
  - `dispatch_message()`：统一执行 before/handler/after 钩子
  - `send_message(chat_id, text)`, `start_typing(chat_id)` 等

### `triggers/base.py`
- `BaseTrigger` — 外部触发器基类（仿 `BaseAdapter`）
  - 生命周期：`startup()` / `shutdown()`
  - 钩子：`on_startup()` / `on_ready()` / `before_event()` / `after_event()`
  - 临时模型调用：`call_model(model_alias="fast", ...)`

### `triggers/email/main.py`
- `EmailTrigger` — IMAP 邮件轮询触发器
  - 检测新邮件后，用 `fast` 模型 + `trigger_email.jinja` 判定 `NOTIFY/SKIP`
  - 命中 `NOTIFY` 时通过 TriggerManager 走主动消息通知链路（等价自动消息体验）

### `skills/loader.py`
- `SkillLoader` — 扫描 `skills/` 加载可用技能

---

## 4. 常用命令

```bash
# 配置向导
python cli.py --configure

# 运行主程序
python main.py
python main.py --no-tui --adapter auto
python main.py --no-tui --adapter onebotv11

# Telegram 指令（运行后在聊天中）
# /regenerate_proactive [replace|append]
# /schedule_today
# /custom_scope （按钮勾选 fast/smart/image/coder，含 none/关闭）
# /debug_cleanup （按钮选择清理范围：Qdrant 记忆、Qdrant 图片、Mongo 图片库、消息镜像库、上下文压缩库、聊天记录，含“全部清空”与取消；二次确认防误触）
# /undo （撤销上一条前脑发送的消息；删除消息历史与镜像库，若记录了平台 message_id 且平台支持，会尝试删除聊天界面消息）

# 查看聊天记录统计
python cli.py history stats

# 清理聊天记录
python cli.py history clear --all
python cli.py history clear --chat-id <chat_id> --platform telegram

# 清理本地数据文件（消息历史 / 镜像库 / 上下文压缩库 / 可选成本库）
python cli.py history clear-data
python cli.py history clear-data --keep-costs

# 查看成本
python view_costs.py --today
python view_costs.py --week --provider gemini

# 运行测试
pytest
pytest tests/test_message_history.py -v

# TUI 终端界面
python tui.py
```

---

## 5. 数据库文件

| 文件 | 说明 |
|------|------|
| `data/memory/message_history.db` | 聊天记录 + 压缩总结 + 对话段落（`messages` + `summaries` + `conversation_sessions` 表） |
| `data/memory/message_log.db` | 消息镜像库：用户/AI 原文双写，便于追溯 |
| `data/memory/context_compression.db` | 按消息段(session)的滑动窗口压缩上下文（最新3段保留、4-6段单独压缩、7-10段合并压缩） |
| `data/memory/message_log.db` / `data/memory/context_compression.db` 清理 | 通过 `/debug_cleanup` 可选单独清空或在“全部清空”时一并删除并重建 |
| 撤销（/undo）影响 | 仅删除前脑的上一条 assistant 消息：`message_history.messages` 与 `message_log.raw_messages` 删除对应记录；若记录了平台 `message_id` 且平台支持，会尝试删除原消息。 |
| `memory/cost_tracking.db` | 成本记录 |
| `memory/vector.db` | 向量数据库（Qdrant） |
| `data/memory/YYYY-MM-DD.md` | 每日对话总结：每日 00:00 生成上一日摘要；若文件已存在则带入旧内容融合输出，默认使用 `scheduler.default_chat_id`（无则回落首个 session），平台默认 `telegram` |
| `lexicon/PROMPT.md` | 词库全局说明：通过统一链路注入 system prompt，约束词库使用方式 |

---

## 6. 提示词体系

| 文件 | 作用 |
|------|------|
| `brain/templates/system.jinja` | **主系统提示词** — 所有行为规则、协议、约定 |
| `brain/templates/persona_nora.jinja` | 人设提示词回退（Nora 的性格，当 SOUL.md 不存在时使用） |
| `brain/prompts.py` | 提示词组装逻辑 + 身份上下文加载 |
| `adapters/PROMPT.md` | 通用平台适配协议：跨平台人格连续性、媒体标记、`[SPLIT]` |
| `adapters/telegram/PROMPT.md` | Telegram 平台特定提示（格式、长度等） |
| `adapters/onebotv11/PROMPT.md` | OneBot v11 / QQ 平台特定提示（群聊、CQ/媒体、工具限制） |

> ⚠️ 修改行为时，优先改 `system.jinja`；修改性格/人设时，改 `SOUL.md`（优先）或 `persona_nora.jinja`（回退）。

---

## 6.1 标记速查（NEED_FOLLOW / NO_REPLY / SPLIT 延时）

- `[NO_REPLY]`
  - 含义：静默执行，本轮不向用户发送可见消息。
  - 典型场景：只做后台操作、默默记录内部信息、避免重复回复。

- `[NEED_FOLLOW]`
  - 含义：请求对话延续模块更快跟进。
  - 行为：首次 follow-up 延迟使用 `FOLLOWUP_NEED_FOLLOW_DELAY`（默认 15 秒）。

- `[SPLIT]` / `[SPLIT:秒数]`
  - 含义：将一条回复拆成多段发送。
  - 延时语法：`[SPLIT:1.5]` 表示下一段前停顿 1.5 秒。
  - 说明：`[SPLIT]` 为默认短停顿，`[SPLIT:秒数]` 为显式延时控制。

---

## 7. 身份与记忆文件（Identity & Memory）

| 文件 | 作用 | 修改方式 |
|------|------|-----------|
| `SOUL.md` | AI 的灵魂：人设、语气、边界、性格 | ✅ 通用文件工具 `read_file`/`write_file`/`edit_file` |
| `USER.md` | 用户档案：名字、偏好、背景 | ✅ 通用文件工具 |
| `data/memory/MEMORY.md` | 长期记忆：决策、偏好、事实 | ✅ 通用文件工具 |
| `data/memory/YYYY-MM-DD.md` | 每日记忆日志 | ✅ 通用文件工具（按需读取） |

> 注意：
> - N.O.R.A Core 不是“每次会话全新实例”，这些文件主要作为可变 prompt 块。
> - 当前默认仅自动注入 SOUL/USER/MEMORY，`daily memory` 不自动注入（按需读取）。
> - 首次运行时若 workspace 缺少 SOUL/USER/MEMORY，会自动从仓库根默认文件复制过去。
