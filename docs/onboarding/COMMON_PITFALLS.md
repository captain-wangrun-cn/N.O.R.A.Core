# ⚠️ 常见陷阱与注意事项

> 历届会话踩过的坑，节省你的时间。

---

## 1. 环境依赖

### ❌ Windows 上 `ZoneInfo` 崩溃
- **现象：** `ZoneInfoNotFoundError: No time zone found with key UTC`
- **原因：** Windows 下 Python 的 `zoneinfo` 需要 `tzdata` 包提供时区数据。
- **修复：** `pip install tzdata`
- **影响范围：** `MessageHistory` 初始化、所有时区相关测试。

### ❌ 缺少 `config.yml`
- **现象：** `FileNotFoundError: config.yml not found`
- **原因：** 配置文件不在版本控制中，需要手动创建。
- **修复：** `cp config.example.yml config.yml` 然后 `python cli.py --configure`

---

## 2. 异步与事件循环

### ❌ 压缩不触发
- **现象：** 消息数量超过 `compress_window` 但 `summaries` 表为空。
- **原因：** `add_message()` 中的 `asyncio.create_task(self._check_and_compress(...))` 需要正在运行的事件循环。在同步调用或无 loop 的测试中，压缩会被静默跳过。
- **排查：** 确认调用链在 `asyncio.run()` 或已有 event loop 的环境内。

### ❌ 异步测试不运行
- **现象：** `test_message_history.py` 标记为 `FAILED: async def functions are not natively supported`
- **原因：** `pytest` 默认不支持 `async def` 测试函数。
- **修复：** `pip install pytest-asyncio` 并在测试上添加 `@pytest.mark.asyncio`。

---

## 3. 消息压缩逻辑

### ⚠️ Level 2 未实现
- 文档描述了三层（Level 1/2/3），但代码只写入 `level=1`（一级摘要）和 `level=3`（归档）。
- `_create_archive_summary` 会**删除所有** `level=1` 记录，导致归档后中期摘要消失。
- 如果需要保留中期层，需要修改归档逻辑。

### ⚠️ 压缩依赖 LLM
- 压缩/归档需要可用的 `summary` 模型（`config.yml` → `llm.models.summary`）。
- 如果模型不可用或 API Key 无效，压缩会静默失败（`except` 捕获后仅打日志）。

### ⚠️ 对话分段 (Session Segmentation)
- 对话段落在 AI 从 `ONLINE` 进入 `SEMI_ONLINE` 时自动创建。
- `messages` 表的 `session_id` 为 `NULL` 表示消息属于当前活跃对话。
- 段落摘要异步生成，短对话可能在摘要完成前 AI 已进入下一轮对话。
- 旧数据库会自动迁移（`ALTER TABLE` 添加 `session_id` 列），无需手动操作。
- `get_context_messages()` 在跨段落消息之间自动插入分隔标记，帮助 LLM 理解时间结构。
- `clear_chat_history` / `clear_all_history` 会同时清理 `conversation_sessions` 表。

### ❌ 本轮用户消息在模型输入里出现两次（AI 反馈"你发了两次"）

所有大脑的输入都是「历史上下文 + 当前用户消息」，而当前用户消息在生成前就已写进
`messages` 表，因此历史里必然包含它一次，**必须在拼 history 时剔除**。

- **不要按字符串相等判断。** `add_message` 会改写入库内容：时间戳前缀、跨地点来源标签
  `[来自 …]`、表情包描述 `[表情包: …]`；群聊提升批次更会把多条消息重排成另一种格式。
  每新增一种内容加工，字符串比较就重新失配，去重静默失效——这个问题因此活了好几个月。
- **正确做法：按行 id 去重。** 写库时把 `add_message` 返回的自增 id 记进 context
  （`core/message_dedup.py` → `record_persisted_user_message`），下游用
  `drop_current_user_message(db_context, context, fallback)` 排除。内容怎么加工都不影响，
  也能覆盖"一次输入对应多条库记录"的群聊批次。
- **新增 `role="user"` 的 `add_message` 调用点时，必须同时记录返回的 id**，否则该路径又会退化
  成字符串兜底。context 被改写成别的指示时（如轮询 continue 分支）要调用
  `clear_persisted_user_messages`。
- **剥时间戳只用 `core.routing.strip_timestamp_markers`**，不要在各处再写窄正则。历史上曾有三份
  只认分钟精度的副本，而实际格式默认带秒和星期（`%Y-%m-%d %H:%M:%S %A`），全部失配。
- context 常被 `dict(...)` / `.copy()` 浅拷贝传递，所以 id 列表是**整体重绑**而非原地 append，
  避免串改上游 context。

---

## 4. 流式输出 / [SPLIT]

### ⚠️ `[SPLIT]` 与 `[SPLIT:秒数]` 只在分段发送链路中生效
- `[SPLIT]` 分段逻辑在发送链路中处理；`[SPLIT:秒数]` 会在段间增加显式延迟（例如 `1.5` 秒）。
- 如果 LLM provider 走 legacy `str` 分支且未输出 `[SPLIT]` 标记，不会自动分段。
- 要让模型输出 `[SPLIT]` 或 `[SPLIT:秒数]`，靠 `system.jinja` 中的“消息节奏协议”引导。

### ⚠️ 工具调用时清空缓冲
- 检测到 `tool_call` 时会清空 `response_text_buffer`，避免"思考过程"泄漏给用户。
- 但如果工具调用前已经通过 `[SPLIT]` 发出了部分文本，这些**已发送的无法撤回**。

---

## 5. Telegram 适配

### ⚠️ Telegram Token 不在全局 config.yml
- Telegram 的 `bot_token` 已迁移到 `adapters/telegram/config.json`。
- `python cli.py --configure` 不再询问 Telegram Token；新 adapter 也应使用自己目录下的 `config.json`。

### ⚠️ 消息长度限制
- Telegram 单条消息上限 4096 字符。超长文本由 `_split_long_text()` 自动按段落/行/字符边界切割。
- 与 `[SPLIT]` 是两套独立机制：`[SPLIT]` 是语义分段，`_split_long_text` 是硬限制兜底。

### ⚠️ 群聊响应条件
- 群聊中暂时只有被 @机器人时才响应；单纯回复机器人不会触发。已触发消息仍会像私聊一样提取 reply 内容。
- `storage_id` 隔离：私聊用 user_id，群聊用 chat_id。

## 5.1 OneBot v11 适配

- OneBot v11 配置在 `adapters/onebotv11/config.json`，支持正向 WebSocket (`connection_type="websocket"`) 和反向 WebSocket (`connection_type="reverse"`)。
- NapCat 扩展 API 默认不暴露；只有 `enable_napcat_api=true` 时，后脑才会看到 `onebotv11_napcat_*` 工具。
- 群聊默认只处理 @机器人或回复机器人消息；可通过 `group_message_policy` 调整。
- 群管/撤回/全员禁言/改群名片等真实 QQ 操作必须先向主人确认，且实际成功取决于登录号权限和 OneBot 实现。

### ⚠️ 聊天记录（合并转发）里的图片不要去解析

`adapters/onebotv11/forward.py` 展开聊天记录时，内部媒体**只留 `[图片]` 占位**，
不下载、不进 ImageStore、不调 `get_image`。这是有意的：一条记录可能嵌套几十张图，
全量下载会拖垮入站链路并污染图库。改动时不要顺手"补上"媒体解析。

- 入站预算 `INBOUND_MAX_*`（20 节点 / 1500 字 / 3 层），工具路径 `FULL_MAX_*` 更宽。
  两套常量不要合并——入站要控上下文体积，工具是用户明确要全文。
- 节点结构各实现不一致（`data.content` / `messages` / `message` 都有），
  发送者名要按 `nickname → card → user_id` 回退，别只认一种。

---

## 5.1.1 AI 引用（reply）的消息不对

三个各自独立的根因，都是"模型拿到的 ID 本身就错了"，不是模型判断力问题：

- **被回复的历史消息 ID 混进了当前入站 ID。** OneBot 在 `main.py` 里把
  `reply_to_message_id` 塞进 `platform_message_ids` 且**排第一位**（供媒体反查），
  模型会把它当成当前消息去 reply。`core/scene_context.py` 的 `_own_inbound_message_ids()`
  负责剔除——新增任何"往 platform_message_ids 里塞非本轮 ID"的路径时都要同步这里。
- **聚合轮只给裸 ID 列表。** 正文被换行拼成一整段，`101, 102, 103` 无法对应到具体哪句。
  现在 part 带 `text_preview`（`adapters/aggregator.py`），场景块逐条列出。
- **合并/折叠分支丢 ID。** 媒体轮折叠、群聊提升批次都是从"某一条" context 复制出来的，
  不显式合并就只剩那一条的 ID。`back_brain_input_context` 快照里要带 `platform_message_ids`，
  `_group_batch_context` 要遍历所有 event 合并。**新增任何合并分支时都要检查这一项。**

> 未修复：`[reply:ID]` 只校验格式不校验存在性（`adapters/message_controls.py:42`）。
> 模型幻觉的 ID 会直达平台，表现为静默失败或引用到无关消息。

## 5.2 群监听 ONLINE 退不出去 / 没被 @ 的群也在监听

这两个现象是同一条链：**ONLINE 一旦点亮，能关掉它的路径极少**；而只要某群挂着 ONLINE，
两个 adapter 就对它整群放行（`adapters/onebotv11/main.py` 的 `online_group` 分支、
`adapters/telegram/incoming.py` 的同类分支），看起来就是"没 @ 也在监听"。

历史上同时踩到的坑（均已修复，改动时不要退回去）：

- **`GroupPresenceStore` 曾无任何时间过期。** 只有 `normalize_single_online()`，而它的语义是
  "保留最近更新的 ONLINE"——保护而非清理。几天前进 ONLINE 的群重启多少次仍是 ONLINE。
  现在启动时先跑 `expire_stale_entries()`（6 小时，对齐 `PrivatePresenceStore`）。
- **窗口为空时不再评估。** `_idle_wait` 旧版在 `not pending_window` 时直接 return，而
  "被 @ → 回复 → 群安静"恰好清空窗口（定向路径会 `_take_recent_pending` 抽走待判断消息），
  于是最安静的群反而最不可能被关掉。现在空窗口走 `_demote_reason` 硬超时判定。
- **并发后缀让 SEMI_ONLINE 结论静默作废。** fast 调用期间到达任意新消息就丢弃结论，活跃群里这是常态。
  现在连续 `semi_online_vote_threshold` 轮都投 SEMI_ONLINE 时按当前窗口末尾强制降级。
- **滞留的 `reply_to_bot` 永久否决降级。** 生成期到达的 reply 会经 `finish_generation` 落进
  pending window，旧版 `any(explicit_bot_mention or reply_to_bot)` 无时效，会卡到它被 20 条上限挤出。
  现在按 `directed_veto_seconds` 只否决近期定向事件。
- **分类器拿不到时间信息。** `passive_user` 旧版只给窗口计数，模型客观上无法知道已经听了几小时——
  "主动性不够"不是模型判断力问题。现在注入 `listening_stats()`：已监听秒数、距最近互动秒数、
  距最近消息秒数、连续 KEEP_LISTENING 轮数 + 两个参考阈值。新增分类信号时记得同步 prompt 与渲染参数。
- **看门狗必须显式启动。** `controller.start_triggers()` 调 `group_listener.start()`。重启后若某群
  仍持久化为 ONLINE 但一直没有新消息，懒启动（`receive` / `set_mode`）永远不会触发。
- **`set_mode` 必须带 platform 元数据。** 缺省时由 runtime_key 拆分补齐，否则持久化记录里
  `platform` / `platform_chat_id` 是空串，任何基于记录的巡检都拿不到平台。

排障先看日志：`群监听 fast=... keep_streak=N semi_votes=N`、`群监听拒绝近期定向窗口的 SEMI_ONLINE 决策`、
`群监听空窗口静默降级`、`群监听看门狗强制降级`、`启动过期清理: N 个群 ONLINE 记录已重置`。

---

## 5.3 view_media 回查媒体：分析轮串味 / 图片太多 / 跨轮误判重复调用

三个现象都出在「工具回查媒体 → 下一轮用 image/video 模型看图 → 结果回灌给正常模型」这条链上。

### ❌ 回查结果带着 Nora 的口吻，把 `question` 当成用户在搭话

分析轮如果沿用 `get_system_prompt()`，`system.jinja` 第一行就是人设/SOUL，再叠上对话历史，
模型自然按对话角色回应，回一句寒暄而不是客观分析。

- 该轮走 `brain/templates/media_analysis.jinja`（无人设的"媒体内容分析引擎"）。
- **代码侧必须同时满足三条，缺一条人设都会漏回来**（`core/back_brain.py`）：
  1. `turn_system_prompt` / `turn_user_prompt` 都换成模板的两个 block（只换 system 不够，
     user prompt 里还带着正常轮的对话上下文）；
  2. `turn_history = []` —— 留着历史，模型会从上文自己学回 Nora 的语气；
  3. 该轮不给工具，且流式产出**既不发用户也不进 `temp_history`**
     （`[SPLIT]` 分支里 `if tool_media_analysis_round: continue`）。少了这条，
     内部观察数据会被当成回复直接发出去。
- 回灌时的措辞也要管：交回去时写"这是你自己亲眼看到的事实"，否则正常轮会复述成
  "分析显示……/报告指出……"这种转述腔。
- 模板里显式禁止输出 `[IMAGE_TAGS]` / `[IMAGE_OCR]` / `[IMAGE_DESC]` / `[VIDEO_TAGS]`——
  那些只用于用户**新发**媒体入库，回查的历史媒体不需要，否则标签块会污染观察结果。

### ⚠️ 搜到一堆图时全量回灌

`view_media` 命中 10+ 张时，旧实现有几条 MediaTag 就传几张，单轮请求爆 token 且注意力被摊薄，
"同时查看+提问"的准确率反而下降。现在有单轮上限：图片 4 个（带 `question` 时 3 个）、视频 1 个
（`core/back_brain.py` 的 `MAX_TOOL_IMAGES_PER_TURN` / `..._WITH_QUESTION` / `MAX_TOOL_VIDEOS_PER_TURN`）。
被截断时 `tool_result` 会附提示引导收窄查询或用 `page` 翻页，`media_truncated_count`
也会带进分析轮 prompt，让模型知道自己看的是子集。

### ❌ 前脑审查 continue 后，上一轮用过的工具第一次调用就被提示"3 次调用"

`sessions[chat_id]` 是 controller 级别的**长期** per-chat 字典，而 `last_loop_key` /
`tool_loop_call_count` / `file_edit_counts` 记录的是「本次后脑连续调用」。
`core/polling.py` 的 continue 分支只 `context.copy()`，session 原样带进下一轮，
计数就从第 4、5 次起算，直接命中 hint/force 干预。

- 修复：`_reset_tool_loop_tracking()` 在 `_generate_response` 入口调用。
- 注意 `_reset_repeated_tool_call()` **不清** `last_loop_key`，别只调它。
- 新增任何"跨本次生成才有意义"的 session 计数器时，记得一并加进这个 reset。

## 5.4 图片和紧随其后的文本各回一次

现象：先发一张图（走后脑 image 轮），紧接着补一句文字说明，Nora 回了两次。

- **不是聚合器的问题。** 图片以 `[image: path]` 文本形式过聚合器，3 秒窗口内到达的图+文本来就能合并。
- 真正的窗口在聚合之后：`core/message_handler.py` 的合并逻辑只在 `backend_busy_or_queued`
  为真时生效。一旦 image 轮在后续文本到达前**跑完**，媒体快路径和合并分支双双跳过，
  文本就成了全新一轮。
- 现在由 `_media_turn_fold_target()` 判定能否折进正在跑的媒体轮，命中则走原有
  cancel + 合并 + 重启路径。**五道闸门不折**，改动时不要放宽：轮询模式、已对用户发过可见回复
  （折 = cancel + 重写，用户会先看半截再看完整版）、该轮已执行过工具（副作用会被重跑）、
  超出 `MEDIA_TURN_TEXT_FOLD_WINDOW`（90 秒）、群聊里发送者不是同一个人。
- 合并分支要同时合 `multimodal_images` **和** `multimodal_videos`——历史上只合了图片，视频被丢。
- `[Image #N]` 是模型自己的措辞（指代历史里的图片），不是代码插入的标记，别去搜代码。

## 5.5 日志时间和调度器时间对不上

同一件事日志行写 `02:37:00`，APScheduler 写 `scheduled at 14:37:00+08:00`，
容易误判成"任务提前 12 小时触发"。

- 原因：`logging.Formatter` 的 `%(asctime)s` 默认用系统本地时区，调度器用
  `memory.message_history.timezone`。现在 `brain/logging_config.py` 的 `_DisplayTimezoneMixin`
  让两个 Formatter 都走配置时区。
- `cron[minute='']` **不是 bug**：注册的是 `CronTrigger(minute="*")`，实测渲染为 `cron[minute='*']`，
  空值是终端复制产物。
- `apscheduler.executors` / `apscheduler.scheduler` 已降到 WARNING：群监听 tick 是每分钟一次的
  cron job，executor 每次触发打两条 INFO 会把日志刷成流水账。要看调度信息请找
  `core/scheduler.py` 自己的打点。

## 5.6 形象生图：标记泄漏 / 图追到别处 / 形象每次都变

- **新增前脑标记必须改两个解析器。** `core/routing.py` 有 `parse_front_brain_response()`（主对话）
  和 `parse_front_brain_review()`（轮询审查）两份近乎相同的实现。只改主解析器，审查轮就会把
  `[DRAW:...]` 当字面文本发给用户。`sanitize_adapter_output_text()` 里还有一层兜底剥离，
  覆盖后脑最终文本、主动消息等所有其它发送路径。
- **生图触发点的位置不能挪。** 在 `core/message_handler.py` 里它必须夹在
  send_front_reply 块**之后**（要用那里算出的 `send_target`，否则图追不到文字实际去的目标）、
  `_apply_presence_markers()` **之前**（`presence_ended` 会提前 `return`，放后面遇到进半在线的
  轮次就永远不触发）。投递目标被拒绝时（`send_target` 有值但 `runtime_key` 为空）也不要生图。
- **没有参考图，每次生成的脸都不一样。** 参考图是形象一致性唯一的锚。链路本身不会报错，
  只会在日志里 warning，然后画出一个陌生人。想稳定就先用 `generate_appearance_reference`
  生成 `face` 等视角。
- **`appearance/refs/` 是 `_is_path_safe` 里唯一的目录级规则**，而不是文件名黑名单。
  规则用的是 `abs_path` 归一化后的 `/appearance/refs/` 子串匹配，所以不要把该目录改名或挪层级，
  否则拦截会静默失效。`manifest.json` 也在拦截范围内。**连带影响**：`refs/` 下的图不能作为
  `generate_appearance_reference` 的 `source_images` 传入（会被同一条规则拒）——已有参考图
  工具本来就自动带，不需要手动指定。
- **来源图和已有参考图在提示词里必须分开点名。** 两者都以附图形式进 `generate_image`，
  但语义相反：来源图是"照着它长"（与文字描述冲突时以图为准），已有参考图是"保持同一个人只改视角"。
  合成一段说明，模型就分不清哪张该照抄，结果既不像来源图也不像旧形象。工具按
  source → anchor 的固定顺序传图，提示词里用 FIRST N / LAST N 指代——**改动传图顺序时
  必须同步改提示词措辞**。
- **`draw` 和 `draw_desc` 必须成对配置。** 只配一个等于没配：`draw_models_configured()` 要求两者
  都有，否则前脑提示不注入 `[DRAW:]` 说明、参考图工具也不注册（避免模型输出无法执行的标记）。
- **`draw_api` 和 `draw_prompt_style` 是两件互相独立的事，别当成一个。** 前者是**协议**
  （`/v1/images/*` 还是 `/v1/chat/completions`+modalities），选错直接 404 或拿不到图，只对 openai
  类型 provider 有意义。后者是**提示词写法**（自然语言句子 / 逗号分隔标签），选错**不报错**——
  照样出图，只是标签串喂 nano-banana 会丢掉空间关系、长句喂 SD 会被 CLIP 截断，表现为"图能出但总画不对"。
  同一个 openai 端点后面既可能挂 nano-banana 也可能挂 SD，所以 CLI 里 `draw_api` 只在 provider 是
  openai 时问，`draw_prompt_style` 配了 `draw` 就问。
- **`draw_edit_encoding` 决定 images.edit 的请求体格式，选错是 415 不是静默降级。**
  官方 SDK 的 `images.edit` 走 **multipart/form-data**（`openai/resources/images.py` 里
  `extract_files()` + 写死 `Content-Type: multipart/form-data`），而不少中转站把
  `/v1/images/edits` 实现成只收 JSON，于是 SDK 一发请求就
  `415 图片编辑仅支持 application/json`。设 `json` 时走 `_images_edit_json()` 手写请求体
  （不经 SDK），415 会自动退回 multipart 保底。**只影响带参考图的图生图**——纯文生图走
  `images.generate`（本来就是 JSON），不受此项影响，所以现象是"有时能出图有时 415"。
- **JSON 直传的字段形态是实测出来的，不是猜的，改之前先看这张表**（wrapi / newapi 系，2026-08 实测）：

  | 请求写法 | 结果 |
  |---|---|
  | `image: {"url": "data:image/png;base64,..."}` | ✅ 200 |
  | `image[0]: "data:..."` | 400 `image 不能为空`（不认下标写法） |
  | `image: "data:..."` | 422 `expected struct ImageUrl`（要对象不要裸串） |
  | `image: [{"url": ...}]` | 422 `invalid type: map`（不吃数组） |

  所以**参考图只能传一张**——多张形象锚传不进去，这是端点限制不是代码取舍，多余的会被
  丢弃并打 warning。
- **JSON 直传必须显式要 `response_format: "b64_json"`，否则拿到的 url 很可能下不下来。**
  不带这个参数时端点回 `{"url", "mime_type"}`，而图托管在模型厂商自己的 CDN
  （`grok-imagine` 回 `imgen.x.ai`）——那些域名在国内机器上直连不通，表现为生图"成功"了
  却卡在下载并最终 `ConnectTimeout`。带上该参数后直接回 base64，绕开 CDN。
  端点不认这个参数会照旧回 url，url 分支仍保留作兜底。
- **生图的 mime 一律按魔数认，不要硬编码 `image/png`。** `grok-imagine` 回的是 **JPEG**，
  而 `b64_json` 分支曾写死 png，落盘就会得到"扩展名 .png、内容是 JPEG"的文件。
  url 分支同理——CDN 链接常带查询参数或没有扩展名，按后缀猜同样会错，统一走 `_guess_mime()`。
- **httpx 和 aiohttp 的响应属性名不一样，混写会 AttributeError。** httpx 是
  `.status_code`，aiohttp 是 `.status`。两个库在同一函数里做主备时必须归一化
  （现在统一走 `_post_json_raw()` 返回 `(status, bytes)`），否则备用分支一旦被触发就崩，
  而这条路平时跑不到、测试也容易漏掉。
- **写法分支有两条路，改一条不够。** `[DRAW:]` 走 `draw_desc.jinja`（`prompt_style` 变量，
  **system 和 user 两个 block 都要传**——`render_template` 每个 block 独立渲染，不共享 context）；
  参考图工具 `generate_appearance_reference` 不过 `draw_desc`，提示词在 `brain/tools.py` 里硬拼，
  自己判 `get_draw_prompt_style()`。漏掉后者的话，标签系模型会把 "Generate a character reference
  image…" 这类整句指令当画面内容画进图里。
- **三份输入的边界一破，图就画不对。** `APPEARANCE.md`（长什么样）/ `STYLE.md`（画成什么样）/
  `[DRAW:要求]`（画什么）各有唯一权威。历史上前脑自己往要求里写"棕色长发、白色针织衫、写实风格"，
  两个后果：它凭记忆写的外貌和 `APPEARANCE.md` 打架；三类信息混在一句里，`draw_desc` 分不清
  谁该服从谁。现在 `front_brain.jinja` 明确禁止在要求里写外貌和画风，`draw_desc.jinja` 顶部有
  同样的三列表并写明"要求里若顺带出现外观或风格，以另两份为准"。**改这三处提示词时要同步**，
  只改一处会让约束和实际行为错位。只有"这一次特意不一样"的东西（临时换的衣服、头发湿的）才进要求。
- **`STYLE.md` 在两条生图路径上取的层次不同，别统一。** `[DRAW:]` 日常拍照要整份（含拍摄方式、
  光线、`4:3` 比例）；`generate_appearance_reference` **只取渲染风格那一层**，明确 IGNORE 拍摄方式、
  布光和比例（`brain/tools.py` 里那段 `Apply ONLY … IGNORE …`）。参考图必须是中性形象锚——
  平背景、均匀光照、正对镜头。让参考图也吃"手机随手拍"，那次的场景味道会顺着锚污染之后所有生图。
- **`STYLE.md` 不注入 system prompt。** 它只在两条生图链路里读（`read_style_text()`）。
  前脑不需要知道画风——它只负责写"画什么"。顺手把它加进 `load_identity_context()`
  等于又给了前脑一份可以往要求里抄的画风描述，正好是上面那条要避免的。
- **生图失败不向用户追发任何文本**（用户的约定）。文字已经发出去了，图没来就是没来；
  排查看日志的 `生图失败` 行，`draw_prompt` 落在 Mongo `appearance_images` 里。
- 生图是旁路任务，不在 `generation_tasks` 里。`/stop` 与 `shutdown()` 需要单独调
  `cancel_appearance_image_task()`，否则停完还会冒出一张图。

---

## 5.7 发完图/表情包之后 AI 就再也不吭声了（followup 静默死亡）

现象：发一张图或表情包，Nora 回一句（甚至只回一句报错），然后彻底 idle，
不再有任何 follow-up。**根因不是 followup 没被 arm**——定时器起来了，
是它醒来时读到的历史是残缺的。

三个各自独立、但都通向"历史留白"的坑：

- **媒体消息入库正文为空。** `[image:...]` / `[sticker:...]` / `[video:...]` 被
  `extract_*_payloads` 剥光后，只发图不配文字的消息入库就只剩时间戳前缀。
  `_detect_followup_intent`（`core/scheduler_mixin.py`）拼出的 `recent_conversation`
  是「用户空消息 + AI 没吭声」，要么命中 `if not recent_conversation: return "END"`，
  要么 fast 模型判 END；END 且 `count == 0` 就跳过 wrapup 静默进 SEMI_ONLINE。
  现在由 `media_placeholder_text()`（`core/message_handler.py`）补 `[图片]` /
  `[表情包]` / `[视频]` 占位，接在 `group_message_content()` 里，覆盖所有入库路径。
- **IMAGE_TAGS 重试失败会清空 `final_response_buffer`。** 清空后 4.5 段的
  `if final_response_buffer:` 不成立，那条硬编码的"图片输出异常"提示**发给了用户
  但没入库**——用户看见了，模型的历史里却是空白。现在失败分支会把提示文本写回
  buffer，让它照常落库。
- **折叠路径注入不到表情包描述。** `core/back_brain.py` 的注入条件是
  `not message_saved`，而媒体折叠/忙碌合并分支（`core/message_handler.py`）
  写库时就把 `_message_saved` 置 True 了，注入永远不会发生。现在那条分支
  自己调 `_describe_stickers()` 就地拼描述。

排障顺序：先查库里那一轮的 user/assistant 两条消息内容是不是空的，
再看日志有没有 `检测到对话已自然告别，followup_loop 静默退出`
（那是另一条路径——`[TASK_DONE]` 在 `_GOODBYE_PATTERNS` 里，
前脑轮会被 `core/routing.py` 剥掉，媒体快路径绕过前脑所以不会剥）。

## 5.8 主动消息发给了最后一个私聊的陌生人

`ProactiveScheduler._resolve_delivery_target()` 里 `default_chat_id` 早已被降级为
**兜底 fallback**：先问 `resolve_delivery_runtime_key()`，它返回
`load_last_active_runtime_key() or fallback`。而 `record_active_scene()` 过去对
**任何**私聊都会刷新 `last_active_runtime_key`。于是陌生人私聊一次就抢走了投递端，
当天的主动消息发给他。`docs/HANDOVER.md` 里"投最近活跃端"的设计假设是
"只有主人会私聊"，有陌生人之后这个假设就破了。

- 现在 `update_last_active_target()` / `record_active_scene()` 都过
  `_may_own_delivery_endpoint()` 闸门，非主人的私聊只记活跃场景、不改投递键。
  `default_chat_id` 的自动初始化（`core/message_handler.py`）同样只认主人私聊。
- **闸门必须能降级。** `is_owner` 未注入 resolver 时恒为 `False`
  （`core/conversation_identity.py`），直接 `if not identity.is_owner` 会把全部流量挡死——
  包括 `core/controller.py` 里 "主人识别回调注入失败" 那个 except 触发之后。
  所以判定要先查 `owner_resolution_available()`，解析器缺失时退回旧行为。
  改这类"仅主人"闸门时都要照做，否则功能会静默锁死而不是报错。
- 相关隐患（未改）：`core/owner_registry.py` 里**每平台第一个私聊者自动绑为主人**。
  陌生人比你先私聊某个新平台，他就进了 `owner_bindings.json`。
  要杜绝可在 `config.yml → owner.identities` 预声明。

---

## 5.9 引用表情包不触发 fast-image 识别

现象：直接发表情包时 Nora 能读懂它的情绪；**引用/回复**一条表情包消息时，
她只看到一个没有内容的占位符。

`[sticker: path]` 是驱动整条 fast-image 链路的**唯一载体**：
`extract_image_payloads` 只认这个标记，`_describe_stickers`（`core/back_brain.py`）
只处理它产出的 `is_sticker=True` 负载。

引用路径上这个标记原本无从还原，因为两层信息都被有意丢弃了：

- 表情包**故意不进 ImageStore**（`QUICK_REFERENCE` §3），所以没有 `image_id`
  可以反查——`_pick_image_id_from_metadata` 这条线对表情包天然是空的。
- 入库正文被 `media_placeholder_text`（`core/message_handler.py`）收敛成裸
  `[表情包]`，路径没有落库。`_extract_reply_info` 从历史里取回来的就是这个裸占位符。

所以修复只能是**重下文件**：`adapters/telegram/reply.py` 的
`_redownload_replied_sticker_as_input()` 照 `_redownload_replied_photo` 的模式
重新下载并重建标记。**这个分支必须排在历史查找之前**——历史查找会先命中并返回
裸 `[表情包]`，把 sticker 分支彻底挡掉（`tests/test_telegram_reply_sticker.py`
有一条测试专门锁这个顺序）。

- 重建的格式必须和 `_handle_sticker`（`adapters/telegram/incoming.py`）**逐字一致**：
  emoji/贴纸包那行给模型看语义，路径那行才是载荷。少了后者，标记等于没重建。
- 命名沿用 `sticker_<file_id>.<ext>`，同 file_id 不重复下载。
- OneBot v11 走的是**另一条路但不用修**：`_enrich_reply_text` 调的是和直发消息同一个
  `segments_to_nora_text`，`media.py` 的 `mface`/`marketface` 分支产出的就是
  `[sticker: path]`，标记是对的。（`message.py:176` 那个 `[表情包:{file}]` 只是
  给人看的摘要函数，不在入站链路上——别被它误导，我曾照它误判过一次。）

---

## 5.12 表情包被路由到后脑（前脑没走、后脑没图）

**表情包不算"图片输入"。** 它的设计是：走 fast-image 生成一句话 desc 带进**前脑**，
真图不进主图模型（`core/back_brain.py` 按 `is_sticker` 过滤掉，也不进 ImageStore /
标签 OCR / 向量图库）。

历史 bug（2026-08-17 生产复现）：`core/message_handler.py` 里
`image_input_detected = bool(multimodal_images)`，而 `extract_image_payloads`
**把 `[sticker:]` 和 `[image:]` 一起提取**（只是多打个 `is_sticker=True`）。
于是纯表情包消息满足"有图片输入"，被直接路由到后脑并 `return`，**跳过整段前脑**；
到了后脑，那张图又刚好被 `is_sticker` 过滤掉。两头落空：

> 前脑没走 → 没有即时回复链路；后脑没图 → 退化成一轮没有任何图片的纯文本轮。

现象很好认：问"这个表情包是什么内容"，回一句"具体画面我看不到"，
日志里是 `Turn 1 模型: coder` 而不是 `image`，且全程只有一条 coder 的 cost 记录。

- 判据必须是**排除表情包之后**的图（`has_real_non_sticker_image`）。
  混合消息（真图 + 表情包）仍然算图片输入，行为不变。
- 但 `has_image_marker and not has_real_image`（"标记有但没加载到"）要继续用
  **含表情包**的 `has_real_image`——否则一个成功加载的表情包会被误判成加载失败，
  给模型注入 `image_load_failed`，让它以为用户发图失败了。
- `context["multimodal_images"]` **仍要带着表情包**，别为了修路由把它摘掉——
  `_describe_stickers` 全靠它，摘了 desc 永远为空。
- desc 必须在**前脑路径上就地生成**：后脑那套注入的条件是 `not message_saved`，
  而前脑路径马上就把 `_message_saved` 置 True，注入永远不会发生。
  生成后要同时回填 `context["text"]`，只改 `message_content` 的话入库有 desc、
  **当轮前脑却看不到**（前脑读的是 context）。
- `_describe_stickers` 走 `brain/templates/sticker_analysis.jinja`（无人设的
  "表情包内容识别引擎"），要求「画面 + 情绪」两者都给。**该轮故意不走
  `_chat_stream_wrapper`**——那个包装会往 system_prompt 里注入词库全局说明和系统
  环境信息（时间/ChatID/OS/Python 版本），对"看图说一句话"全是噪音，正是它把无关
  上下文带进了描述。历史实现是一句内联 prompt 只问"情绪或含义"，所以用户问
  "这是什么内容/哪个系列的梗图"时描述里没有可用信息，且模型容易写成一句主观感想。
  和 `media_analysis.jinja` 同源同理：输出不是给用户看的话，是注入上下文的观察数据。
- OneBot 的 `_enrich_reply_text` 判断"媒体"时**必须排除表情包**（用
  `message.py` 的 `is_media_segment()`，它内部调 `is_sticker_segment()`）。
  置位 `reply_to_contains_media` 会带来两个坏处：被引用消息 ID 补进
  `platform_message_ids` 但表情包没入 ImageStore，`view_media` 查不到、补了是空；
  更糟的是附上 `HISTORICAL_REPLY_MEDIA_NOTE`「上方媒体就是被回复消息里的真实内容」，
  而模型其实拿不到那张图——等于邀请它幻觉。
  引用表情包应该和直发完全一样：下载 → `[sticker: path]` → fast-image 出 desc → 前脑。

**判断表情包段只用 `is_sticker_segment()`，别再手写那三个字段的 fallback。**
QQ 的表情包段有两种形态：`mface`/`marketface`，以及 `subType`/`sub_type`/`type`
任一为 `1` 的 `image`。这三个键名在不同实现（NapCat / go-cqhttp / Lagrange）里不一样，
必须全试。这段逻辑曾在 `media.py`、`message.py`、`forward.py` 各抄一份，已统一。

`tests/test_sticker_routing.py` 用源码级断言锁住上面每一条（这个 bug 的本质就是
一个布尔量的取值来源，跑完整 `handle_new_message` 要拉起 DB/LLM/presence 一大套依赖）。

---

## 5.10 draw_desc 被安全策略拦截后，错误提示被当成生图提示词

现象（实测日志）：

```
[OpenAI chat/draw_desc] 内容被安全策略拦截 finish_reason=prohibited_content
draw_desc 未输出 [DRAW_PROMPT] 区块，整段文本作为提示词。
生图开始: prompt=Error: 模型因内容安全策略拒绝生成（finish_reason=...）
```

根因不在生图链路，而在 provider 的失败约定：**失败时返回一段错误文本，而不是抛异常**
（历史设计，好让对话链路能把原因说给用户）。于是 `_resolve_draw_prompt`
（`core/appearance_gen.py`）的「没包 `[DRAW_PROMPT]` 就整段当提示词」兜底分支
把那句错误说明原样喂进了文生图模型。

- 判据是 `client.last_error`（`brain/interface.py` 的 `_begin_call()` / `_fail()`），
  `BaseLLM.is_error_result()` 的 `Error: ` 前缀只是第二道闸门。
  **不要靠匹配返回文本判断**——各 provider 的失败文案互不相同、也会改。
- `check_finish_reason_and_log` 已从 classmethod 改成实例方法，就是为了在返回错误
  文本的同时记上 `last_error`。新增 provider 时记得在入口调 `_begin_call()`，
  失败分支走 `_fail()`，否则该 provider 的失败对调用方不可见。
- **凡是把 `chat()` 结果当"产物"而不是"给用户看的话"的调用方，都要检查这个信号。**
  除了 draw_desc，参考图挑选、各类判定轮都属于这一类。截断（`finish_reason=length`）
  不算失败：正文仍可用，只是不完整。

---

## 5.11 推理强度（effort）配了没生效 / 直接 400

`llm.effort` / `llm.effort_by_alias` 存的是**统一档位字符串**
（`none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`auto`，见 `config.EFFORT_LEVELS`），
各家 API 的字段完全不通用，由各 provider 自己翻译：

| provider | 字段 | 取值 |
|---|---|---|
| openai（Chat Completions） | `reasoning_effort` | 档位字符串，**平铺** |
| openai（Responses） | `reasoning.effort` | 同上，但**嵌套** |
| openrouter | `reasoning.effort` | 全档位照收，自己往下游翻 |
| anthropic 4.6+ / sonnet-5 / opus-5 | `output_config.effort` | 档位字符串，**顶层兄弟字段** |
| anthropic 4.5 及更早 | `thinking.budget_tokens` | 整数预算，且**必须小于** `max_tokens` |
| gemini 3.x | `thinking_config.thinking_level` | `MINIMAL`/`LOW`/`MEDIUM`/`HIGH` 枚举 |
| gemini 2.5 及更早 | `thinking_config.thinking_budget` | 整数预算（`-1`=dynamic） |
| gemini（REST 视频路径） | `thinkingConfig.thinkingLevel/Budget` | 同上，但**camelCase** |

**档位是按「模型」支持的，不是按「家」。** 这是最容易踩的一层：同一家不同代次
认的字段和值都不一样，猜错的后果通常是 400 而不是降级。三条已经踩过的：

- **Anthropic 2026 架构变了，effort 不在 `thinking` 里。** 现在是两个字段：
  `thinking` 管模式（`enabled`/`adaptive`/`disabled`），`output_config.effort` 管强度。
  **Opus 4.7+ 收到旧的 `{"type":"enabled","budget_tokens":N}` 直接 400**
  （`thinking.type.enabled is not supported for this model`）。
  `adaptive` 是模式名不是档位名，别当 effort 值传。
- **Gemini 3.x 必须用 `thinkingLevel`，2.5 必须用 `thinkingBudget`**，两个方向传错都报错。
  3.x 关不掉思考，所以 `none` 只降到 `MINIMAL` 而不是发 `budget=0`；
  Gemini 只有 4 档，`xhigh`/`max` 一起降到 `HIGH`。
- **Responses API 要嵌套 `reasoning={"effort":...}`**，平铺 `reasoning_effort` 会
  `400 unsupported_parameter`。`_filter_supported_kwargs` 救不了这个——`reasoning`
  本身是合法顶层参数名，它不知道里面该放什么。

代次判断靠**模型名子串**（`_supports_native_effort()` / `_uses_thinking_level()`），
一定会有猜错的时候（网关改名、新模型没进列表），所以三家都必须有"摘掉字段重试一次"
的兜底（`_is_effort_rejection` / `_strip_effort_fields`）。**别把重试分支删掉**——
否则一个可选调优字段会让整个别名不可用。判断代次时**用反向匹配**（"是不是老型号"）
而不是枚举新型号：新模型只会往上出，枚举必然过期，过期就是新模型直接报错。

⚠️ **流式路径的重试只能发生在第一个 yield 之前**（`emitted_any` 闸门）。
400 是建流那一刻抛的，正常不会冲突；但流中途断的话重试会把已发给用户的文本再发一遍。

其余"配了但静默无效"的坑：

- **Gemini 有两套命名，不能合并。** SDK 的 `generation_config` 走 snake_case，
  而 `_video_stream_via_rest` 直接拼 REST body，必须 camelCase。
  写错的一侧**不报错**，只会被 API 忽略——看起来配了却没生效。
- **`none` 是合法档位，不是"未配置"。** 判据必须是 `is None`；写
  `if not self.effort` 会把 `none` 档（显式关闭思考）一起吞掉。
  配置层同理：`get_llm_effort()` 返回 `None` 表示不传字段，返回 `"none"` 表示传关闭。
  预算侧则是 `None` vs `0`。
- **`auto` 是本项目自己的第 8 档，不是任何一家的合法字符串值。** 语义是"让端点自己
  决定"，各家翻译成自己的原生自动模式：openai 不传字段 / gemini 2.5 用
  `thinkingBudget=-1` / anthropic 新模型用 `thinking={"type":"adaptive"}`。
  `EFFORT_BUDGET_TOKENS["auto"] = -1` 是 Gemini 的 dynamic 哨兵，**不是"负预算"**——
  不吃这个哨兵的 API（anthropic 旧模型）必须自己判负数，别直接塞进 `budget_tokens`。
- **`ultracode` 不是档位。** 那是 Claude Code 的编排关键词，没有任何 API 对应物。
  有测试钉死它不在 `EFFORT_LEVELS` 里。
- **Anthropic 的 `budget_tokens` 必须小于 `max_tokens`**，否则直接报错。
  `_apply_budget_effort` 会按 `max_tokens` 夹一下并留 512 token 给正文；
  `max_tokens` 太小时宁可不开思考，也不能发非法请求。
- **关不掉思考的模型：** `fable-5` / `mythos-5` 连 `{"type":"disabled"}` 都 400，
  `none` 档只能降到最低档；Opus 5 的 `disabled` 只在 effort ≤ high 时合法。
- **GPT-5.4+ 在 Chat Completions 里带 function tools 就不能传 `reasoning_effort`**
  （除了 `none`），必须走 Responses——后脑工具循环正是这个组合。
- **改 effort 会失效 Anthropic 的 prompt cache**（配置被渲染进 prompt）。
- **改动在下次创建该模型客户端时才生效。** effort 在 `__init__` 里读进实例字段，
  `/effort` 写完 config.yml 后，已存在的 client 实例仍用旧值。
- 新增模型别名时改 `adapters/telegram/commands.py` 的 `MODEL_ALIASES`
  一处即可，`/model` 和 `/effort` 共用它。
- Telegram 的 `_EFFORT_CHOICES` **直接引用 `config.EFFORT_LEVELS`**，不要另抄一份；
  抄了就会漂移，表现是按钮能点的档位写进 config.yml 后被 `normalize_effort` 判非法丢掉。

## 6. 成本跟踪

### ⚠️ 无内置价格表
- 已移除所有内置价格，完全依赖 `config.yml` → `cost_tracking.custom_prices`。
- 缺价模型**首次**会打 warning，之后不重复；但成本会记为 $0。
- CLI 向导中可现场输入价格并写入配置。

---

## 7. 开发注意

### ⚠️ 不要读 `config.yml` / `.env`
- 系统提示词中明确禁止 AI 读取配置文件（含 API Key）。
- 技能使用 `config.json` + 环境变量注入机制，不直接读取全局配置。

### ⚠️ `edit_file` 失败后立刻切 `write_file`
- 系统提示词约定：`edit_file` 失败一次就换 `write_file`，禁止反复重试。

### ⚠️ 同一文件 `edit_file` ≥ 3 次会被强制引导
- `controller.py` 中有计数器，对同一文件 `edit_file` 达 3 次时，会注入系统提示强制改用 `write_file`。

---

## 8. 工具调用泄漏 (Tool Call Text Leak)

### ❌ LLM 以文本形式输出工具调用，而非真正使用 function calling
- **现象：** AI 在回复中写出 `execute_tool('edit_file', {...})`、`<execute_skill>...</execute_skill>` 等文本，或带 `new_code="""..."""` 的 Python 代码块，而不是真正调用工具。用户看到了原始的工具调用代码。
- **原因：**
  1. LLM 从 system prompt 中的示例和工具描述中"学到"了调用语法，然后以文本形式模仿输出。
  2. **Gemini history 格式不正确**：工具调用/结果以纯文本格式追加到 history，导致 Gemini 在后续轮次中也以文本形式输出工具调用，而非触发 function calling。
  3. 某些模型（特别是 context 较长或多轮对话后）可能退化为文本模式。
  4. System prompt 中工具调用的示例写法可能误导模型。
- **防护层（已实现，四层防护）：**
  1. **Prompt 层：** `system.jinja` 中有"工具调用方式 — 严格规范"条目，明确禁止文本形式的工具调用。已清理所有可被模仿的调用语法示例。
  2. **History 格式层：** `controller.py` 中将工具调用以 `tool_call` / `tool_response` 结构化格式写入 history，provider 会将其转换为原生格式（Gemini: `function_call`/`function_response` parts；OpenAI: assistant/user messages）。
  3. **泄漏拦截 + 自动执行层：** `controller.py` 中 `_try_parse_leaked_tool_call()` 在检测到泄漏时，尝试解析并真正执行工具调用；解析失败则注入纠正提示让 LLM 重新生成。
  4. **最终输出过滤层：** `_is_tool_call_leak()` 和 `_clean_tool_call_leaks()` 在 [SPLIT] 实时分段和最终发送前清理泄漏文本。
- **如果仍然出现：**
  1. 检查 `system.jinja` 中是否有新增的工具调用文本示例——绝不要在 prompt 中写出 `tool_name("arg1", "arg2")` 形式的代码。
  2. 检查 `_TOOL_LEAK_PATTERNS` 是否覆盖了新的泄漏模式，按需添加。
  3. 确认 LLM provider 正确传递了 `tools` 参数到 API。
  4. 检查 `_try_parse_leaked_tool_call()` 是否能解析新的泄漏格式。

### ⚠️ 双脑架构中"反复要求修改但没执行"
- **现象：** 用户反复要求修改文件，但后台显示没有执行工具。
- **原因：**
  1. 当工具泄漏（P1 问题）发生时，后端 `_generate_response` 认为已"完成"（因为没有真正的 tool_call），文件从未被修改。
  2. 用户连续发消息时，可能命中双脑架构的"忙碌"路径，消息被 fast_llm 处理为 queue/queue/queue，没有触发新的完整生成流程。
- **修复：** 解决 P1（泄漏 → 自动执行）后，此问题自动消失。

### ⚠️ LLM 只回复文本，不主动调用工具（工具自觉性差）
- **现象：** 用户请求明显需要工具操作（如"帮我改一下这个文件"），但 LLM 只给出纯文本回复（如"建议您运行以下命令..."、给出代码块让用户自己跑），不调用任何工具。
- **原因：**
  1. 某些模型（尤其上下文较长时）倾向于给建议而非直接执行。
  2. 之前的 `_generate_response` 中，LLM 第一轮不调用工具就直接 `break` 退出循环，没有重引导机制。
  3. 工具 schema 中参数缺少 description，LLM 不确定如何传参时倾向于不调用。
- **防护层（已实现，三层防护）：**
  1. **Prompt 层：** `system.jinja` 中新增"你是执行者，不是建议者"条款，明确禁止回复代码块让用户自己跑。
  2. **Controller 层：** `_should_nudge_tool_use()` 在第一轮无工具调用时，检查回复是否包含代码块（``` ）——这是"建议用户自己跑"的最直接信号。若检测到，注入纠正提示强制 LLM 重新生成并使用 function calling。仅纠正一次，防止无限循环。
  3. **Schema 层：** `_generate_schema()` 现在从 docstring 的 `:param` 注释中提取参数描述，让 LLM 更清楚如何使用工具。
- **如果仍然出现：**
  1. 确认工具 schema 的 description 字段是否足够清晰。
  2. 检查 `system.jinja` 中"执行者不是建议者"条款是否仍然存在。

---

## 9. 词库系统（Lexicon）

### ⚠️ 修改 `.dict` 后没生效
- **现象：** 新增词条后模型回复里没有体现。
- **原因：** 词库在进程启动时初始化（含 lazy manifest）；运行中修改文件不会自动热重载。
- **建议：** 重启进程或后续实现热重载机制。

### ⚠️ `@prompt` 写法错误被当普通文本
- **规范：** 必须写成 `@prompt: 说明文本`（英文冒号）。
- **注意：** 其他不符合格式的行会按普通词条规则解析或忽略。

### ⚠️ 误以为懒加载会注入全部词义
- 当前策略是“仅命中词注入 user prompt”，不是全量懒词库注入。
- 常加载词库注入在 system prompt，懒加载词库注入在 user prompt。
