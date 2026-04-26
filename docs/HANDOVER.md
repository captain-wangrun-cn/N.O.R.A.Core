## 项目概览
- 仓库：N.O.R.A.Core（branch: main）
- 目标：多平台智能体内核，包含 LLM 适配、工具/技能体系、成本追踪、记忆/RAG。
- 当前状态：可运行，自测通过；已完成图片记忆链路（入库/检索/回查再分析）、`view_image` 工具增强、CLI 高危清理保护。

## 近期关键改动（截至 2026-04-26）

### 📚 词库系统正式接入（System/User 双通道）— 2026-04-26

- 词库能力已从“模块可用”升级到“提示词链路正式接入”：
  - **常加载词库**注入 `system prompt`（基础语义背景）。
  - **懒加载词库**按用户输入命中后注入 `user prompt`（仅补命中词含义）。
- 新增词库全局说明文件 `lexicon/PROMPT.md`，作为统一行为约束与用途说明；通过 controller 的统一 LLM 调用封装全链路注入。
- `.dict` 文件新增 `@prompt:` 元数据能力，可在词库文件内写“该词库用途/解释风格提示”。
- 懒加载命中机制升级为 `manifest(term -> file)`，不再逐文件遍历，按命中词定位对应词库文件再加载。

**当前词库行为（关键约束）：**
- `supplement_meanings_for_text(...)` 仅匹配懒加载词条（不包含常加载词条）。
- 某词命中后仅补该词对应含义；可附带该懒词库文件的 `@prompt` 用途提示。

**涉及文件：**
- `lexicon/manager.py`
- `lexicon/PROMPT.md`
- `lexicon/README.md`
- `lexicon/always/*.dict` / `lexicon/lazy/*.dict`
- `brain/prompts.py`
- `core/controller.py`
- `core/front_brain.py`
- `core/back_brain.py`
- `tests/test_lexicon_manager.py`
- `tests/test_lexicon_global_prompt.py`

**验证：**
- 词库相关测试通过（`test_lexicon_manager.py` + `test_lexicon_global_prompt.py`）。

### 🔔 外部 Trigger 子系统 + Email 触发器 — 2026-04-02

- 新增独立 `triggers/` 子系统（与 `adapters/` 解耦），用于接入非 IM 外部触发源。
- 新增 `BaseTrigger` 基类，风格对齐 `BaseAdapter`：生命周期 + hooks + health_check。
- 新增 `TriggerManager`：统一注册/启动/事件分发，并提供 trigger 内临时模型调用能力。
- 新增 `EmailTrigger`（IMAP 轮询）：
   - 拉取新邮件摘要
   - 使用 `fast` 模型 + `brain/templates/trigger_email.jinja` 判定 `NOTIFY/SKIP`
   - 命中后通过 controller 复用 `_send_proactive_message` 链路通知 Nora。

**涉及文件：**
- `triggers/base.py` / `triggers/manager.py` / `triggers/factory.py`
- `triggers/email/main.py` / `triggers/email/PROMPT.md`
- `core/controller.py` / `core/message_handler.py` / `main.py`
- `brain/templates/trigger_email.jinja`
- `config.py` / `config.example.yml`
- `docs/architecture/trigger-system.md` / `docs/architecture/README.md`
- `docs/onboarding/README.md` / `docs/onboarding/QUICK_REFERENCE.md`

### 🧾 后脑忙碌时的二次确认入队 — 2026-03-25

- 新增“待确认入队”流程：当前脑判定用户请求需要后脑，但后脑此时仍在忙碌时，**不再直接入队**。
- 系统会先把当前后脑进度与已有队列详情交给前脑，前脑向用户发起二次确认；只有在用户明确确认后，才会真正加入 `BackendTaskQueue`。
- 若用户明确取消，则丢弃该待入队请求；若用户回复含糊，则继续保持“待确认”状态并追问。
- 该流程使用独立模板 `brain/templates/queue_confirmation.jinja`，由模型语义判定 `confirm / cancel / unclear`，避免硬编码关键词分支。

**涉及文件：**
- `core/message_handler.py`（新增待确认入队状态、确认消息生成、确认结果处理）
- `brain/templates/queue_confirmation.jinja`（二次确认询问/判定模板）
- `brain/templates/front_brain.jinja`（补充忙碌时需说明队列并确认后再追加的规则）

### 🧠 前脑/审查静默转交与任务指示 — 2026-03-16

- 新增前脑可选 **不向用户回复**：在前脑回复末尾标记 `[NO_REPLY]`，仅向后脑下达任务。
- 新增 **任务指示块**：在前脑或前脑审查回复中使用
   ```
   [TASK_INSTRUCTION]
   <写给后脑的指令，越具体越好>
   [/TASK_INSTRUCTION]
   ```
   后脑读取执行，用户不可见。
- 前脑普通对话与主动消息均可选择是否回复用户，且可附带任务指示。
- 前脑转交后脑时，会将任务指示写入后脑上下文；前脑审查继续轮询时也会把新增指示注入后脑。

**涉及文件：**
- `core/routing.py`（解析 `[NO_REPLY]` 与 `[TASK_INSTRUCTION]`，返回 `should_reply`、`task_instruction`）
- `core/front_brain.py`（透传 `should_reply`/`task_instruction` 调试与返回）
- `core/message_handler.py`（根据 `should_reply` 决定是否发送前脑回复，传递任务指示给后脑）
- `core/polling.py`（审查阶段支持 `NO_REPLY`，续跑时附带任务指示）
- `core/back_brain.py`（后脑 prompt 注入前脑任务指示）
- `core/scheduler_mixin.py`（主动消息链路传递任务指示）
- `brain/templates/front_brain.jinja` / `front_brain_review.jinja`（文档化 `NO_REPLY` 与 `TASK_INSTRUCTION` 用法）

### 🕒 消息时间戳可配置（含年月日星期时分秒） — 2026-03-16

- `memory.message_history.timestamp_format` 支持自定义消息时间戳格式，默认 `%Y-%m-%d %H:%M:%S %A`（含年月日星期时分秒）。
- MessageHistory 初始化时读取该配置，插入时间戳使用此格式，带上配置时区。
- 示例配置已写入 `config.example.yml`。

### � 对话滑动压缩与独立上下文库 — 2026-03-15

- 新增独立消息镜像库 `workspace/data/memory/message_log.db`：所有用户/AI 消息原文双写，原库仍保留。
- 新增上下文压缩库 `workspace/data/memory/context_compression.db`：存储按对话段(session)组织的滑动窗口摘要，重启可直接加载。
- 滑动压缩策略（summary 模型）：最新 3 个消息段完整保留（超阈值单独压缩）；第 4~6 个消息段单独压缩；第 7~10 个消息段合并压缩；新消息段出现时仅重压缩受影响窗口。
- `MessageHistory.get_context_messages()` 优先读取压缩库，原有消息库和归档逻辑保留回退。
- 配置示例新增 `mirror_db_path` / `context_db_path` / `long_message_threshold`。

**涉及文件：**
- `memory/context_store.py`（新）
- `memory/message_history.py`（集成镜像写入与滑动压缩读取）
- `core/controller.py`、`config.example.yml`（透传配置）

### �🧩 CUSTOM 注入范围可控 + Telegram 指令 — 2026-03-15

- 新增 `custom_injection.scopes` 配置：控制 CUSTOM.md 注入范围（fast/smart/image/coder），空或缺失表示全量注入，`none` 关闭注入。
- 新增 Telegram 指令 `/custom_scope`：按钮式勾选/取消注入范围（fast/smart/image/coder，含“关闭全部”）并写入 `config.yml`。
- 前脑/后脑/系统提示构建统一尊重 scope 配置。

**涉及文件：**
- `config.py`（新增 `get_custom_injection_scopes`）
- `config.example.yml`（新增 `custom_injection.scopes` 示例）
- `brain/prompts.py`（scope 解析 + system 注入开关）
- `core/front_brain.py`（smart scope 注入控制）
- `core/back_brain.py`（smart/image scope 注入控制）
- `core/message_handler.py`（/custom_scope 指令实现）
- `adapters/telegram/main.py`（指令注册与透传）
- `docs/onboarding/QUICK_REFERENCE.md`（新增配置/指令说明）

### 🗝️ CUSTOM/SECRET 文件与加密私密本 — 2026-03-12

### 🧹 Telegram /debug_cleanup 清理范围扩展 — 2026-03-19

- `/debug_cleanup` 按钮新增：消息镜像库（`message_log.db`）、上下文压缩库（`context_compression.db`）。
- “全部清空” 现在会同时删除并重建以上两个库，仍包含 Qdrant 记忆/Qdrant 图片、Mongo 图片库、聊天记录。
- 所有清理操作均需二次确认，避免误触。

**涉及文件：**
- `adapters/telegram/main.py`（指令按钮与回调，新增 `_purge_message_log`、`_purge_context_db`）
- `docs/onboarding/QUICK_REFERENCE.md`（指令与数据库清理说明）

### 📝 每日对话总结任务 — 2026-03-19

- 调度器新增每日 00:00 触发的对话总结：汇总前一日的镜像消息，生成摘要写入 `workspace/data/memory/YYYY-MM-DD.md`。
- 若文件已存在，会将旧内容一并带入模型提示进行融合；若当日无新消息且已有文件则跳过改写。
- 默认使用 `scheduler.default_chat_id`（缺省回落首个 session），平台默认为 `telegram`；依赖消息镜像库 `message_log.db`。
- 摘要通过 `summary` 模型（回退通用 llm），输出 Markdown 要点/待办。

**涉及文件：**
- `core/scheduler.py`（每日总结 Cron 触发）
- `core/controller.py`（注册 `daily_summary_callback`）
- `core/scheduler_mixin.py`（`_generate_daily_summary` 实现，融合旧文件并落盘）
- `memory/message_history.py`（`get_messages_between` 按时间段取原文）

- 新增 `CUSTOM.md`：用户维护的全局指令，自动注入提示，AI 禁止用文件工具读取/修改。
- 新增 `SECRET.md`：AI 私人加密记事本，使用专用工具 `read_secret_vault` / `write_secret_vault`，首次写入生成 `workspace/data/secret.key`（Fernet）。通用文件/命令工具禁止访问。
- 前脑 prompt 增加 CUSTOM/SECRET 路由说明：
   - 发现隐含偏好/习惯/日程 → `[NEED_BACKEND]` 写入 USER/SCHEDULE/MEMORY；
   - AI 自己的想法/偏见/反思 → `[NEED_BACKEND]` 触发写入 SECRET（回复中不得泄露内容）；
   - CUSTOM 由用户编辑，AI 仅遵守。
- 系统 prompt 更新文件边界表，SENSITIVE 列表扩充（`SECRET.md`/`CUSTOM.md`/`secret.key`），`write_file`/`exec_command` 增加阻拦。
- 工具列表新增 `read_secret_vault` / `write_secret_vault`，TOOL_INTROS 说明用途。

**涉及文件：**
- `brain/prompts.py`（CUSTOM 注入、路径常量）
- `brain/templates/system.jinja`、`front_brain.jinja`、`front_brain_review.jinja`、`context_injection.jinja`（CUSTOM/SECRET 块与路由规则）
- `brain/tools.py`（加密读写、敏感防护、工具注册、TOOL_INTROS）
- `CUSTOM.md`、`SECRET.md`（新增占位）
- `requirements.txt`（新增 `cryptography`）
- `docs/architecture/tools.md`（工具文档新增 SECRET 章节）

### 📋 对话分段系统 (Conversation Session Segmentation) — 2026-03-11

- 当 AI 从 `ONLINE` 进入 `SEMI_ONLINE` 时，自动将本次在线期间的所有对话封装为一个**对话段落 (Session)**。
- 数据库变更：
  - `messages` 表新增 `session_id` 列（旧数据库自动迁移）
  - 新建 `conversation_sessions` 表，记录每个段落的起止时间、消息数、摘要、触发来源
- 段落封闭后异步生成摘要（使用 `summary` 模型）
- `get_context_messages()` 增强：跨段落消息之间自动插入 `[📋 对话段落分隔]` 标记，帮助 LLM 理解时间结构
- `get_statistics()` 新增 `total_sessions` 和 `active_messages` 字段
- `/status` 指令新增对话段落统计显示
- `clear_chat_history` / `clear_all_history` 同步清理 `conversation_sessions` 表

**涉及文件：**
- `memory/message_history.py`（新增 `close_session`、`_generate_session_summary`、`get_session_messages`、`get_recent_sessions`、`get_current_session_id`、`_migrate_db`；修改 `_init_db`、`get_context_messages`、`get_statistics`、`clear_chat_history`、`clear_all_history`）
- `core/controller.py`（修改 `_transition_to_semi_online`、`/status` 指令）
- `docs/architecture/message_history.md`（新增对话分段章节、`conversation_sessions` 表文档）
- `docs/architecture/README.md`（更新索引）
- `docs/onboarding/QUICK_REFERENCE.md`（更新 MessageHistory 方法列表、数据库文件说明）
- `docs/onboarding/COMMON_PITFALLS.md`（新增对话分段注意事项）

### 🧭 工具/技能简介注入与前后脑职责收敛 — 2026-03-11

- 在 `brain/tools.py` 新增 `TOOL_INTROS`（工具一句话简介）。
- 在前脑/后脑 prompt 注入工具与技能简介，仅用于**前脑路由判断**；前脑禁止调用工具/技能，所有执行由后脑完成。
- `system.jinja` / `front_brain.jinja` 明确：简介只作为“需要后脑”的判断提示，前脑仅标记 `[NEED_BACKEND]` 触发后脑。
- 语气要求具体化：前脑/后脑回复必须使用第一人称。

**涉及文件：**
- `brain/tools.py`（TOOL_INTROS）
- `core/controller.py`（注入工具简介到 instructions/front brain）
- `brain/templates/context_injection.jinja`（工具简介块，强调前脑仅路由）
- `brain/templates/front_brain.jinja`、`brain/templates/system.jinja`（前脑禁用工具执行、第一人称约束）

## 近期关键改动（截至 2026-03-10）

### 🔧 七项架构修复 — 2026-03-10

**1. 抢占机制响应优化**
- 在后脑工具循环的关键节点（工具执行前/后）插入 `await asyncio.sleep(0)` 抢占检查点
- 缩短 `_interrupt_backend` 中的 cancel 等待超时从 2s → 1s
- **效果**：前脑发送 stop/change 指令后，后脑能更快响应取消

**2. 后脑工具调用内容不污染聊天记录**
- session history 保存时过滤 `【Tool Output for xxx】` 格式的工具中间输出
- 数据库（message_history）只保存最终 `final_response_buffer`，不含工具调用细节
- **效果**：避免 fast_llm 在忙碌分支处理时看到大量工具输出占用 token

**3. Telegram 多图片（相册）+ caption 支持**
- `_handle_photo` 重写：通过 `media_group_id` 检测 Telegram 相册
- 新增 `_media_group_buffers` + `_media_group_timers` 缓冲机制，等待 1s 后合并同组图片
- 多张图片合并为一条消息（多个 `[image: ...]` 标签 + caption）发给聚合器
- 单张图片仍走原有直接处理路径

**4. Skill 路径统一为代码仓库路径**
- 新增 `CODE_ROOT` / `CODE_SKILLS_DIR` 常量（`brain/tools.py`），指向项目根目录的 `skills/`
- `create_new_skill`、`execute_skill`、`get_available_skills` 全部改用 `CODE_SKILLS_DIR`
- 注入到 LLM 的工作区提示也更新为代码仓库的 skills 路径
- **效果**：AI 不会把技能脚本错误地创建/查找到 workspace 目录下

**5. 前脑纯聊天路由简化**
- `front_brain.jinja` 明确：纯聊天时不需要输出任何标记（不需要 `needs_backend=False`）
- 代码逻辑本已正确：不包含 `[NEED_BACKEND]` 即视为纯聊天

**6. 异步化工具执行，解除事件循环阻塞**
- `execute_skill` 改为 `async def`，内部使用 `asyncio.create_subprocess_exec` 替代同步 `subprocess.run`
- `exec_command` 同样改为 `async def`，使用 `asyncio.create_subprocess_shell`
- **效果**：工具执行期间不再阻塞事件循环，前脑可正常接收和处理用户消息

**7. WorkerStatus 进度系统增强**
- 新增 `preview_next(tool_name, tool_args)` 方法：工具执行前预告"准备做什么"
- 新增 `tool_history_readable` 列表：用户可读的步骤描述（"执行技能: web_search" 而非原始参数）
- 新增 `_TOOL_DESCRIPTIONS` 映射表：工具名 → 中文可读描述
- `get_summary()` 输出增加"下一步"预告信息
- **效果**：前脑在忙碌回复中能告诉用户后脑"正在做什么"和"准备做什么"

**涉及文件**：
- `core/controller.py`：WorkerStatus 增强、抢占检查点、session history 过滤
- `brain/tools.py`：CODE_ROOT/CODE_SKILLS_DIR、async 化 execute_skill/exec_command
- `adapters/telegram/main.py`：多图相册支持
- `brain/templates/front_brain.jinja`：纯聊天路由提示优化
- `brain/templates/context_injection.jinja`：skills 路径提示更新

### 🆕 AI 在线状态重构 & 对话延续机制 — 2026-03-09

**核心变更：两态模型 + 对话延续检测**

1) **取消 OFFLINE 状态**，AI 在线状态简化为两态：
   - `SEMI_ONLINE`：空闲待命 — 允许调度好的主动消息（每日计划/闹钟）
   - `ONLINE`：活跃对话中 — 忽略调度主动消息，启用对话延续检测

2) **对话延续机制（Conversation Follow-up）**
   - 当 AI 处于 `ONLINE` 状态且用户超过 **2 分钟** 没有发消息时，用 **fast 模型** 分析上下文判断是否需要主动追话
   - 判断结果为三种：
     - `FOLLOWUP`：用正常模型生成追话消息（类似朋友聊天时一方停下来，另一方追一句）
     - `WAIT`：暂时不追话，1 分钟后再检测
     - `END`：对话已自然结束，进入 `SEMI_ONLINE`
   - 每 **1 分钟** 复查一次，最多连续追话 **3 次**
   - 追话超限或判定 END → 生成友好的结束消息 → 进入 `SEMI_ONLINE`
   - **所有追话消息和结束消息都会写入 MessageHistory 和 session history**

3) **状态转换流程**：
   ```
   SEMI_ONLINE → 收到用户消息 → ONLINE
   ONLINE → 生成完毕 → 保持 ONLINE + 启动延续定时器
   ONLINE → 2分钟无回复 → fast_llm 检测 → FOLLOWUP/WAIT/END
   ONLINE → END 或追话超限 → 结束消息 → SEMI_ONLINE
   SEMI_ONLINE → 调度主动消息正常触发
   ```

4) **新增文件/模板**：
   - `brain/templates/schedule.jinja` 新增 6 个 block：
     - `followup_detect_system/user`：追话检测 prompt
     - `followup_message_system/user`：追话内容生成 prompt
     - `wrapup_message_system/user`：结束对话 prompt

5) **代码改动点**：
   - `core/scheduler.py`：`AIPresence` 枚举移除 `OFFLINE`；`_AIPresenceState` 新增 per-chat 对话追踪数据
   - `core/controller.py`：新增 `_followup_loop`、`_detect_followup_intent`、`_send_followup_message`、`_send_wrapup_message`、`_transition_to_semi_online` 等方法

### 🆕 主动消息调度系统与交互链路迭代（Schedule + UX）— 2026-03-09

已完成以下行为对齐：

1) 指令入口迁移到 Adapter（Telegram）
- 新增 `/regenerate_proactive`：手动重建今日主动消息计划（`replace` / `append`）。
- 新增 `/schedule_today`：查看今日未触发主动消息计划。
- 两个命令均由 `adapters/telegram/main.py` 透传到 `NoraController.handle_new_message()` 统一处理。

2) 在线状态语义固定为 **AI 状态**（非用户状态）
- 使用全局 `AIPresence`：`ONLINE` / `SEMI_ONLINE`（已取消 `OFFLINE`）。
- 触发跳过依据：`presence` + `is_generating` + `is_backend_busy`。

3) proactive 跳过后改为“延迟重试一次”
- proactive 事件在 AI 在线/忙碌时，不再直接丢弃。
- 当前实现会创建一次 **5分钟后重试** 的延迟事件（alarm 仍然始终触发）。

4) 主动消息写回上下文
- `_send_proactive_message()` 成功发送后，除了写入 `MessageHistory`，还会同步写入 `self.sessions[chat_id]["history"]`。
- 保持与普通回复一致的 in-memory 上下文连续性。

5) 主动消息生成会参考当前会话上下文
- `_send_proactive_message()` 在调用 LLM 生成主动消息前，会注入最近会话历史：
   - 优先使用 `MessageHistory`（最近若干条）
   - 若数据库上下文较少，则补充 `session["history"]` 最近内容
- 使主动消息更贴近当前对话语境。

6) 新增调试命令 `/debug`
- 通过 Telegram 适配器透传到 Controller。
- 每个 chat 可独立开关 debug 模式（toggle）。
- 开启后会实时推送关键链路信息：RAG 检索、推理轮次、工具调用参数、工具结果摘要、token usage、生成完成状态。

7) `/status` 已扩展
- 可查看前脑/后脑状态、模型配置、scheduler 状态、排队消息、RAG/成本开关。

8) `system.jinja` 清理与收敛
- 清理重复内容、错乱缩进与多余空行。
- 删除不必要或有争议的具体示例，保留规则型描述，降低 prompt 污染与 token 开销。

5) SCHEDULE 模板去具体化
- `SCHEDULE.md` 已改为“节奏/偏好线索”风格，移除固定场景模板（如工作学习等），提高 LLM 动态生成空间。

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
- `brain/tools.py`：工具注册与实现（含 `view_image(return_image)`）。
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
