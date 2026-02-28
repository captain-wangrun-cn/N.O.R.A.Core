## 项目概览
- 仓库：N.O.R.A.Core（branch: main）
- 目标：多平台智能体内核，包含 LLM 适配、工具/技能体系、成本追踪、记忆/RAG。
- 当前状态：可运行，自测通过；近期完成“脑口分离”与打断队列；成本追踪依赖用户配置价格（无内置价表）。

## 近期关键改动（截至 2026-02-28）
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
