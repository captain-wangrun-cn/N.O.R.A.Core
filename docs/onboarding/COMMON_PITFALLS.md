# ⚠️ 常见陷阱与注意事项

> **现役约束速查。** 每条只留「现象 → 约束/判据 → 代码位置」。
> 完整复盘（排查过程、实测数据、当时怎么发现的）见 `docs/archive/COMMON_PITFALLS_full.md`。
>
> 写作约定：新增条目请写**约束**，不要写故事。踩坑过程留给提交信息和归档。
> 引用本文件内的小节请用 `§<编号> <标题>`（如「§7 媒体回查与表情包」），
> 不要用会漂移的裸序号。

---

## 1. 环境依赖

| 现象 | 原因 | 修复 |
|---|---|---|
| `ZoneInfoNotFoundError: No time zone found with key UTC` | Windows 下 `zoneinfo` 缺时区数据 | `pip install tzdata` |
| `FileNotFoundError: config.yml not found` | 配置不入版本控制 | `cp config.example.yml config.yml` + `python cli.py --configure` |

**异步测试用 anyio，不是 pytest-asyncio。** 本仓 venv 没装 pytest-asyncio，
`tests/conftest.py` 是 `pytest_plugins = ["anyio"]` + `anyio_backend` fixture，
测试上标 `@pytest.mark.anyio`（参照 `tests/test_rtc_server.py`）。
async 测试如果写成 `@pytest.mark.asyncio` 会被静默跳过。

---

## 2. 消息压缩 / 归档 / 摘要

⚠️ **判据要挂在只增不减的量上。** 归档的判据是
`SELECT SUM(message_count) FROM summaries WHERE level=1`，不是"未归档消息数"——
后者会被压缩自身排空，压在 `compress_window` 附近永远够不到 `archive_threshold`。

⚠️ **一级摘要必须合并成单条 `role: "user"` 消息**（`_build_summary_message`）。
`level < 3` 的摘要逐条 append 且 role 全是 `system` 时，长历史实例会
400 `Request contains an invalid argument`——**只改 role、内容一字不动就恢复 200**，
这是最快的定性手法。别改回 system，也别和 `context_store` 的槽位摘要"统一"：
那边 slot 1-10 **数量有界**，这里是**无界历史**。

⚠️ **归档是滚动合并，删旧行必须按 id。** `WHERE platform=? AND chat_id=? AND level=3`
会把同一事务里刚插入的新归档一起删掉，表现为"日志说归档成功、库里一条不剩"。

⚠️ **压缩/归档要有按 `(platform, chat_id)` 的在途互斥**（`_acquire_compress_gate()` /
`_release_compress_gate()`）。`_launch_background` 是裸 `create_task`，突发时段几十个
worker 会读到同一批最早的未归档消息各写一条摘要。
闸门放在 `_perform_compression()` / `_create_archive_summary()` 这一层，因为
**重试 worker 会绕过 `_check_and_compress`**。要点：

- 检查与置位之间**不能有 `await`**，否则单事件循环里也不原子。
- 拿不到闸门**直接 return**，不排队——这些任务本就是"下次消息会再触发"的幂等检查。
- return 时**不要**调 `_mark_retry_success`，重试队列那条要留在 `pending`。
- 测试要卡"LLM 调用次数 == 1"，只看结果的话拆掉闸门也照样过。

⚠️ **"已归档"按实际选中的 id 列表标记**，不要 `UPDATE ... WHERE id BETWEEN start AND end`。
那个写法依赖"id 顺序 == 时间顺序"这条没写在任何地方的前提，平台乱序投递或带历史
时间戳回填时会静默丢记忆。区间也改用选中集合的 `min/max id`。

⚠️ **摘要注入逻辑有两份**：`get_compressed_context_messages()` 和 `get_context_messages()`。
改这类"摘要注入"时两处都要看，漏一处等于没修。

### 摘要产物质量守卫

⚠️ **判据只能用 `client.last_error`，不能匹配返回文本前缀。**
provider 的失败约定是**返回一段错误文本而不是抛异常**（历史设计：好让对话链路能把
原因说给用户），所以各 provider 自己的 `_fail()` 调用点返回的是
「抱歉，处理您的请求时遇到了问题：…」这类**没有前缀**的文本。
**不要去改那些字符串**——它们同时是直接发给用户的聊天回复。

统一判据在 `memory/summary_quality.py`（`summarize_error_reason` / `ensure_usable_summary`），
三个写入点都在**第一条写语句之前**调用，不可用时 `raise`——`except` 里已有
`rollback()` + `_enqueue_retry()`，抛出等于免费接上重试队列。

⚠️ **凡是把 `chat()` 结果当"产物"而不是"给用户看的话"的调用方，都要检查这个信号**
（摘要、draw_desc、参考图挑选、各类判定轮）。截断（`finish_reason=length`）不算失败。

### 重试与分块降级

⚠️ **重试对"输入侧拦截"基本无效**，那是两种不同的拦截：

| 拦截类型 | 层 | 同输入重试 |
|---|---|---|
| `content_filter` | 输出侧 | ✅ 有效 |
| `PROHIBITED_CONTENT` / `prompt_blocked`(400) | 输入侧（请求没进模型） | ❌ 无效 |
| `empty_choices` | 输入侧 | ❌ 无效 |

两者经 `check_finish_reason_and_log` 后都表现为 `last_error="blocked:<reason>"`，
**运行时分辨不出来**。策略是「原样重试 2~3 次捞输出侧随机失败，再退到切小」，
不要调大 `retry_max_attempts`。

⚠️ **判据是聚合密度，不是某条消息有毒。** "找出并删掉敏感消息"这条路是死的，
只有**切小 / 截断 / 中性化**有用。

⚠️ **分块降级必须"有缺口就不写"。** `chunked_summary()` 在任何区间救不回来时返回 `None`，
调用方必须整体放弃——产出"看起来正常但有缺口"的摘要比彻底失败更危险。
分块时**来源标记要随每块带走**（归档的 `【此前归档】/【本轮新增】`）。

⚠️ **每块必须先精确重试几次再劈半。** 输出侧 `content_filter` 是**随机**的，
不带重试会把"某块随机失败"误判成"过不去"，一路劈到单条再判定无解 → 整条摘要放弃。
调用侧 `MessageHistory._chunked_fallback` 内部走 `summary_retry.call_with_retry`，别去掉。

### 压缩槽位（context_compression.db）

⚠️ **失败时保留旧槽位，绝不写降级内容。** `_persist_segments` 按 slot 覆盖写，
调用方 `continue` 掉一个槽等于**把它删了**，失败路径必须显式 `_carry_over` 带回旧行。

⚠️ **判据不能是字符长度，必须是结构。** 失败说明、截断原文、模型入戏写的散文
都长过任何合理的字数阈值。只有结构判据拦得住：六字段摘要必须含 **`【时间线】`**
（`has_timeline`）。分块路径开 `enforce_timeline=True`。

⚠️ **降级阶梯的顺序不能反：整段 > 首尾锚点 > 分块。** 大多数段整段送是过得去的，
一上来就送锚点等于用"丢一半细节"去换"本来就不需要换的东西"。锚点是兜底不是默认。

⚠️ **`source_key` 必须带段内 `max(timestamp)`。** 它就是槽位的缓存键；少了它，
活跃段只有 `active:<id列表>`，注入新消息时若改的是别的字段，键不变，槽位会一直吃旧摘要。
格式：`session:<sid>:<count>:<stamp>` / `active:<ids>` + 段内最大时间戳。
排序也用这个 `stamp`——只按 `ended_at` 排的话活跃段会被排到最后，与"最新在前"矛盾。

⚠️ **槽位标签不落库，读时按 slot 现拼**（`_decorate(slot, segment_type, content)`）。
拼进 `content` 存库会在缓存回填时层层嵌套。存量库要先 `_unwrap_label()` 洗一遍。

⚠️ **分块结果的拼装方式取决于用途**：

| 用途 | merge_mode | 理由 |
|---|---|---|
| 压缩槽位（**同一个**槽位切块） | `MERGE_MODE_SECTIONS` | 各块都独立输出六字段，直接拼会得到三四份重复段落 |
| 归档（**不同**时间段各一块） | `MERGE_MODE_CONCAT` | 本来就是要拼接，套结构化合并会白丢块内标题 |

结构化合并**不能按人称/口吻聚合**——那会把模型入戏写的散文正好归进用户侧聚合里。
按「行首是时间线条目 / 行首是字段名 / 其余」分类才对，并丢掉字段外的入戏散文。

⚠️ **摘要的人称写死在注入头部**的对照表里（「我」= Nora、「你」= 主人），
不依赖 role 怎么被网关映射——容器角色（`user`）和正文人称本来就是打架的。

⚠️ **未解决的前提**：`_build_archive_message` 会把同一 memory_scope 下**所有**
`(platform, chat_id)` 分区的归档拼成一条，而每个分区的「我／你」指的不是同一对人。
当前生产只有一个分区（`telegram/6112866979`）所以安全；
**一旦出现第二个分区，这里必须改成按分区分别标注**。

**已知未解决**：分块后模型可能"入戏"，以 Nora 的身份续写场景描写（实测出现过
231 字的入戏散文），它会接在最后一个 `【隐私边界】` 之后，守卫拦不住。
加固方向：对分块结果做结构校验，或对分块路径单独强化"你是总结助手，不是对话参与者"。

---

## 3. 消息去重与时间戳

⚠️ **本轮用户消息在模型输入里出现两次**（AI 反馈"你发了两次"）。

所有大脑的输入都是「历史上下文 + 当前用户消息」，而当前用户消息在生成前就已写进
`messages` 表，历史里必然包含它一次，**必须在拼 history 时剔除**。

- **不要按字符串相等判断。** `add_message` 会改写入库内容（时间戳前缀、`[来自 …]`、
  `[表情包: …]`），群聊提升批次还会重排格式。每新增一种内容加工，字符串比较就重新失配。
- **正确做法：按行 id 去重。** 写库时把 `add_message` 返回的自增 id 记进 context
  （`core/message_dedup.py` → `record_persisted_user_message`），下游用
  `drop_current_user_message(db_context, context, fallback)` 排除。
- **新增 `role="user"` 的 `add_message` 调用点时，必须同时记录返回的 id。**
  context 被改写成别的指示时（如轮询 continue 分支）要调 `clear_persisted_user_messages`。
- context 常被 `dict(...)` / `.copy()` 浅拷贝传递，id 列表是**整体重绑**而非原地 append。

### 时间戳：剥在"输出侧"是对的，剥在"模型输入侧"是错的

| 链路 | 该不该剥 | 用什么 |
|---|---|---|
| 模型回复发给用户前 / 去重兜底比较 / 轮询转述后脑结果 | **该剥** | `core.routing.strip_timestamp_markers` |
| 构造模型可见 history（前脑、审查、打断判定、轮询） | **不该剥** | `core.message_dedup.build_history_messages`（保留） |

- `[<时间>] <原文>` 前缀是模型判断"这话多久以前说的"的**唯一锚点**，全剥掉模型既不知道
  对话间隔也算不出时间跨度。后脑从来没剥过，所以踩坑时表现为"只有前脑对时间犯迷糊"。
- **保留它不破坏缓存**：前缀是**入库那一刻冻结**的，之后每轮读出来字节一致，落在可缓存的
  稳定前缀里，零成本。
- **"一处现在"原则**：历史前缀 = 过去（每条消息各自的发生时刻），当前时间 = 现在
  （每轮现算，**只注入当轮最后一条 user 消息**）。放在 system 里会每轮作废整个前缀。
  任何时刻整个 prompt 里只应有一处"现在"。
- **别改成"读取时现算当前时间拼进每条历史"**，那样每轮 history 都变、缓存前缀全废。
  `tests/test_current_message_dedup_e2e.py::test_front_brain_history_timestamp_is_frozen_not_per_turn` 锁这条。
- 历史上曾有三份只认分钟精度的窄正则副本，而实际格式默认带秒和星期
  （`%Y-%m-%d %H:%M:%S %A`）——**剥时间戳只用 `strip_timestamp_markers`**。

⚠️ **`[系统备注]` / `[内部备注]` 块后面必须留空行。**
`_INTERNAL_NOTE_BLOCK_PATTERN` / `_SYSTEM_NOTE_BLOCK_PATTERN` 的终止符是 `\n\s*\n`
**或字符串结尾**。块在**最末尾**（靠 `$` 终止）或块后**跟一个空行**都是对的。
危险的是第三种：**块后面还拼了别的块、且块尾没空行**——剥除会把后续内容一起吞掉。
往 user_prompt 追加块时，先确认已有块的位置与结尾空行。

---

## 4. 流式输出 / 前脑标记

### `[SPLIT]` 只在分段发送链路中生效

- `[SPLIT]` 语义分段，`[SPLIT:秒数]` 加显式延迟（如 `1.5`）。
- 要让模型输出它，靠 `system.jinja` 的"消息节奏协议"引导。
- 与 `_split_long_text()` 是两套独立机制：后者是平台长度硬限制兜底。
- **检测到 `tool_call` 会清空 `response_text_buffer`**，避免思考过程泄漏；
  但工具调用前已通过 `[SPLIT]` 发出的文本**无法撤回**。

### ⚠️ 新增前脑标记/字段：三层透传，漏一层静默失效

现象：模型明明输出了标记（日志/原始回复里能看到），但下游什么都没发生——
标记被正常剥离、功能不触发、也不报错；若模型只输出标记没配文字，用户侧表现为"回复是空的"。

字段要过**三层**才生效，且**任何一层都不报错**：

1. **routing 解析层提取**——`parse_front_brain_response()` 与 `parse_front_brain_review()`
   **两个解析器都要改**。审查轮就算不消费该标记，也必须剥离，否则会作为字面文本发给用户。
2. **前脑 return dict 透传**——`core/front_brain.py` 的最终 return dict，
   **早退分支（`needs_backend=True` 兜底返回）也要检查**。
3. **调用方消费**——`core/message_handler.py`（主对话轮）、`core/scheduler_mixin.py`
   （主动消息轮）、轮询审查轮。

配套：

- 空输入早退 dict 也加同名字段，保持键集一致。
- `sanitize_adapter_output_text()` 加兜底剥离，覆盖后脑/主动消息等其它发送路径。
- 排查手段：debug 输出（`⚡ 前脑:` 块）和 `前脑结果:` 日志行把关键字段打出来，
  一眼能看到是"模型没输出"还是"输出被丢"。
- 测试建议：仿 `tests/test_tts.py::test_controller_has_voice_gen_tasks_wired`，
  用 `inspect.getsource` 做源码级断言锁"return dict 含字段名"，不用拉起完整链路。
- 通用化：**任何"解析层产出新字段 → 上层 dict 转发"的链路**（adapter context、
  工具结果、provider 返回）都有同一个坑。

### ⚠️ 前脑标记与平台媒体协议撞名：剥离正则绝不能 IGNORECASE

| 方向 | 格式 | 消费方 |
|---|---|---|
| 进站（模型输出） | 大写 `[VOICE]...[/VOICE]` / `[DRAW:...]` | 前脑解析器 |
| 出站（adapter 消费） | 小写 `[voice: path]` / `[image: path]` | `_send_platform_message` |

`_VOICE_SHORT_PATTERN` 曾带 `re.IGNORECASE`，把出站媒体标签也当 TTS 标记在
`sanitize_adapter_output_text()` 里剥掉 → 文本变空 → `if not text: return []`
**静默返回**，调用方却照常打"已追发"。

- **新增前脑标记前先 grep 平台协议**：查 `adapters/*/constants.py` 的
  `FILE_PATTERN` / `_MEDIA_TAG_PATTERN`，确认新标记名（含大小写变体）不撞名。
- **`if not text: return []` 是静默点**。排查"发了但没收到"时，先在 sanitize 之后
  log 一次文本内容确认没被剥空。
- 测试锁：`tests/test_routing.py::test_voice_marker_case_sensitivity_vs_media_protocol`。

---

## 5. 平台适配

### Telegram

- **Token 不在全局 config.yml**，在 `adapters/telegram/config.json`。
- 单条消息上限 4096 字符，由 `_split_long_text()` 按段落/行/字符边界切割。
- 群聊只有被 @ 才响应，单纯回复机器人不触发（已触发消息仍会提取 reply 内容）。

### OneBot v11

- 配置在 `adapters/onebotv11/config.json`，支持正向 (`connection_type="websocket"`)
  和反向 (`"reverse"`) WebSocket。
- NapCat 扩展 API 默认不暴露，只有 `enable_napcat_api=true` 时后脑才看到
  `onebotv11_napcat_*` 工具。
- 群聊默认只处理 @机器人或回复机器人消息，由 `group_message_policy` 调整。
- **群管/撤回/禁言/改群名片等真实 QQ 操作必须先向主人确认**，实际成功与否取决于
  登录号权限和 OneBot 实现。

### ⚠️ 聊天记录（合并转发）里的图片不要去解析

`adapters/onebotv11/forward.py` 展开时内部媒体**只留 `[图片]` 占位**，不下载、
不进 ImageStore、不调 `get_image`。这是有意的：一条记录可能嵌套几十张图，
全量下载会拖垮入站链路并污染图库。**改动时不要顺手"补上"媒体解析。**

- 入站预算 `INBOUND_MAX_*`（20 节点 / 1500 字 / 3 层），工具路径 `FULL_MAX_*` 更宽。
  两套常量不要合并——入站要控上下文体积，工具是用户明确要全文。
- 节点结构各实现不一致（`data.content` / `messages` / `message`），
  发送者名按 `nickname → card → user_id` 回退。

### AI 引用（reply）的消息不对

三个各自独立的根因，都是"模型拿到的 ID 本身就错了"，不是模型判断力问题：

- **被回复的历史消息 ID 混进了当前入站 ID。** OneBot 把 `reply_to_message_id` 塞进
  `platform_message_ids` 且**排第一位**（供媒体反查）。`core/scene_context.py` 的
  `_own_inbound_message_ids()` 负责剔除——新增任何"往 platform_message_ids 里塞非本轮 ID"
  的路径时都要同步这里。
- **聚合轮只给裸 ID 列表。** 正文被换行拼成一整段，`101, 102, 103` 无法对应到具体哪句。
  现在 part 带 `text_preview`（`adapters/aggregator.py`），场景块逐条列出。
- **合并/折叠分支丢 ID。** 媒体轮折叠、群聊提升批次都是从"某一条" context 复制出来的，
  不显式合并就只剩那一条的 ID。`back_brain_input_context` 快照里要带
  `platform_message_ids`，`_group_batch_context` 要遍历所有 event 合并。
  **新增任何合并分支时都要检查这一项。**

> ⚠️ **未修复**：`[reply:ID]` 只校验格式不校验存在性（`adapters/message_controls.py`）。
> 模型幻觉的 ID 会直达平台，表现为静默失败或引用到无关消息。

---

## 6. 群监听

⚠️ **ONLINE 一旦点亮，能关掉它的路径极少**；只要某群挂着 ONLINE，两个 adapter 就对它
整群放行（`adapters/onebotv11/main.py` 的 `online_group` 分支、
`adapters/telegram/incoming.py` 的同类分支），表现为"没 @ 也在监听"。

历史修过的七条（**改动时不要退回去**）：

- **`GroupPresenceStore` 必须有时间过期**：启动时跑 `expire_stale_entries()`（6 小时，
  对齐 `PrivatePresenceStore`）。`normalize_single_online()` 是**保护而非清理**。
- **窗口为空时仍要评估**：`_idle_wait` 在 `not pending_window` 时直接 return 是错的——
  "被 @ → 回复 → 群安静"恰好清空窗口（定向路径会 `_take_recent_pending` 抽走待判断消息），
  最安静的群反而最不可能被关掉。空窗口走 `_demote_reason` 硬超时判定。
- **并发后缀不能让 SEMI_ONLINE 结论静默作废**：连续 `semi_online_vote_threshold` 轮
  都投 SEMI_ONLINE 时按当前窗口末尾强制降级。
- **滞留的 `reply_to_bot` 不能永久否决降级**：按 `directed_veto_seconds` 只否决近期定向事件。
- **分类器必须拿到时间信息**：注入 `listening_stats()`（已监听秒数、距最近互动秒数、
  距最近消息秒数、连续 KEEP_LISTENING 轮数 + 两个参考阈值）。
  新增分类信号时记得同步 prompt 与渲染参数。
- **看门狗必须显式启动**：`controller.start_triggers()` 调 `group_listener.start()`。
  懒启动（`receive` / `set_mode`）对"持久化为 ONLINE 但一直没新消息"的群永远不会触发。
- **`set_mode` 必须带 platform 元数据**，否则持久化记录里 `platform`/`platform_chat_id`
  是空串，任何基于记录的巡检都拿不到平台。

排障看日志：`群监听 fast=... keep_streak=N semi_votes=N`、
`群监听拒绝近期定向窗口的 SEMI_ONLINE 决策`、`群监听空窗口静默降级`、
`群监听看门狗强制降级`、`启动过期清理: N 个群 ONLINE 记录已重置`。

---

## 7. 媒体回查与表情包

### ⚠️ 回查媒体的分析轮不能用人设

分析轮若沿用 `get_system_prompt()`，`system.jinja` 第一行就是人设/SOUL，再叠上对话历史，
模型会把 `question` 当成用户在搭话，回一句寒暄而不是做客观分析。

该轮走 `brain/templates/media_analysis.jinja`（无人设的"媒体内容分析引擎"）。
**代码侧必须同时满足三条，缺一条人设都会漏回来**（`core/back_brain.py`）：

1. `turn_system_prompt` / `turn_user_prompt` 都换成模板的两个 block
   （只换 system 不够，user prompt 里还带着正常轮的对话上下文）；
2. `turn_history = []`——留着历史，模型会从上文自己学回 Nora 的语气；
3. 该轮不给工具，且流式产出**既不发用户也不进 `temp_history`**
   （`[SPLIT]` 分支里 `if tool_media_analysis_round: continue`）。少了这条，
   内部观察数据会被当成回复直接发出去。

回灌时措辞也要管：写"这是你自己亲眼看到的事实"，否则正常轮会复述成
"分析显示……/报告指出……"这种转述腔。
模板里显式禁止输出 `[IMAGE_TAGS]` / `[IMAGE_OCR]` / `[IMAGE_DESC]` / `[VIDEO_TAGS]`。

### ⚠️ 搜到一堆图时不要全量回灌

单轮上限：图片 4 个（带 `question` 时 3 个）、视频 1 个（`core/back_brain.py` 的
`MAX_TOOL_IMAGES_PER_TURN` / `..._WITH_QUESTION` / `MAX_TOOL_VIDEOS_PER_TURN`）。
被截断时 `tool_result` 会附提示引导收窄查询或用 `page` 翻页，
`media_truncated_count` 也会带进分析轮 prompt。

### ⚠️ IMAGE_TAGS 标签块必须放回复最开头（tags-first）

标签块放回复末尾时，模型聊嗨了会在标签前收尾（漏标签），或输出被
`max_output_tokens` 截断把标签腰斩。改为 tags-first 后截断只砍聊天尾巴。
**改动时不要把顺序改回去。**

- **标签提取源必须优先 `last_image_raw_output`**（视频 `last_video_raw_output`），
  其次才是 `final_response_buffer`——tags-first 后标签块落在回复开头，若处于某个
  `[SPLIT]` 分段内，分段会先剥掉标签再进 buffer，从后者提取就漏标签、误触发重试。
- **排障先看 provider 日志的「输出被长度上限截断」warning**：截断在上层只表现为
  "IMAGE_TAGS 异常"，两件事日志里不在一处。`max_output_tokens_by_alias.image`
  建议 8192（全局默认 768 很容易截断）。
- 测试锁：`tests/test_image_memory.py` 的 tags-first 断言组。

### ⚠️ 表情包不算"图片输入"

设计：走 fast-image 生成一句话 desc 带进**前脑**，真图不进主图模型
（`core/back_brain.py` 按 `is_sticker` 过滤，也不进 ImageStore / 标签 OCR / 向量图库）。

历史 bug：`image_input_detected = bool(multimodal_images)` 而 `extract_image_payloads`
把 `[sticker:]` 和 `[image:]` 一起提取，于是纯表情包消息被路由到后脑并 `return`，
**跳过整段前脑**；到了后脑那张图又被 `is_sticker` 滤掉。两头落空。
现象很好认：问"这个表情包是什么内容"回"具体画面我看不到"，
日志里是 `Turn 1 模型: coder` 而不是 `image`。

- 判据必须是**排除表情包之后**的图（`has_real_non_sticker_image`）。
  混合消息（真图 + 表情包）仍算图片输入。
- 但 `has_image_marker and not has_real_image`（"标记有但没加载到"）要继续用
  **含表情包**的 `has_real_image`，否则成功加载的表情包会被误判成加载失败，
  给模型注入 `image_load_failed`。
- `context["multimodal_images"]` **仍要带着表情包**——`_describe_stickers` 全靠它。
- desc 必须在**前脑路径上就地生成**（后脑那套注入的条件是 `not message_saved`，
  而前脑路径马上把 `_message_saved` 置 True，注入永远不会发生）。
  生成后要同时回填 `context["text"]`——只改 `message_content` 的话入库有 desc、
  **当轮前脑却看不到**。
- `_describe_stickers` 走 `brain/templates/sticker_analysis.jinja`，要求「画面 + 情绪」。
  **该轮故意不走 `_chat_stream_wrapper`**——那个包装会注入词库全局说明和系统环境信息
  （时间/ChatID/OS/Python 版本），对"看图说一句话"全是噪音。
- OneBot 的 `_enrich_reply_text` 判断"媒体"时**必须排除表情包**（用
  `message.py` 的 `is_media_segment()`）。置位 `reply_to_contains_media` 会附上
  `HISTORICAL_REPLY_MEDIA_NOTE`「上方媒体就是被回复消息里的真实内容」，
  而模型其实拿不到那张图——等于邀请它幻觉。
- **判断表情包段只用 `is_sticker_segment()`**，别再手写那三个字段的 fallback。
  QQ 的表情包段有两种形态：`mface`/`marketface`，以及
  `subType`/`sub_type`/`type` 任一为 `1` 的 `image`。三个键名在不同实现
  （NapCat / go-cqhttp / Lagrange）里不一样，必须全试。
- `tests/test_sticker_routing.py` 用源码级断言锁住上面每一条。

### 引用表情包不触发 fast-image 识别

`[sticker: path]` 是驱动整条 fast-image 链路的**唯一载体**。引用路径上这个标记原本
无从还原，因为两层信息都被有意丢弃：表情包**故意不进 ImageStore**（没有 `image_id`
可反查），入库正文被收敛成裸 `[表情包]`（路径没落库）。

修复只能是**重下文件**：`adapters/telegram/reply.py` 的
`_redownload_replied_sticker_as_input()`。**这个分支必须排在历史查找之前**——
历史查找会先命中并返回裸 `[表情包]`，把 sticker 分支彻底挡掉
（`tests/test_telegram_reply_sticker.py` 锁这个顺序）。

- 重建的格式必须和 `_handle_sticker`（`adapters/telegram/incoming.py`）**逐字一致**：
  emoji/贴纸包那行给模型看语义，路径那行才是载荷。少了后者标记等于没重建。
- 命名沿用 `sticker_<file_id>.<ext>`，同 file_id 不重复下载。
- OneBot v11 走的是**另一条路但不用修**：`_enrich_reply_text` 调的是和直发消息同一个
  `segments_to_nora_text`。`message.py:176` 那个 `[表情包:{file}]` 只是给人看的摘要函数，
  不在入站链路上——别被它误导。

### 图片和紧随其后的文本各回一次

- **不是聚合器的问题。** 图片以 `[image: path]` 文本形式过聚合器，3 秒窗口内到达的
  图+文本来就能合并。真正的窗口在聚合之后：`core/message_handler.py` 的合并逻辑只在
  `backend_busy_or_queued` 为真时生效。
- 现在由 `_media_turn_fold_target()` 判定能否折进正在跑的媒体轮。
  **五道闸门不折，改动时不要放宽**：轮询模式、已对用户发过可见回复、该轮已执行过工具、
  超出 `MEDIA_TURN_TEXT_FOLD_WINDOW`（90 秒）、群聊里发送者不是同一个人。
- 合并分支要同时合 `multimodal_images` **和** `multimodal_videos`——历史上只合了图片。
- `[Image #N]` 是模型自己的措辞，不是代码插入的标记，别去搜代码。

### 发完图/表情包之后 AI 再也不吭声（followup 静默死亡）

根因不是 followup 没被 arm，是它醒来时读到的历史是残缺的。三个独立坑：

- **媒体消息入库正文为空。** `[image:...]` / `[sticker:...]` / `[video:...]` 被
  `extract_*_payloads` 剥光后，只发图不配文字的消息入库就只剩时间戳前缀。
  现在由 `media_placeholder_text()`（`core/message_handler.py`）补 `[图片]` /
  `[表情包]` / `[视频]` 占位，接在 `group_message_content()` 里，覆盖所有入库路径。
- **IMAGE_TAGS 重试失败会清空 `final_response_buffer`**，那条硬编码的"图片输出异常"
  提示发给了用户**但没入库**——用户看见了，模型的历史里却是空白。
  失败分支要把提示文本写回 buffer，让它照常落库。
- **折叠路径注入不到表情包描述。** `core/back_brain.py` 的注入条件是 `not message_saved`，
  而媒体折叠/忙碌合并分支写库时就把 `_message_saved` 置 True 了。
  那条分支要自己调 `_describe_stickers()` 就地拼描述。

排障顺序：先查库里那一轮的 user/assistant 两条消息内容是不是空的，再看日志有没有
`检测到对话已自然告别，followup_loop 静默退出`（那是另一条路径——`[TASK_DONE]` 在
`_GOODBYE_PATTERNS` 里，前脑轮会被 `core/routing.py` 剥掉，媒体快路径绕过前脑所以不会剥）。

---

## 8. 形象生图

### 标记与触发点

- **新增前脑标记必须改两个解析器**（见 §4 流式输出 / 前脑标记）。
- **生图触发点的位置不能挪。** 在 `core/message_handler.py` 里它必须夹在
  send_front_reply 块**之后**（要用那里算出的 `send_target`，否则图追不到文字实际去的目标）、
  `_apply_presence_markers()` **之前**（`presence_ended` 会提前 `return`）。
  投递目标被拒绝时（`send_target` 有值但 `runtime_key` 为空）也不要生图。
- **生图是旁路任务，不在 `generation_tasks` 里。** `/stop` 与 `shutdown()` 需要单独调
  `cancel_appearance_image_task()`，否则停完还会冒出一张图。
- **生图失败不向用户追发任何文本**（用户的约定）。排查看日志的 `生图失败` 行，
  `draw_prompt` 落在 Mongo `appearance_images` 里。

### 一致性

- **没有参考图，每次生成的脸都不一样。** 参考图是形象一致性唯一的锚。链路本身不会报错，
  只会在日志里 warning，然后画出一个陌生人。
- **`appearance/refs/` 是 `_is_path_safe` 里唯一的目录级规则**，用 `abs_path` 归一化后的
  `/appearance/refs/` 子串匹配（不是文件名黑名单）。**不要把该目录改名或挪层级**，
  否则拦截会静默失效。`manifest.json` 也在拦截范围内。
  连带影响：`refs/` 下的图不能作为 `generate_appearance_reference` 的 `source_images`
  传入（会被同一条规则拒）——已有参考图工具本来就自动带，不需要手动指定。
- **来源图和已有参考图在提示词里必须分开点名。** 两者都以附图形式进 `generate_image`
  但语义相反：来源图是"照着它长"，已有参考图是"保持同一个人只改视角"。
  工具按 source → anchor 的固定顺序传图，提示词用 FIRST N / LAST N 指代——
  **改动传图顺序时必须同步改提示词措辞**。
- **三份输入的边界一破，图就画不对。** `APPEARANCE.md`（长什么样）/ `STYLE.md`（画成什么样）/
  `[DRAW:要求]`（画什么）各有唯一权威。`front_brain.jinja` 禁止在要求里写外貌和画风，
  `draw_desc.jinja` 顶部有同样的三列表。**改这三处提示词时要同步。**
- **`STYLE.md` 在两条生图路径上取的层次不同，别统一。** `[DRAW:]` 日常拍照要整份；
  `generate_appearance_reference` **只取渲染风格那一层**，明确 IGNORE 拍摄方式、布光和比例。
  参考图必须是中性形象锚，让参考图也吃"手机随手拍"会污染之后所有生图。
- **`STYLE.md` 不注入 system prompt**，只在两条生图链路里读（`read_style_text()`）。
  顺手加进 `load_identity_context()` 等于又给前脑一份可以往要求里抄的画风描述。

### 配置

- **`draw` 和 `draw_desc` 必须成对配置。** 只配一个等于没配：
  `draw_models_configured()` 要求两者都有，否则前脑提示不注入 `[DRAW:]` 说明、
  参考图工具也不注册。
- **`draw_api` 是协议，`draw_prompt_style` 是提示词写法，两件独立的事。**

  | 配置项 | 作用 | 选错的后果 |
  |---|---|---|
  | `draw_api` | `/v1/images/*` 还是 `/v1/chat/completions`+modalities | **404 或拿不到图**；只对 openai 类型 provider 有意义 |
  | `draw_prompt_style` | 自然语言句子 / 逗号分隔标签 | **不报错**，照样出图，但标签串喂 nano-banana 丢空间关系、长句喂 SD 被 CLIP 截断 |

  同一个 openai 端点后面既可能挂 nano-banana 也可能挂 SD，所以 CLI 里 `draw_api`
  只在 provider 是 openai 时问，`draw_prompt_style` 配了 `draw` 就问。
- **写法分支有两条路，改一条不够。** `[DRAW:]` 走 `draw_desc.jinja`
  （`prompt_style` 变量 **system 和 user 两个 block 都要传**——
  `render_template` 每个 block 独立渲染，不共享 context）；
  参考图工具不过 `draw_desc`，提示词在 `brain/tools.py` 里硬拼，自己判
  `get_draw_prompt_style()`。漏掉后者的话，标签系模型会把 "Generate a character
  reference image…" 这类整句指令当画面内容画进图里。

### images.edit 端点契约（实测，2026-08，wrapi / newapi 系）

⚠️ **`draw_edit_encoding` 选错是 415 不是静默降级。** 官方 SDK 的 `images.edit` 走
**multipart/form-data**，而不少中转站把 `/v1/images/edits` 实现成只收 JSON。
设 `json` 时走 `_images_edit_json()` 手写请求体（不经 SDK），415 会自动退回 multipart 保底。
**只影响带参考图的图生图**——纯文生图走 `images.generate`（本来就是 JSON），
所以现象是"有时能出图有时 415"。

⚠️ **JSON 直传的字段形态是实测出来的，不是猜的，改之前先看这张表**：

| 请求写法 | 结果 |
|---|---|
| `image: {"url": "data:image/png;base64,..."}` | ✅ 200 |
| `image[0]: "data:..."` | 400 `image 不能为空`（不认下标写法） |
| `image: "data:..."` | 422 `expected struct ImageUrl`（要对象不要裸串） |
| `image: [{"url": ...}]` | 422 `invalid type: map`（不吃数组） |

所以**参考图只能传一张**——多张形象锚传不进去，这是端点限制不是代码取舍，
多余的会被丢弃并打 warning。

⚠️ **JSON 直传必须显式要 `response_format: "b64_json"`。** 不带这个参数时端点回
`{"url", "mime_type"}`，而图托管在模型厂商自己的 CDN（`grok-imagine` 回 `imgen.x.ai`），
那些域名在国内机器上直连不通，表现为生图"成功"了却卡在下载并最终 `ConnectTimeout`。
端点不认这个参数会照旧回 url，url 分支仍保留作兜底。

⚠️ **生图的 mime 一律按魔数认，不要硬编码 `image/png`。** `grok-imagine` 回的是 **JPEG**，
写死 png 会落成"扩展名 .png、内容是 JPEG"。url 分支同理——CDN 链接常带查询参数或
没有扩展名。统一走 `_guess_mime()`。

⚠️ **httpx 和 aiohttp 的响应属性名不一样，混写会 AttributeError。** httpx 是
`.status_code`，aiohttp 是 `.status`。两个库做主备时必须归一化
（现在统一走 `_post_json_raw()` 返回 `(status, bytes)`），否则备用分支一旦被触发就崩，
而这条路平时跑不到、测试也容易漏掉。

---

## 9. 调度与投递

### 主动消息发给了最后一个私聊的陌生人

`_resolve_delivery_target()` 里 `default_chat_id` 是**兜底 fallback**：先问
`resolve_delivery_runtime_key()`，它返回 `load_last_active_runtime_key() or fallback`。
而 `record_active_scene()` 过去对**任何**私聊都会刷新 `last_active_runtime_key`，
于是陌生人私聊一次就抢走了投递端。

- 现在 `update_last_active_target()` / `record_active_scene()` 都过
  `_may_own_delivery_endpoint()` 闸门，非主人的私聊只记活跃场景、不改投递键。
- **闸门必须能降级。** `is_owner` 未注入 resolver 时恒为 `False`
  （`core/conversation_identity.py`），直接 `if not identity.is_owner` 会把全部流量挡死。
  判定要先查 `owner_resolution_available()`，解析器缺失时退回旧行为。
  **改这类"仅主人"闸门时都要照做**，否则功能会静默锁死而不是报错。
- ⚠️ **未改的隐患**：`core/owner_registry.py` 里**每平台第一个私聊者自动绑为主人**。
  陌生人比你先私聊某个新平台，他就进了 `owner_bindings.json`。
  要杜绝可在 `config.yml → owner.identities` 预声明。

### 日志时间和调度器时间对不上

同一件事日志行写 `02:37:00`，APScheduler 写 `scheduled at 14:37:00+08:00`，
容易误判成"任务提前 12 小时触发"。原因：`%(asctime)s` 默认用系统本地时区，
调度器用 `memory.message_history.timezone`。现在 `brain/logging_config.py` 的
`_DisplayTimezoneMixin` 让两个 Formatter 都走配置时区。

- `cron[minute='']` **不是 bug**：注册的是 `CronTrigger(minute="*")`，
  实测渲染为 `cron[minute='*']`，空值是终端复制产物。
- `apscheduler.executors` / `apscheduler.scheduler` 已降到 WARNING——群监听 tick 是
  每分钟一次的 cron job，每次触发打两条 INFO 会把日志刷成流水账。

### 前脑审查 continue 后，上一轮用过的工具第一次调用就被提示"3 次调用"

`sessions[chat_id]` 是 controller 级别的**长期** per-chat 字典，而 `last_loop_key` /
`tool_loop_call_count` / `file_edit_counts` 记录的是「本次后脑连续调用」。
`core/polling.py` 的 continue 分支只 `context.copy()`，session 原样带进下一轮。

- 修复：`_reset_tool_loop_tracking()` 在 `_generate_response` 入口调用。
- 注意 `_reset_repeated_tool_call()` **不清** `last_loop_key`，别只调它。
- 新增任何"跨本次生成才有意义"的 session 计数器时，一并加进这个 reset。

---

## 10. 推理强度（effort）

`llm.effort` / `llm.effort_by_alias` 存的是**统一档位字符串**（`config.EFFORT_LEVELS`），
各家 API 字段完全不通用，由各 provider 自己翻译：

| provider | 字段 | 取值 |
|---|---|---|
| openai（Chat Completions） | `reasoning_effort` | 档位字符串，**平铺** |
| openai（Responses） | `reasoning.effort` | 同上，但**嵌套** |
| openrouter | `reasoning.effort` | 全档位照收 |
| anthropic 4.6+ / sonnet-5 / opus-5 | `output_config.effort` | 档位字符串，**顶层兄弟字段** |
| anthropic 4.5 及更早 | `thinking.budget_tokens` | 整数预算，**必须小于** `max_tokens` |
| gemini 3.x | `thinking_config.thinking_level` | `MINIMAL`/`LOW`/`MEDIUM`/`HIGH` 枚举 |
| gemini 2.5 及更早 | `thinking_config.thinking_budget` | 整数预算（`-1`=dynamic） |
| gemini（REST 视频路径） | `thinkingConfig.thinkingLevel/Budget` | 同上，但 **camelCase** |

⚠️ **档位是按「模型」支持的，不是按「家」。** 同一家不同代次认的字段和值都不一样，
猜错的后果通常是 400 而不是降级。三条已经踩过的：

- **Anthropic 2026 架构变了，effort 不在 `thinking` 里。** 现在是 `thinking` 管模式
  （`enabled`/`adaptive`/`disabled`）、`output_config.effort` 管强度。
  **Opus 4.7+ 收到旧的 `{"type":"enabled","budget_tokens":N}` 直接 400。**
  `adaptive` 是模式名不是档位名，别当 effort 值传。
- **Gemini 3.x 必须 `thinkingLevel`，2.5 必须 `thinkingBudget`**，两个方向传错都报错。
  3.x 关不掉思考，`none` 只降到 `MINIMAL`；Gemini 只有 4 档，`xhigh`/`max` 一起降到 `HIGH`。
- **Responses API 要嵌套 `reasoning={"effort":...}`**，平铺会
  `400 unsupported_parameter`。`_filter_supported_kwargs` 救不了——`reasoning`
  本身是合法顶层参数名，它不知道里面该放什么。

⚠️ **代次判断靠模型名子串，一定会有猜错的时候，所以三家都必须有"摘掉字段重试一次"的兜底**
（`_is_effort_rejection` / `_strip_effort_fields`）。**别把重试分支删掉**——
否则一个可选调优字段会让整个别名不可用。
判断代次时**用反向匹配**（"是不是老型号"）而不是枚举新型号：
新模型只会往上出，枚举必然过期，过期就是新模型直接报错。

⚠️ **流式路径的重试只能发生在第一个 yield 之前**（`emitted_any` 闸门）。
400 是建流那一刻抛的，正常不冲突；流中途断的话重试会把已发给用户的文本再发一遍。

其余"配了但静默无效"的坑：

- **Gemini 有两套命名，不能合并。** SDK 的 `generation_config` 走 snake_case，
  `_video_stream_via_rest` 拼 REST body 必须 camelCase。写错的一侧**不报错**，
  只会被 API 忽略。
- **`none` 是合法档位，不是"未配置"。** 判据必须是 `is None`；写
  `if not self.effort` 会把 `none` 档一起吞掉。配置层同理：
  `get_llm_effort()` 返回 `None` = 不传字段，返回 `"none"` = 传关闭。
- **`auto` 是本项目自己的第 8 档**，不是任何一家的合法字符串值。
  `EFFORT_BUDGET_TOKENS["auto"] = -1` 是 Gemini 的 dynamic 哨兵，**不是"负预算"**——
  不吃这个哨兵的 API（anthropic 旧模型）必须自己判负数。
- **`ultracode` 不是档位**，那是 Claude Code 的编排关键词。有测试钉死它不在
  `EFFORT_LEVELS` 里。
- **Anthropic 的 `budget_tokens` 必须小于 `max_tokens`**，`_apply_budget_effort`
  会按 `max_tokens` 夹一下并留 512 token 给正文；`max_tokens` 太小时宁可不开思考。
- **关不掉思考的模型**：`fable-5` / `mythos-5` 连 `{"type":"disabled"}` 都 400；
  Opus 5 的 `disabled` 只在 effort ≤ high 时合法。
- **GPT-5.4+ 在 Chat Completions 里带 function tools 就不能传 `reasoning_effort`**
  （除了 `none`），必须走 Responses——后脑工具循环正是这个组合。
- **改 effort 会失效 Anthropic 的 prompt cache**（配置被渲染进 prompt）。
- **改动在下次创建该模型客户端时才生效**——effort 在 `__init__` 里读进实例字段，
  `/effort` 写完 config.yml 后已存在的 client 仍用旧值。
- 新增模型别名时改 `adapters/telegram/commands.py` 的 `MODEL_ALIASES` 一处即可，
  `/model` 和 `/effort` 共用它。
- Telegram 的 `_EFFORT_CHOICES` **直接引用 `config.EFFORT_LEVELS`**，不要另抄一份，
  抄了就会漂移。

---

## 11. 词库系统（Lexicon）

- **修改 `.dict` 后不生效**：词库在进程启动时初始化（含 lazy manifest），
  运行中改文件不会热重载。重启进程。
- **`@prompt` 必须写成 `@prompt: 说明文本`**（英文冒号）；不符合格式的行按普通词条
  规则解析或忽略。
- **懒加载只注入命中词的词义，不是全量懒词库注入**。常加载词库 → system prompt，
  懒加载词库 → user prompt。

---

## 12. 工具调用泄漏

**现象**：AI 在回复中写出 `execute_tool('edit_file', {...})`、
`<execute_skill>...</execute_skill>` 等文本，或带 `new_code="""..."""` 的 Python
代码块，而不是真正调用工具。用户看到了原始的工具调用代码。

**原因**：模型从 system prompt 的示例和工具描述里"学到"了调用语法；
Gemini history 格式不正确时也会在后续轮次退化为文本模式。

**四层防护（已实现）**：

1. **Prompt 层**：`system.jinja` 的"工具调用方式 — 严格规范"，已清理所有可被模仿的
   调用语法示例。
2. **History 格式层**：工具调用以 `tool_call` / `tool_response` 结构化格式写入 history，
   provider 转成原生格式（Gemini `function_call`/`function_response`；OpenAI
   assistant/user messages）。
3. **泄漏拦截 + 自动执行层**：`_try_parse_leaked_tool_call()` 检测到泄漏时尝试解析并
   真正执行；解析失败则注入纠正提示让 LLM 重新生成。
4. **最终输出过滤层**：`_is_tool_call_leak()` / `_clean_tool_call_leaks()` 在
   `[SPLIT]` 实时分段和最终发送前清理。

**仍出现时排查**：① `system.jinja` 里有没有新增的调用语法示例
（**绝不要在 prompt 里写 `tool_name("arg1", "arg2")` 形式的代码**）；
② `_TOOL_LEAK_PATTERNS` 是否覆盖新的泄漏模式；③ provider 是否正确传递了 `tools`；
④ `_try_parse_leaked_tool_call()` 能否解析新的泄漏格式。

**相关：LLM 只回复文本不主动调用工具（工具自觉性差）。** 三层防护：
`system.jinja` 的"你是执行者，不是建议者"条款；
`_should_nudge_tool_use()` 在第一轮无工具调用且回复含代码块时注入纠正提示（仅一次）；
`_generate_schema()` 从 docstring 的 `:param` 提取参数描述。

---

## 13. 成本跟踪与配置

- **无内置价格表**，完全依赖 `config.yml → cost_tracking.custom_prices`。
  缺价模型**首次**打 warning，之后不重复；成本记为 $0。CLI 向导中可现场输入价格。
- **不要读 `config.yml` / `.env`**（系统提示词中明确禁止，含 API Key）。
  技能使用 `config.json` + 环境变量注入机制。
