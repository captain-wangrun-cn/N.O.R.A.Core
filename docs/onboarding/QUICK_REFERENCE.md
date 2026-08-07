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
  ├─ 纯文本紧随自己刚发的图片/视频轮？→ 折进那一轮（cancel + 合并输入 + 重启后脑）
  │            └─ 见 `_media_turn_fold_target()`；轮询中/已发过可见回复/已调过工具/超 90 秒/群聊换人则不折
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
| 模型别名 | `config.yml` → `llm.models.*` | `smart` / `fast` / `coder` / `image` / `video` / `fast-image`(可选) / `draw`+`draw_desc`(可选，成对) / `summary` |
| 生图接口形态 | `config.yml` → `llm.draw_api` | `images`(默认) / `chat`；只对 openai 类型 provider 生效 |
| 生图提示词写法 | `config.yml` → `llm.draw_prompt_style` | `natural`(默认，句子) / `tags`(标签串)；与接口形态无关，取决于 `draw` 模型 |
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
  - 内置工具：`create_new_skill`, `execute_skill`, `execute_tool_plan`, `read_file`, `search`, `write_file`, `edit_file`, `list_dir`, `get_available_skills`, `exec_command`, `view_media`, `crop_image_for_llm`, `generate_appearance_reference`(仅配置 `draw` 时注册), `set_alarm`, `list_alarms`, `cancel_alarm`
  - `view_media`: 默认 `return_image=true`，输出 `[image: abs_path]` 或 `[video: abs_path]` MediaTag，后脑下一轮会读取真实图片/视频并临时使用 image/video 模型；可用 `question` 参数指定对媒体要问的问题，且 `question` 非空时会自动强制 `return_image=true`。可用 `type` 参数筛选（`image`/`video`，不设则全部）。只查元数据/标签/OCR 时可设 `return_image=false`。
  - `view_media` 的 `keyword`：走**词法检索 + 语义检索 RRF 融合**（`memory/image_store.py`）。词法路 `search_by_lexical()` 分词 + CJK bigram 扩展 + 字段加权打分（tags 3.0 / description 2.0 / ocr_text 1.5），语义路走 Qdrant；两路各取 `limit+offset` 条、都不自己跳 offset，融合排序后统一切片。两路皆空才回退 MongoDB `$text`。
  - 工具回灌媒体有**单轮数量上限**：图片 4 个（带 `question` 时 3 个）、视频 1 个（`core/back_brain.py` 的 `MAX_TOOL_IMAGES_PER_TURN` 等）。被截断时 `tool_result` 会附带提示，引导收窄查询或用 `page` 翻页。
  - 回查媒体的**分析轮不用人设**：走 `brain/templates/media_analysis.jinja`，且该轮清空对话历史、不给工具、流式输出不发用户（详见 §6）。
  - `generate_appearance_reference(requirement, view, usage, replace, source_images)`: 按 `appearance/APPEARANCE.md` 生成形象参考图存进 `appearance/refs/{view}.png` 并更新 `manifest.json`。已有同名 view 必须显式 `replace=true` 才覆盖（覆盖会改变形象，须先问用户）；生成新视角时自动带上已有参考图作锚。不经 `draw_desc`。`appearance/refs/` 被 `_is_path_safe` 目录级拦截，通用文件工具读写会被拒。
  - `source_images`（逗号分隔本地路径）: 用户发图说"你就长这样"时用，路径从 `view_media` 的 `File:` 行取。来源图**优先占用** 3 张配额、排在附图列表最前，提示词写"与文字描述冲突以图为准"；剩余名额才补已有参考图（写"保持同一个人，只改视角"）。路径过 `_is_path_safe`，读不到直接报错不生图。
  - IMAGE_TAGS / IMAGE_OCR / IMAGE_DESC 质量兜底：缺失标签或标签未使用英文逗号分隔时，会触发一次仅请求 `IMAGE_TAGS` 的补齐重试。
  - 详细说明：`docs/architecture/tools.md`

### `core/scheduler.py`
- `ProactiveScheduler` — APScheduler 驱动主动消息调度
- `AIPresence` — 全局系统级忙碌标志（`is_generating` / `is_backend_busy`），**不表示任何私聊在线状态**
- `PrivatePresenceStore`（`core/private_presence_store.py`）— 按 `memory_scope_id` 存储私聊的 `ONLINE` / `SEMI_ONLINE` 状态，持久化到 JSON，重启后过期自动重置
- 群运行状态由 `core/group_listener.py` + `core/group_presence_store.py` 按 `platform:chat_id` 识别，但全局最多一个群 ONLINE；新群进入 ONLINE 会接管并关闭旧活跃群。ONLINE 群绕过 sender aggregator，SEMI_ONLINE 群仍仅由 @/回复进入正常聚合路径
- 群 ONLINE 有三重硬超时兜底（`no_interaction_timeout_seconds` / `idle_exit_seconds` / `max_online_seconds`），由 `_watchdog_loop` 巡检强制降级，不经过 fast 模型；持久化 ONLINE 记录超过 6 小时未更新会在启动时过期重置
- 群 fast 决策显式注入 SOUL/USER/MEMORY、最近三天 daily memory，以及按 `place_scope_id` 隔离的完整当前未封闭 segment；焦点 user prompt 只携带判定元数据 + 监听时长/互动信号（`listening_stats()`），不重复正文
- 群聊生命周期完全由 group listener 管理，不启动 scheduler followup，避免两套 ONLINE/SEMI_ONLINE 状态机并行
- 表情包仅在配置 `fast-image` 时生成一句话描述并进入普通消息上下文；sticker 不进入 ImageStore、图片标签/OCR/描述重试或向量图库
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

# 配置总览 + 分区修改（查看已配置/缺失项，只重配需要的分区，无需重跑整个向导）
python cli.py configure
python cli.py configure --no-wizard   # 无 config.yml 时不自动进向导，纯查看

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
| `brain/templates/media_analysis.jinja` | **回查媒体的分析轮专用**（`view_media` 返回图片/视频后的那一轮）：无人设的"媒体内容分析引擎"，输出是给上层推理模型看的内部观察数据 |
| `brain/templates/draw_desc.jinja` | **`[DRAW:]` 生图提示词轮专用**：无人设的"生图提示词生成引擎"，输出直接进文生图模型（用户看不到）。SOUL 在这里是判断表情/姿态的**素材**，不是让它扮演 Nora。顶部有「外观 / 风格 / 本次要求」三份输入的职责边界表。提示词写法按 `llm.draw_prompt_style` 分支（natural 句子 / tags 标签串） |
| `brain/prompts.py` | 提示词组装逻辑 + 身份上下文加载 |
| `adapters/PROMPT.md` | 通用平台适配协议：跨平台人格连续性、媒体标记、`[SPLIT]` |
| `adapters/telegram/PROMPT.md` | Telegram 平台特定提示（格式、长度等） |
| `adapters/onebotv11/PROMPT.md` | OneBot v11 / QQ 平台特定提示（群聊、CQ/媒体、工具限制、聊天记录） |
| `adapters/onebotv11/forward.py` | QQ 聊天记录（合并转发）展开：递归嵌套、媒体只留占位、按预算截断 |

> ⚠️ 修改行为时，优先改 `system.jinja`；修改性格/人设时，改 `SOUL.md`（优先）或 `persona_nora.jinja`（回退）。

**回查媒体分析轮为什么要单独一套 prompt**：分析轮如果沿用 `get_system_prompt()`，
`system.jinja` 第一行就是人设/SOUL，再叠上对话历史，模型会把 `question` 当成用户在搭话，
用 Nora 的口吻回一句寒暄而不是做客观分析。`media_analysis.jinja` 明确声明模型没有人设、
输出是用户看不到的系统内部数据，并禁止寒暄/emoji/向用户提问/输出标签块与消息控制标记。
**代码侧必须同时满足三条，缺一条人设都会漏回来**（`core/back_brain.py`）：
覆盖 system + user prompt、`turn_history = []`、该轮不给工具且流式输出不发用户不进 history。

---

## 6.1 标记速查（消息控制 / NEED_FOLLOW / NO_REPLY / SPLIT 延时）

- `[reply:MESSAGE_ID]`
  - 含义：让当前语义分段原生回复目标平台消息。
  - 模型可见性：标准场景提示会注入单条 `platform_message_id`，或按输入顺序去重后的聚合 `platform_message_ids`；主动消息等没有真实入站 ID 时不会虚构。
  - 聚合轮里每条消息还会附一行 `ID xxx: 正文摘要`（`core/scene_context.py` 的 `_inbound_message_id_lines`），否则模型只看到一串裸 ID，无法知道哪个对应哪句话。
  - 被回复的历史消息 ID **不算**当前入站 ID：OneBot 会把它塞进 `platform_message_ids` 供媒体反查（还排第一位），`_own_inbound_message_ids()` 会按 `reply_to_message_id` 剔除，否则 reply 会打到很旧的消息上。
  - 约束：ID 只能来自当前目标平台/场景的可靠上下文；不写标记就不自动引用，看到 ID 也不表示必须引用；跨平台/跨场景后不得复用来源 ID。
  - 区分：`platform_message_id(s)` 是当前输入自身；入站 `reply_to_message_id` 只描述用户回复了哪条历史消息；应用选择的 `reply_target_message_id` 才是代码层出站目标，三者不得自动互相推导。
  - ⚠️ 出站只校验 ID 格式（`adapters/message_controls.py`），不校验是否真实存在——模型幻觉的 ID 会直达平台。

- `[at:USER_ID]`
  - 含义：在明确群聊场景原生 @ 已知用户。
  - 约束：私聊、未知场景、无效 ID 或不支持的平台只移除标记；禁止猜测、跨平台复用 ID，禁止手写 CQ 码或 Telegram `tg://user` HTML。OneBot v11 会强制在原生 @ 后保留一个 ASCII 空格。

- `[poke:USER_ID]`
  - 含义：OneBot v11/NapCat 对指定用户执行一次戳一戳；它是轻量招呼、提醒或轻催促，不是普通消息段。
  - 约束：仅在关系和语境合适时偶尔使用；禁止猜 ID。未启用或不支持时标记会被移除，marker-only 不发送空消息。

- reply / at 选择
  - 回应具体消息默认 `[reply:MESSAGE_ID][at:USER_ID]`；只提醒/点名用 `[at:USER_ID]`；只引用且不想打扰作者用 `[reply:MESSAGE_ID]`；面向全群不加标记。

- 消息控制作用域
  - 每个 `[SPLIT]` 分段独立解释 `[reply:...]` / `[at:...]` / `[poke:...]`。
  - 平台长度切块时，reply 只附着该分段首个实际发送项，mention 不复制到后续 chunk。
  - OneBot 的 `[reply]`、`[at:current]`、`[at:reply]` 仅为兼容旧格式；新输出优先显式 ID。

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

- `[DRAW:要求]`
  - 含义：生成形象图（自拍/穿搭等）。要求里**只写**视角、角度、场景、动作、表情、构图，**不能带方括号**。
  - ⚠️ 要求里**不写外貌也不写画风**：外貌读 `APPEARANCE.md` + 参考图，画风读 `appearance/STYLE.md`。
    三份输入各有唯一权威，前脑重复写会打架。只有"这一次特意不一样"的东西（临时换的衣服、
    头发湿的、临时配饰）才写进要求。`draw_desc.jinja` 顶部有同样的三列表，并写明冲突时以另两份为准。
  - 行为：旁路通路，与 `[NEED_BACKEND]` 互不影响、不进后脑工具循环。文字先发，图后台生成完追发。
    单轮只取第一个；per-runtime_key 只保留最新一张。
  - 前置：`draw` + `draw_desc` 都配置；否则前脑提示里不注入该说明。
  - 触发范围：只在**主对话轮**。轮询审查轮、主动消息轮只剥离不生图。
  - 落盘：`appearance/generated/YYYY-MM-DD/draw_xxxxxxxx.png`，元数据进 Mongo `appearance_images`。
  - 失败：只记日志，不向用户追发安慰或报错文本。
  - 改形象设定（不是拍照）→ 走 `[TASK_INSTRUCTION]`+`[NEED_BACKEND]`，让后脑改 `APPEARANCE.md` +
    调 `generate_appearance_reference`。用户发图说"你就长这样"也走这条，后脑把图路径传 `source_images`。

---

## 7. 身份与记忆文件（Identity & Memory）

| 文件 | 作用 | 修改方式 |
|------|------|-----------|
| `SOUL.md` | AI 的灵魂：人设、语气、边界、性格 | ✅ 通用文件工具 `read_file`/`write_file`/`edit_file` |
| `appearance/APPEARANCE.md` | AI 的外貌形象：体征、发型、常穿、固定特征 | ✅ 通用文件工具 |
| `appearance/STYLE.md` | 出图风格偏好：写实/动漫、质感、镜头、比例。**不注入 system prompt**，只在生图链路读 | ✅ 通用文件工具（主人的偏好，AI 不自主改） |
| `appearance/refs/` | 形象参考图（视觉一致性的锚）+ `manifest.json` | ❌ 通用文件工具被目录级拦截；只能用 `generate_appearance_reference` |
| `USER.md` | 用户档案：名字、偏好、背景 | ✅ 通用文件工具 |
| `data/memory/MEMORY.md` | 长期记忆：决策、偏好、事实 | ✅ 通用文件工具 |
| `data/memory/YYYY-MM-DD.md` | 每日记忆日志 | ✅ 通用文件工具（按需读取） |

> 注意：
> - N.O.R.A Core 不是“每次会话全新实例”，这些文件主要作为可变 prompt 块。
> - 当前默认仅自动注入 SOUL/APPEARANCE/USER/MEMORY，`daily memory` 不自动注入（按需读取）。
>   `<appearance>` 紧跟 `<soul>` 注入——形象是"她自己"的一部分。
> - 首次运行时若 workspace 缺少 SOUL/APPEARANCE/USER/MEMORY，会自动从仓库根默认文件复制过去。
> - 形象系统是**文字在前、图在后**：`APPEARANCE.md` 写清楚了才生成参考图；改了描述则已有参考图
>   与文字不符，需要 `replace=true` 重新生成。详见 `docs/architecture/identity_files.md`。
