# 📖 快速参考手册

> 接手项目后的速查表。不需要全部记住，需要时来查即可。

---

## 1. 消息处理流水线

```
用户发消息
  ↓
adapters/telegram/main.py     收到原始消息
  ↓
adapters/aggregator.py        聚合连续消息（3秒缓冲）
  ↓
core/controller.py            NoraController.handle_message()
  ├─ 忙碌？→ fast_llm 判定 stop/change/queue → 即时回复
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
| Telegram Token | `config.yml` → `telegram.bot_token` | |
| LLM Provider | `config.yml` → `llm.provider` | `gemini` 或 `openai` |
| API Keys | `config.yml` → `llm.api_keys.*` | |
| 模型别名 | `config.yml` → `llm.models.*` | `smart` / `fast` / `coder` / `summary` |
| 消息历史 | `config.yml` → `memory.message_history.*` | `raw_window`, `compress_window` 等 |
| 成本跟踪 | `config.yml` → `cost_tracking.*` | `enabled`, `custom_prices` |
| 主动消息调度 | `config.yml` → `schedule.*` | `enabled` |
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

### `brain/tools.py`
- `ToolManager` — 工具注册、schema 生成、执行
  - 内置工具：`create_new_skill`, `execute_skill`, `execute_tool_plan`, `read_file`, `search`, `write_file`, `edit_file`, `list_dir`, `get_available_skills`, `exec_command`, `view_image`, `crop_image_for_llm`, `set_alarm`, `list_alarms`, `cancel_alarm`
  - 详细说明：`docs/architecture/tools.md`

### `core/scheduler.py`
- `ProactiveScheduler` — APScheduler 驱动主动消息调度
- `AIPresence` — 全局 AI 在线状态（ONLINE / SEMI_ONLINE）
- 手动重建接口：`regenerate_today_plan(clear_existing=True)`

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
  - `send_message(chat_id, text)`, `start_typing(chat_id)` 等

### `skills/loader.py`
- `SkillLoader` — 扫描 `skills/` 加载可用技能

---

## 4. 常用命令

```bash
# 配置向导
python cli.py --configure

# 运行主程序
python main.py

# Telegram 指令（运行后在聊天中）
# /regenerate_proactive [replace|append]
# /schedule_today
# /custom_scope [fast smart image coder | all | none]

# 查看聊天记录统计
python cli.py --show-history

# 清理聊天记录
python cli.py --clear-history

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
| `memory/cost_tracking.db` | 成本记录 |
| `memory/vector.db` | 向量数据库（Qdrant） |

---

## 6. 提示词体系

| 文件 | 作用 |
|------|------|
| `brain/templates/system.jinja` | **主系统提示词** — 所有行为规则、协议、约定 |
| `brain/templates/persona_nora.jinja` | 人设提示词回退（Nora 的性格，当 SOUL.md 不存在时使用） |
| `brain/prompts.py` | 提示词组装逻辑 + 身份上下文加载 |
| `adapters/telegram/PROMPT.md` | Telegram 平台特定提示（格式、长度等） |

> ⚠️ 修改行为时，优先改 `system.jinja`；修改性格/人设时，改 `SOUL.md`（优先）或 `persona_nora.jinja`（回退）。

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
