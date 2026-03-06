## 项目概览
- 仓库：N.O.R.A.Core（branch: main）
- 目标：多平台智能体内核，包含 LLM 适配、工具/技能体系、成本追踪、记忆/RAG。
- 当前状态：可运行，自测通过；近期完成“脑口分离”与打断队列；成本追踪依赖用户配置价格（无内置价表）。

## 近期关键改动（截至 2026-03-06）

### 🆕 工具调用泄漏防护体系（2026-03-06）

**问题：** LLM 有时会以文本形式"伪造"工具调用（如输出 `execute_tool('edit_file', {...})` 或 `<execute_skill>...</execute_skill>`），而不是真正使用 function calling 机制。用户会看到原始的调用代码。

**三层防护（已实现）：**
1. **Prompt 层** — `brain/templates/system.jinja`：
   - 新增"工具调用方式 — 严格规范"章节，明确列出所有禁止的伪调用格式
   - 在"主动执行协议"中强化 function calling 要求，明确区分"调用工具"和"写出调用代码"
   - "工具与技能的区别"中反复强调只能通过 function calling 完成
2. **实时过滤层** — `core/controller.py` → `_is_tool_call_leak()`：
   - 在 `[SPLIT]` 分段发送时拦截包含泄漏文本的段落，阻止发送给用户
3. **最终输出清理层** — `core/controller.py` → `_clean_tool_call_leaks()`：
   - 在最终响应发送前，清理所有泄漏模式（XML 标签、函数调用语法、JSON 参数块等）
   - 保留自然语言部分，仅移除泄漏内容

**检测覆盖的泄漏模式：**
- `tool_name(...)` 函数调用格式
- `<execute_skill>...</execute_skill>` XML 标签格式
- `execute_tool('tool_name', {...})` 间接调用格式
- 包含 `old_code`/`new_code`/`file_path`/`args_json` 的 JSON 块

**文件改动：** `brain/templates/system.jinja`、`core/controller.py`、`docs/onboarding/COMMON_PITFALLS.md`

### 🆕 身份与记忆文件策略重构（2026-03-06）

本轮已将身份/记忆策略从“每会话全新实例”改为**N.O.R.A Core 持续运行 + 文件作为可变 prompt**：

**核心行为变更：**
- `SOUL.md` / `USER.md` / `MEMORY.md` 仍用于注入系统提示，但定位为“可变提示块”，不是“唯一连续性来源”。
- 每日记忆（`YYYY-MM-DD.md`）**不再自动注入**系统提示（防止上下文膨胀），需要时由模型显式读取。

**路径与初始化变更：**
- `brain/prompts.py` 现在优先读取 workspace：
   - `workspace/SOUL.md`
   - `workspace/USER.md`
   - `workspace/data/memory/MEMORY.md`
- 首次运行若 workspace 缺失上述文件，会自动从仓库根目录同名文件复制默认版本。
- 仍保留旧路径回退读取（兼容历史文件）。

**聊天记录数据库路径迁移：**
- `memory/message_history.py` 新增 `get_default_message_history_db()`，默认 DB 路径迁移为：
   - `workspace/data/memory/message_history.db`
- `core/controller.py` 与 `cli.py` 已改为使用该默认路径（当 `config.yml` 未显式配置 `memory.message_history.db_path` 时）。

**工具策略修正：**
- 已移除 `update_soul` / `update_user` / `update_memory` 专用工具。
- 统一使用通用文件工具 `read_file` / `edit_file` / `write_file` 维护身份与记忆文件。

**Telegram typing 体验改进：**
- `core/controller.py` 调整 typing 触发时机：进入 LLM 生成阶段即触发 `start_typing`，而非等待首个 text chunk，减少“输入状态过短暂”问题。

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
- `brain/tools.py`：工具注册与实现（`read_file` / `exec_command` / `edit_file` 等）。
- `skills/web_fetch`：网页抽取（r.jina.ai + fast LLM）。
- `cli.py`：配置向导；OpenAI Base URL 默认官方，可手动修改；模型缺价时现场手输价格并写入配置；支持开关成本追踪。
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

## 快速检查清单
- `config.cost_tracking.enabled` 是否开启。
- `config.cost_tracking.custom_prices` 是否覆盖预期（无则成本为 0）。
- CLI 模型列表价格展示取决于自定义价格；缺价会提示手输。
- 日志中是否能看到 `[Cost] provider/model: tokens = $cost`。
- 后端忙碌时，新消息是否被 fast_llm 正确判定并回复（stop/change/queue）。
- Telegram 私聊是否响应，群聊是否仅在 @ 机器人或回复时响应；消息是否带用户名；RAG/历史是否按 storage_id 隔离。

## 交接
- 记录人：GitHub Copilot
- 日期：2026-02-28
