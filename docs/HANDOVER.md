## 项目概览
- 仓库：N.O.R.A.Core（branch: main）
- 目标：多平台智能体内核，包含 LLM 适配、工具/技能体系、成本追踪、记忆/RAG。
- 当前状态：可运行，自测通过；已完成图片记忆链路（入库/检索/回查再分析）、`view_image` 工具增强、图片裁剪工具 `crop_image_for_llm`、CLI 高危清理保护、**主动消息调度系统**。

## 近期关键改动（截至 2026-03-08）

### 🆕 主动消息调度系统 (Schedule System) — 2026-03-08

1) 新增核心模块：`core/scheduler.py`
- `ProactiveScheduler` 类：后台循环每 30 秒检查触发事件
- 每天凌晨 00:00 自动调用 LLM，根据 `SCHEDULE.md` + `SOUL.md` + `USER.md` + `MEMORY.md` 生成今日主动消息触发计划
- 到达触发时间后，将缘由发送给 LLM 生成自然的主动消息
- 两个状态变量：`ONLINE`（在线）和 `SEMI_ONLINE`（半在线），在线时忽略 proactive 消息
- `ScheduledEvent` 数据类支持 proactive（每日计划）和 alarm（动态闹钟）两种类型
- 闹钟和每日计划持久化到 `cache/alarms.json` 和 `cache/daily_plan.json`

2) 新增动态闹钟工具（`brain/tools.py`）
- `set_alarm`: 设置定时或倒计时闹钟，支持 `HH:MM`、`YYYY-MM-DD HH:MM`、`MM-DD HH:MM` 和倒计时模式
- `list_alarms`: 列出所有活跃闹钟
- `cancel_alarm`: 按 ID 取消闹钟
- 闹钟**必须**附带缘由，持久化到本地缓存防止重启失效

3) 新增 `SCHEDULE.md` 文档模板
- 包含作息习惯、固定日程、主动消息偏好、特殊日期四个板块
- 首次运行时从仓库根目录自动复制到 workspace

4) 身份上下文扩展（`brain/prompts.py`）
- `_ensure_workspace_identity_files()` 新增 `SCHEDULE.md` 复制逻辑
- `load_identity_context()` 注入 `<schedule>` 块到 system prompt

5) Controller 集成（`core/controller.py`）
- `__init__` 中初始化 `ProactiveScheduler`，注入 LLM 回调
- `handle_new_message` 中设置用户在线状态
- `_generate_response` 中同步忙碌/空闲状态
- 新增 `_generate_daily_plan_via_llm()` 和 `_send_proactive_message()` 回调
- `ToolManager` 构造器接受 `scheduler` 参数

6) 启动集成（`main.py`）
- 在 adapter `on_ready` 钩子中自动启动 scheduler

7) 配置（`config.example.yml`）
- 新增 `schedule.enabled` 配置项（默认 true）

8) 文档
- 新增 `docs/architecture/schedule-system.md` 架构文档
- 更新 `docs/architecture/README.md` 索引
- 更新 `docs/HANDOVER.md`（本文件）

### 🆕 图片裁剪工具（LLM Focus Crop）— 2026-03-08

1) 新增工具：`crop_image_for_llm`（`brain/tools.py`）
- 目标：在图像分析前先裁剪重点区域，提高 LLM 读图聚焦能力。
- 参数模式：`preset` 与像素范围（`left/top/right/bottom`）二选一。
- 优先级：若两者同时传入，**preset 优先**，并在返回中提示像素范围被忽略。
- 支持 `padding` 扩展裁剪框，支持 `output_path` 自定义输出。
- `return_image=true`（默认）时返回 `MediaTag: [image: ...]`，可直接进入下一轮多模态分析。

2) 控制器联动（`core/controller.py`）
- 原先仅处理 `view_image(return_image=true)` 的图片回注。
- 现在 `crop_image_for_llm(return_image=true)` 也会被解析并注入 `multimodal_images`。
- 下一轮自动切换到 `image_llm` 继续分析裁剪结果。

3) 测试覆盖（`tests/test_image_memory.py`）
- 新增 `crop_image_for_llm` schema 注册断言。
- 新增“preset 优先于像素范围”用例。
- 新增“像素范围参数完整性（left/top/right/bottom 必填）”用例。

### 🆕 内置工具技术文档补齐 — 2026-03-08

1) 新增工具总览文档
- 新增 `docs/architecture/tools.md`。
- 内容覆盖 `ToolManager` 当前全部内置工具：
   - `create_new_skill`
   - `execute_skill`
   - `execute_tool_plan`
   - `read_file`
   - `search`
   - `write_file`
   - `edit_file`
   - `list_dir`
   - `get_available_skills`
   - `exec_command`
   - `view_image`
   - `crop_image_for_llm`

2) 文档索引同步
- `docs/architecture/README.md` 已加入 `tools.md` 入口。
- `docs/onboarding/QUICK_REFERENCE.md` 已同步最新工具列表并链接 `tools.md`。
- `docs/CODE_STRUCTURE.md` 已补充 `crop_image_for_llm` 与 tools 架构文档说明。
- `README.md` 已添加 tools 技术文档链接，便于新接手会话快速定位。

### 🆕 图片记忆系统（Image Memory）全链路落地 — 2026-03-08

已实现“用户发图 → 标签入库 → 可检索回查 → 可再次图像分析”的完整流程：

1) `brain/multimodal.py`
- 从 `[image: ...]` 解析真实图片内容时，为每张图生成 `image_id`（`img_<8hex>`）。
- 支持 `downloads/` 与 `data/` 前缀映射到 workspace 对应目录。

2) `memory/image_store.py`
- 新增图片存储模块：MongoDB + Qdrant 双写。
- Mongo 存结构化元数据（`image_id/file_path/tags/user_id/chat_id/timestamp`）。
- Qdrant 存标签向量与 payload（用于语义检索）。
- 支持按 `image_id` / 关键词 / 时间范围 / 语义检索。
- 修复了 pymongo Collection 的 bool 判断问题（统一 `is None` 比较）。

3) `core/controller.py`
- 图片输入自动走 `image` 模型。
- 图片标签提示词从内联迁移为模板渲染（见 `brain/templates/image_tags.jinja`）。
- 标签协议保留 `[IMAGE_TAGS:img_xxx]...[/IMAGE_TAGS]`，并在对用户输出前剥离。
- 新增图片元数据异步入库逻辑。

4) `brain/tools.py` / `view_image`
- `view_image` 增加 `return_image: bool`。
- `return_image=false`：返回元数据。
- `return_image=true`：额外返回 `MediaTag: [image: absolute_path]`，用于直接发送或后续多模态分析。

5) `view_image(return_image=true)` 后自动切 image 模型
- Controller 会解析工具输出中的图片标签并在下一轮注入 `multimodal_images`。
- 下一轮自动切换到 `image_llm` 进行图片分析。

6) Telegram 下载路径迁移
- `adapters/telegram/main.py` 收到图片/文档/贴纸时改为落盘到：`workspace/data/telegram/`。
- 传入上下文的路径改为 workspace 相对路径（如 `data/telegram/xxx.jpg`）。

7) Prompt / 文档同步
- 新增模板：`brain/templates/image_tags.jinja`（关键词标签策略）。
- `system.jinja` 补充 `view_image` 参数约束（禁止空参数调用、优先 keyword/image_id/time、可启用 `return_image=true`）。
- 更新 `docs/architecture/image-memory.md`、`docs/CODE_STRUCTURE.md`、`README.md`、`docs/CLI_USAGE.md`。

8) 测试状态
- 相关回归：`tests/test_image_memory.py`、`tests/test_multimodal_input.py`、`tests/test_routing.py`、`tests/test_openai_tools.py` 均通过（最近一次 32 passed）。

## 历史改动（截至 2026-03-07）

### 🆕 路由改造：前脑优先（Front-Brain-First）— 2026-03-07

本轮已将“先独立路由、再决定前/后脑”的流程改为“所有消息先交给前脑”：

- 新流程：
   1) 用户消息先进入前脑（`_generate_front_chat_response`）
   2) 前脑即时回复用户，并同时判断是否需要后脑
   3) 若需要后脑，在回复中附加 `[NEED_BACKEND]` 信号
   4) Controller 清理该信号后发给用户，再自动启动 `_generate_response`

**代码级变化：**

1) `core/controller.py`
- `_start_routed_task` 改为统一启动前脑路径。
- 前脑回复逻辑升级为“回复 + 路由判断”二合一。
- 删除旧的独立路由判定调用链（不再先跑一次路由 LLM）。

2) `core/routing.py`
- 新增 `parse_front_brain_response(response_text)`：
   - 解析是否包含 `[NEED_BACKEND]`
   - 返回 `needs_backend` 与清理后的 `user_reply`

3) `tests/test_routing.py`
- 新增前脑路由信号解析测试，覆盖：
   - 需要后脑
   - 不需要后脑
   - 空回复
   - 信号在末尾/中间

**收益：**
- 少一次路由模型调用（降低延迟与成本）
- 用户先收到自然回复，再由后脑在后台执行任务
- 路由判断更贴近对话语境

### 🆕 跨平台媒体标识统一（English-only）— 2026-03-07

已统一媒体输入标识为英文协议，移除中文标识：

- 系统协议：`[image:]` / `[video:]` / `[audio:]` / `[file:]` / `[doc:]`
- Telegram 适配输出同步改为英文标签（如 `[image: ...]`）
- 图片输入检测仅保留英文结构化标签（`[image:]`）+ 扩展名兜底
- `adapters/telegram/PROMPT.md` 已重写为干净版本并与主协议对齐

### 🆕 记忆与身份提示重构（2026-03-06 ~ 2026-03-07）

这轮已将“身份/记忆文件”的行为从 OpenClaw 风格调整为 N.O.R.A Core 实际语义：

- **不是每次会话全新实例**：N.O.R.A Core 进程通常持续运行。
- `SOUL.md` / `USER.md` / `MEMORY.md` 作为**可变 prompt 块**使用，而不是唯一连续性来源。

**代码级变化：**

1) `brain/prompts.py`
- 身份文件改为 **workspace 优先**：
   - `workspace/SOUL.md`
   - `workspace/USER.md`
   - `workspace/data/memory/MEMORY.md`
- 保留仓库根目录文件作为回退兼容（旧数据可继续读取）。
- 新增首次启动自动复制逻辑：若 workspace 缺少上述文件，会从仓库根默认文件复制过去。
- **取消每日记忆自动注入**（`memory/YYYY-MM-DD.md` 不再自动拼进 system prompt），避免上下文膨胀。

2) 消息历史数据库路径迁移到 workspace
- `memory/message_history.py` 默认 DB 改为：
   - `workspace/data/memory/message_history.db`
- `core/controller.py` / `cli.py` 已同步为“配置未指定时走该默认路径”。
- `tests/test_message_history.py` 提示信息同步更新默认 DB 路径来源。

3) Telegram typing 行为优化
- `core/controller.py`：进入 `llm.chat_stream(...)` 生成阶段时**立即**调用 `start_typing(chat_id)`。
- 不再等首个 text chunk 到达才触发，修复“输入状态太短暂/偶尔看不到”的问题。

4) 工具侧策略调整
- 移除了 `update_soul` / `update_user` / `update_memory` 专用工具。
- 统一使用通用文件工具 `read_file` / `write_file` / `edit_file` 维护身份/记忆文件。

### 🆕 身份与记忆系统（SOUL.md / USER.md / MEMORY.md）— 2026-03-05
参考 OpenClaw 的工作区设计理念，新增持久化身份与记忆文件系统：

**新增文件：**
- `SOUL.md` — AI 的灵魂：人设、语气、边界、性格。定义"你是谁"。
- `USER.md` — 用户档案：名字、偏好、背景、当前关注。定义"你在帮助谁"。
- `MEMORY.md` — 长期记忆：重要决策、用户偏好、持久事实、经验教训。
- `memory/YYYY-MM-DD.md` — 每日记忆：当天事件、笔记和临时上下文。

**代码改动（当前版本已演进）：**
- `brain/prompts.py`：`load_identity_context()` 仍负责注入身份上下文，但现已调整为 workspace 优先，并取消每日记忆自动注入。
- `brain/tools.py`：已回归通用文件工具策略（无 `update_*` 专用工具）。
- `brain/templates/system.jinja`：已同步新协议与路径说明。

**设计理念（当前版本）：**
- 借鉴 OpenClaw 的文件化思路，但 N.O.R.A Core **不采用“每次会话全新实例”前提**。
- 身份文件作为可变提示块，辅助对齐语气/偏好/长期事实。
- 每日记忆改为按需读取，默认不注入。

### 之前的改动（截至 2026-02-28）
1) **脑口分离 / 打断队列（core/controller.py）**
   - `WorkerStatus` 跟踪阶段/步骤/耗时；忙时用 `fast_llm` 判定 stop / change / queue 并即时回复。
   - 支持取消 asyncio 任务、排队新消息，`pending_messages` 完成后自动处理。
   - LLM 流式 chunk 统一为 dict（`type: text|tool_call|usage`），收集 usage 做成本统计。

2) **会话隔离 & Telegram 适配**
   - `controller` 使用 `storage_id`（私聊=用户ID，群聊=chat_id）隔离 MessageHistory/RAG；群聊消息前置用户名。
   - `MessageAggregator` 支持携带完整 context（chat_id/user_id/chat_type/user_name/text），回调签名改为 `on_complete(context)`。
   - `TelegramAdapter`：
     - 支持群聊仅响应 @机器人 或 回复机器人；私聊始终响应。
     - 兼容非文本（图片/文件/贴纸/回调）聚合；修复 reply 提取 KeyError。
     - 获取 bot username（post_init）；link 发送前插入零宽空格规避安全过滤。

3) **Gemini 提供方（brain/providers/gemini.py）**
   - 安全策略放宽（HarmBlockThreshold.BLOCK_NONE），避免链接触发内容过滤。
   - 链接净化 `_sanitize_links_for_safety`；统一错误前缀，返回“抱歉，处理您的请求时遇到了问题：{error}”。
   - 保留 usage chunk；仍有类型提示 lint（GenerativeModel 导出）可忽略，运行不受影响。

4) **成本追踪 / 定价（core/cost_tracker.py & cli.py）**
   - 仅使用配置 `cost_tracking.custom_prices`，已移除全部内置价格表；缺价模型首次警告，后续不重复刷屏。
   - CLI 模型选择时，若缺价会提示手动输入输入/输出单价并写入 `cost_tracking.custom_prices`，供后续计算。
   - SQLite 持久化使用日志，记录 provider/model/alias/tokens/cost/context/timestamp；`view_costs.py` 支持统计。

5) **工具层与 Schema（brain/tools.py）**
   - `read_file` 行号转 int；`_generate_schema` 解析 `Optional[int]`、`float` → NUMBER；修复类型安全。

6) **图像与依赖**
   - 发送大图时优先压缩，使用 `Pillow`（需已安装）；`Image.Resampling.LANCZOS` 取代旧常量。

7) **文档**
   - 新增架构文档《双进程架构》及索引；README 中文化并更新安装指引。

8) **测试**
   - 现有测试：`tests/test_context_pricing.py`、`tests/test_schema_fix.py`、embedding/vector/rag 等。
   - 尚未补充脑口分离/Telegram 端到端用例，后续可补。

## 主要模块速览
- `core/controller.py`：消息路由、脑口分离、任务队列、打断与状态跟踪。
- `core/cost_tracker.py`：成本记录、定价表、统计输出。
- `brain/providers/openai.py` / `brain/providers/gemini.py`：流式输出 + usage chunk。
- `brain/tools.py`：工具注册与实现（含 `view_image(return_image)`、`crop_image_for_llm`）。
- `brain/multimodal.py`：图片 payload 解析、`image_id` 生成、路径映射。
- `memory/image_store.py`：图片元数据与向量存储/检索。
- `skills/web_fetch`：网页抽取（r.jina.ai + fast LLM）。
- `cli.py`：配置向导 + 运维入口；`clean_qdrant/clean_mongodb` 已改为清空所有 collections，并要求二次确认（输入 `DELETE ALL`）。
- `view_costs.py`：命令行统计查看（today/week/month/provider/alias）。

## 使用与验证
- 配置：`python cli.py --configure`
- 运行：`python main.py`
- 成本查看：`python view_costs.py --today`（可加 `--provider` / `--alias`）
- 测试示例：`python tests/test_context_pricing.py`、`python tests/test_schema_fix.py`、`pytest`

## 注意事项 / 待办
- 若需成本计算，务必在配置或向导中为所选模型补齐 `cost_tracking.custom_prices`（已无内置价表）。
- 新增工具若含可选数值参数，确认 `_generate_schema` 兼容。
- 适配新的 LLM 提供方时，流式 chunk 需包含 `usage`，字段结构与现有实现一致。
- 脑口分离逻辑尚缺端到端自动化测试，可补充回归用例（队列、stop/change）。
- 服务器需安装 `Pillow` 以支持图片压缩；本地 lint 对 `google-generativeai` 的 `GenerativeModel` 导出警告可忽略，线上运行正常。
- Telegram 适配：确认机器人 username 可获取；群聊仅响应 @ 或回复；私聊已修复未响应问题。
- MessageAggregator 接口已改为 `add_message(chat_id, text, context)`，下游回调签名 `on_complete(context)`。
- 身份文件主路径已迁移到 workspace（`workspace/SOUL.md`、`workspace/USER.md`、`workspace/data/memory/MEMORY.md`），旧路径仅作回退兼容。
- 每日记忆（`workspace/data/memory/YYYY-MM-DD.md`）默认不自动注入；如业务需要可在 prompt 组装层手动恢复。
- 聊天记录默认 DB 路径为 `workspace/data/memory/message_history.db`；若配置了 `memory.message_history.db_path` 则按配置优先。
- Telegram 媒体下载路径为 `workspace/data/telegram/`；多模态解析支持 `data/...` 路径映射。
- `view_image` 若用于“找回并继续看图”，应传 `return_image=true`；否则只返回元数据。
- `crop_image_for_llm` 建议优先传 `preset`；只有需要精确坐标时再传 `left/top/right/bottom`。
- CLI 清理 RAG 数据是高危操作：会删除所有 collections，且必须二次确认（`DELETE ALL`）。

## 快速检查清单
- `config.cost_tracking.enabled` 是否开启。
- `config.cost_tracking.custom_prices` 是否覆盖预期（无则成本为 0）。
- CLI 模型列表价格展示取决于自定义价格；缺价会提示手输。
- 日志中是否能看到 `[Cost] provider/model: tokens = $cost`。
- 后端忙碌时，新消息是否被 fast_llm 正确判定并回复（stop/change/queue）。
- Telegram 私聊是否响应，群聊是否仅在 @ 机器人或回复时响应；消息是否带用户名；RAG/历史是否按 storage_id 隔离。
- 生成阶段开始时 Telegram 是否立即出现 typing（而不是等首个 chunk）。
- workspace 首次运行时是否自动复制 `SOUL.md`/`USER.md`/`MEMORY.md`。

## 交接
- 记录人：GitHub Copilot
- 日期：2026-03-08
