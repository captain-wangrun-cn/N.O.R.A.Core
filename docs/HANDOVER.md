## 近期关键改动（截至 2026-08-27）

### 🎤 TTS 语音合成子系统（[VOICE] 标记 + Fish Audio + 可扩展 provider）

仿 `[DRAW:]` 旁路模式加语音合成：前脑输出 `[VOICE]要念的话[/VOICE]` → tts provider
后台合成 → 落盘 `.oga` → 以 `[voice: path]` 追发（两端都显示语音条）。文字先发、语音后到，
与 `[NEED_BACKEND]` / `[DRAW:]` 正交。**允许 Nora 在合适的时候主动发语音**（情绪浓/深夜
亲密/长解释/用户反馈好；正事/用户在忙/文字党用文字），判据写在前脑提示里。

- **`tts/` 子系统（新）**：目录既是子系统也是 provider 容器（与 `adapters/` 同构）——
  内置 `fish_audio` 和用户自写 provider 平级放在 `tts/<name>/`（`provider.py` +
  `config.json` + `config.example.json`）。`registry.py` importlib 动态发现加载
  （**仓库首个进程内动态加载用户代码的先例**，无沙箱、只放可信代码）；单个 provider
  加载失败只跳过不崩启动；下划线开头目录（`_template/`）跳过。
- **provider 内置提示词**：`get_text_guidance()` 返回该 provider 的文本写法指南
  （fish_audio 是 S2 情绪标记速查：句首 `[happy]`/自然语言 `[warm and happy]`/强度
  `[very excited]`，任意位置 `[whispering]` `[laughing]` `[break]`），经
  `voice_guidance` 变量注入 front_brain.jinja——Nora 写 `[VOICE]` 内容时就知道可用标记。
- **标记语法用块格式而非短格式**：provider 情绪标记本身是方括号，`[VOICE:...]`
  会在第一个 `]` 截断切碎情绪标记。`routing.py` 的 `_VOICE_BLOCK_PATTERN` 块优先、
  `_VOICE_SHORT_PATTERN` 兜底；两个解析器 + sanitize 兜底三处都剥（照抄 DRAW 四位置）。
- **`core/speech_gen.py`（新）**：`SpeechGenMixin`，per-runtime_key 只保留最新一条、
  失败只记日志不追发文本、`/stop` 与 `shutdown()` 单独 cancel（同 image_gen_tasks）。
  语音文本剥离系统标记时**逐个点名**（SPLIT/NEED_BACKEND/reply 等）——不能用宽逻辑
  剥所有方括号，会误杀 provider 情绪标记。
- **失败约定与 LLM provider 相反**：TTS `synthesize` 失败 **raise** 而非返回错误文本
  ——产物是二进制、调用方是任务 Mixin，异常即失败。
- **Telegram FILE_PATTERN 补 `voice` 标记**（`.oga` 已映射 voice→send_voice）；
  OneBot `_MEDIA_TAG_PATTERN` 原本已含 voice→record 段，无需改。
- **Fish Audio 契约**（openapi.json 实测）：`POST /v1/tts`、Bearer 认证、**模型经
  `Model` header 不在 body**、`format: opus` 落盘 **`.oga`**（Ogg 容器；`.opus` 不在
  TG voice 扩展集会退化成 document）、`proxy` 配置键单独指代理否则 trust_env 继承全局。
- 配置：全局 `config.yml` 只有 `tts.provider: "fish_audio"`；连接参数在
  `tts/fish_audio/config.json`（`tts/*/config.json` 已 gitignore）。CLI 加 StepTTS +
  单项修改菜单。文档：`tts/TTS_GUIDE.md`（编写指南，仿 ADAPTER_GUIDE）+
  `docs/architecture/tts.md`。
- 测试：`tests/test_tts.py`（20：registry 发现/坏文件跳过/配置层叠/fish payload 与错误/
  Mixin 生成-取消-守卫/接线断言）、`test_routing.py` +7（VOICE 镜像 DRAW 命名）、
  `test_front_brain_prompt.py` +2（注入/不注入）。全量 748 passed / 11 failed，
  失败集与基线一致（context_pricing×4、cost_tracker×2、image_memory×2[干净基线同样失败，
  顺序污染]、retry_queue×2、timezone×1）。

### 🎛️ CUSTOM 注入范围新增 `video`（后加的视频模型没被 scope 覆盖）

`/custom_scope` 只有 fast/smart/image/coder，而 `core/back_brain.py` 早已按输入类型切到真正的
`video` 模型轮（`multimodal_videos and video_llm_available`）。但 CUSTOM 注入范围原来只判
`"image" if multimodal_images else "smart"/"coder"`，视频轮被并进 smart/coder，
导致 `video` 这个 scope 根本无从单独开关。

- **`core/back_brain.py`**：模型选择后算出 `media_custom_scope`（video/image，否则 None）；
  正文轮 `media_custom_scope or "smart"`、工具轮 `media_custom_scope or "coder"`，与实际使用的
  多模态模型对齐。
- **`adapters/telegram/callbacks.py`**：按钮键盘 + 回调白名单加 `video`。
- **`core/message_handler.py`**：文本命令 `/custom_scope` 的 allowed 集合、用法提示、报错提示补上 `video`。
- **`config.example.yml`**：注释可选值与默认 scopes 列表加 `video`。
- ⚠️ 现存 config.yml 若仍是老的 `[fast,smart,image,coder]`，视频轮现在不再注入 CUSTOM，
  需在 `/custom_scope` 里手动勾上 video（空列表=全量注入则不受影响）。

### 🖼️ IMAGE_TAGS 重试加强（“图片输出异常，已自动重试失败”老是没效果）

旧重试只把 `retry_prompt` 发出去，而首轮那份完整的 `image_tags.jinja` 规范只作为首轮
user_prompt 传入、没进 temp_history——等于用**比首轮更弱**的提示重试，自然没效果；且严格的
`"," in tags` 校验遇到中文逗号必判失败、反复空转。

- **`core/back_brain.py`**：重试提示重新注入完整 `image_tags.jinja` 规范 + 针对本次失败的强调
  （每个 ID 三区块成对闭合、必须半角逗号并给示例、每图 ≥8 词、识别不出用占位补全）。
  另：原重试传的 `temperature=0.55` 其实一直被 `_chat_stream_wrapper` 的 `_filter_kwargs`
  过滤掉（provider 签名不收该 kwarg），已删除；温度改由下面新增的全局配置统一管辖。
- 标签解析归一「，」「、」为半角逗号（`_canon_tag_text`），避免合法多标签被判 `bad_comma`
  触发无谓重试；初解析与重试后解析都归一。
- 重试后一并 clear + 重解析 `IMAGE_DESC`（原来只重解析 TAGS/OCR，DESC 会残留首轮结果，
  与“整条重写”语义不符）。

### 🌡️ 新增全局 / 按别名采样温度配置（原来 temperature 根本没接上）

发现：`temperature` 在整个主链路里从没被设置过——三个 provider 的 `chat_stream` 签名都不收这个
参数，`_chat_stream_wrapper`（`core/controller.py`）的 `_filter_kwargs` 会把任何 provider 不认的
kwarg 静默丢掉。所以此前无论谁传 `temperature=` 都到不了 API，一律用各家端点默认（~1.0），
也不存在"项目全局默认温度"。

新增（完全对齐 `effort` / `max_output_tokens` 的既有形态：provider 只拿 alias，自己按 alias 读 config）：
- **`config.py`**：`get_llm_temperature(alias)`，优先级 `llm.temperature_by_alias[alias] → llm.temperature → None`；非法值按 None。
- **三个 provider**：`__init__` 读 `self.temperature`，在各自 build payload 处（紧挨 max_tokens）下发：
  - openai：`_apply_temperature()`，chat / responses 都是顶层 `temperature`；
  - gemini：`generationConfig.temperature`（REST camel + SDK snake 两条路）；
  - anthropic：`_apply_temperature()`，**思考态（thinking=enabled / output_config.effort）下不下发**——
    Anthropic 硬约束此时温度只能为 1，否则 400。
- **推理模型 400 兜底**（用户选的"自动降级"）：openai 新增 `_is_temperature_rejection()`，
  chat() 与 chat_stream() 的 strip-retry 把 `temperature` 一并纳入，被拒时摘掉重试一次。
- **`config.example.yml`**：`llm.temperature` + `llm.temperature_by_alias` 注释块（含各家取值范围与推理模型/思考态的坑）。
- 默认**不配置** = 行为不变（不发字段、用端点默认）。

## 近期关键改动（截至 2026-08-17）

### 🃏 表情包被路由到后脑，前脑没走、后脑没图（修复）

生产现象：问"这个表情包是什么内容"，Nora 回"具体画面我看不到"，
日志是 `检测到图片输入，直接启动后脑` + `Turn 1 模型: coder`（不是 `image`）。

根因在 `core/message_handler.py` 的 `image_input_detected = bool(multimodal_images)`：
`extract_image_payloads` 把 `[sticker:]` 和 `[image:]` **一起**提取（只多打个
`is_sticker=True`），于是纯表情包消息满足"有图片输入"→ 直接路由后脑并 `return`、
**跳过整段前脑**；到后脑那张图又刚好被 `is_sticker` 过滤掉。前脑没走 + 后脑没图。

按设计，表情包应该走 fast-image 出一句 desc 带进**前脑**，真图不进主图模型。

- **`core/message_handler.py`**：路由判据改为 `has_real_non_sticker_image`
  （排除表情包后的真图）；混合消息（真图 + 表情包）行为不变。
  `has_image_marker and not has_real_image` 那条**继续用含表情包的** `has_real_image`，
  否则成功加载的表情包会被误判成加载失败、注入 `image_load_failed`。
  前脑路径新增就地生成 desc（后脑那套注入条件是 `not message_saved`，而前脑马上
  就置 True，不在这里生成就永远没有），且**同时回填 `context["text"]`**——
  只改 `message_content` 的话入库有 desc、当轮前脑看不到。
- **`adapters/onebotv11/`**：新增 `message.is_sticker_segment()` /
  `message.is_media_segment()`，`media.py` / `message.py` / `forward.py` 里三份重复的
  「`subType`/`sub_type`/`type` 任一为 1」判断统一到前者。
  `_enrich_reply_text` 判断"媒体"改用 `is_media_segment()`——**表情包不算媒体**：
  置位 `reply_to_contains_media` 会把被引用消息 ID 补进 `platform_message_ids`
  （但表情包没入 ImageStore，`view_media` 查不到、补了是空），更糟的是附上
  `HISTORICAL_REPLY_MEDIA_NOTE`「上方媒体就是被回复的真实内容」，而模型拿不到那张图。
  引用商城表情本来就会下载并产出 `[sticker: path]`，和直发走同一条路，不需要特殊处理。
- **`brain/templates/sticker_analysis.jinja`（新增）** + `_describe_stickers` 改写：
  表情包描述轮改成**客观分析**，和 `media_analysis.jinja` 同源同理（输出不是给用户
  看的话，是被压成一行 `[表情包: ...]` 注入上下文的观察数据，所以要剥离人设）。
  两处改动：①要求「画面 + 情绪」而不只是情绪——原来那句内联 prompt 只问"情绪或含义"，
  用户问"这是什么内容/哪个系列的梗图"时描述里没有可用信息；②**不再走
  `_chat_stream_wrapper`**，那个包装会注入词库全局说明和系统环境信息
  （时间/ChatID/OS/Python 版本），对看图说一句话全是噪音，正是它把无关上下文
  带进了描述、让非 ONLINE 场景的描述变得不客观。
- 测试：`tests/test_sticker_routing.py`（6，源码级断言）、
  `tests/test_sticker_description.py`（15，含 env 噪音回归锁）、
  `tests/test_onebotv11_adapter.py` 新增 7 条（表情包 vs 媒体的判定与引用行为）。
- 详见 `docs/onboarding/COMMON_PITFALLS.md` §5.12。

## 更早改动（截至 2026-08-16）

### 🧩 推理强度（effort）按模型别名配置 + `/effort` 按钮

新增 `llm.effort`（全局）与 `llm.effort_by_alias`（按别名覆盖）。档位表照 OpenAI 官方
reasoning 文档的枚举取：`none / minimal / low / medium / high / xhigh / max`，外加本项目
自己的第 8 档 `auto`（= 让端点自己决定，翻译成各家的原生自动模式）。
**不配置 = 完全不传该字段**，保持端点默认行为。

> `ultracode` **不是**档位——那是 Claude Code 的编排关键词，没有任何 API 对应物，
> 有测试钉死它不在 `EFFORT_LEVELS` 里。

**档位是按「模型」支持的，不是按「家」。** 同一家不同代次认的字段和值都不一样，
猜错通常是 400 而不是降级，所以每家都按模型名分支 + 摘字段重试兜底。

- **`config.py`**：`get_llm_effort(alias)` / `get_llm_effort_budget_tokens(alias)` /
  `get_effort_thinking_level(effort)` / `set_llm_effort_by_alias(alias, effort)` /
  `normalize_effort(value)`，两级回退仿 `get_llm_max_output_tokens`。
  `EFFORT_LEVELS` + `EFFORT_BUDGET_TOKENS` + `EFFORT_THINKING_LEVELS` 是唯一映射表。
  `EFFORT_BUDGET_TOKENS["auto"] = -1` 是 Gemini 的 dynamic 哨兵，不是"负预算"。
- **各 provider 自己翻译同一个档位**（各家字段完全不通用）：
  - `brain/providers/openai.py`：`_apply_effort(payload, responses_api=)` ——
    Chat Completions 平铺 `reasoning_effort`，**Responses 嵌套 `reasoning={"effort":...}`**。
    平铺字段传给 Responses 是 `400 unsupported_parameter`，而 `_filter_supported_kwargs`
    救不了它（`reasoning` 本身是合法顶层参数名）。`_is_effort_rejection` 除了认"字段不支持"
    也认"值不支持"（老模型收到 `xhigh`/`max` 只列合法值、不提字段名），命中就去掉重试一次。
    OpenRouter 通过继承自动获得，且它全档位照收、自己往下游翻。
  - `brain/providers/anthropic.py`：**2026 架构变更**——effort 档位字符串在顶层
    `output_config.effort`，`thinking` 只管模式。4.6+/sonnet-5/opus-5 走
    `_apply_native_effort()`，4.5 及更早走 `_apply_budget_effort()`
    （`thinking={type:enabled, budget_tokens:N}`，按 `max_tokens` 夹并留 512 token 给正文）。
    **Opus 4.7+ 收到旧写法直接 400**。`auto` → `thinking={"type":"adaptive"}`；
    `minimal` → `low`（Anthropic 没有 minimal）；`fable-5`/`mythos-5` 关不掉思考，
    `none` 只降到最低档。新增 `_strip_effort_fields()` 给 chat / chat_stream 兜底重试。
  - `brain/providers/gemini.py`：`_effort_generation_config(camel_case)` 按代次分两路——
    **3.x 用 `thinking_level` 枚举**（`MINIMAL/LOW/MEDIUM/HIGH`），
    **2.5 及更早用 `thinking_budget` 整数**，两个方向传错都报错。
    3.x 关不掉思考所以 `none` → `MINIMAL`；只有 4 档所以 `xhigh`/`max` → `HIGH`。
    SDK 路径 snake_case、`_video_stream_via_rest` 的 REST body camelCase，
    **两套命名不能合并**，写错的一侧不报错、只会被 API 静默忽略。
- **`adapters/telegram/`**：`/effort` 两级 inline keyboard（选别名 → 选档位，
  含"跟随全局"清除覆盖），仿 `/model` + `/custom_scope`。
  回调前缀 `effort_pick:` / `effort_set:`。只列出**已配置模型**的别名。
  别名列表提取为 `commands.py` 的 `MODEL_ALIASES`，`/model` 与 `/effort` 共用。
  `_EFFORT_CHOICES` 直接引用 `config.EFFORT_LEVELS`，不另抄以防漂移。
- ⚠️ 代次判断靠模型名子串，一律**反向匹配**（判"是不是老型号"）而不是枚举新型号——
  新模型只会往上出，枚举必然过期，过期就是新模型直接报错。
- ⚠️ 流式路径的摘字段重试只在**第一个 yield 之前**允许（`emitted_any` 闸门），
  否则流中途断的重试会把已发给用户的文本再发一遍。
- ⚠️ effort 在 provider `__init__` 里读进实例字段，改动**在下次创建该模型客户端时生效**。
- ⚠️ GPT-5.4+ 在 Chat Completions 里带 function tools 就不能传 `reasoning_effort`
  （除 `none`），必须走 Responses——后脑工具循环正是这个组合。改 effort 还会失效
  Anthropic 的 prompt cache。
- 测试：`tests/test_llm_effort.py`、`tests/test_effort_telegram.py`。

### 🖼️ 引用表情包不触发 fast-image 识别（修复）

`[sticker: path]` 是驱动整条 fast-image 链路的唯一载体，但引用路径上它无从还原：
表情包**故意不进 ImageStore**（没有 `image_id` 可反查），入库正文又被
`media_placeholder_text` 收敛成裸 `[表情包]`（路径没落库）。
`_extract_reply_info` 从历史取回的就是那个裸占位符，于是
`extract_image_payloads` → `_describe_stickers` 整条链路空转。

- **`adapters/telegram/reply.py`**：新增 `_redownload_replied_sticker()` +
  `_redownload_replied_sticker_as_input()`，照 `_redownload_replied_photo` 的模式
  重下文件并重建标记，格式与 `_handle_sticker` 逐字一致（emoji 行 + 路径行）。
  命名沿用 `sticker_<file_id>.<ext>`，同 file_id 不重复下载。
- **sticker 分支必须排在历史查找之前**——否则历史查找先命中并返回裸 `[表情包]`，
  把 sticker 分支彻底挡掉。`tests/test_telegram_reply_sticker.py` 有测试锁这个顺序。
- 已更正：OneBot v11 的引用表情包**本来就是通的**——`_enrich_reply_text` 调的是和
  直发消息同一个 `segments_to_nora_text`，`media.py` 的 `mface`/`marketface` 分支
  产出的就是 `[sticker: path]`。之前记的"QQ 侧仍未识别"是误判：`message.py` 那个
  `[表情包:{file}]` 只是给人看的摘要函数，不在入站链路上。
- 测试：`tests/test_telegram_reply_sticker.py`（9）。

### 🎨 draw_desc 被拦截时把错误提示当成生图提示词（修复）

实测日志里 `生图开始: prompt=Error: 模型因内容安全策略拒绝生成（finish_reason=...）`。
根因在 provider 的失败约定：**失败返回一段错误文本而不是抛异常**，
`_resolve_draw_prompt` 的「没包 `[DRAW_PROMPT]` 就整段当提示词」兜底分支于是把
那句错误说明原样喂进了文生图模型。

- **`brain/interface.py`**：`BaseLLM` 新增 `last_error` 字段 + `_begin_call()` /
  `_fail()` / `is_error_result()`。`check_finish_reason_and_log` 从 classmethod 改为
  **实例方法**，返回错误文本的同时记上 `last_error` —— 调用方不必再靠匹配文本判断。
  失败文本统一带 `ERROR_RESULT_PREFIX`（`"Error: "`）作为第二道闸门。
- **`core/appearance_gen.py`**：`_resolve_draw_prompt` 在 `[DRAW_PROMPT]` 兜底**之前**
  检查 `desc_client.last_error` / `is_error_result(raw)`，命中直接 raise。
- **三个 provider 的 chat/chat_stream** 入口调 `_begin_call()`、失败分支走 `_fail()`
  或置 `last_error`（含"返回内容为空"这类无异常 finish_reason 的情况）。
- 凡是把 `chat()` 结果当**产物**而非"给用户看的话"的调用方，都应检查这个信号。
  截断（`finish_reason=length`）不算失败。
- 测试：`tests/test_draw_desc_error_guard.py`（10）。

---

## 近期关键改动（截至 2026-08-08）

### 🌐 OpenRouter 生图 provider（`draw` 模型绑 openrouter 类型时）

OpenRouter 的生图端点（POST `/api/v1/images`，body `{model, prompt}`）与
OpenAI 原生 images API、Gemini `:generateContent` 都不一样，新写了一个 provider：

- **新增 `brain/providers/openrouter.py`**：`OpenRouterProvider(OpenAIProvider)`——
  `chat` / `chat_stream` 直接继承 OpenAI 兼容实现；`generate_image` 用 aiohttp
  POST `{base_url}/images`（`Authorization: Bearer`，180s 超时），解析
  `data[0].b64_json` base64 解码，mime 按魔数推断（PNG/JPEG/GIF/BMP/WEBP，兜底 PNG）。
  参考图经 `input_references`（`type: image_url`）传入，本地参考图以 data URI
  内嵌（`data:{mime};base64,{b64}`，与图片 URL 等价），格式复用
  multimodal_images 同构结构（mime_type / bytes / base64，缺 base64 自动编码）。
- **`brain/llm.py`** 工厂加 `provider_type == "openrouter"` 分支。
- **`cli.py`** 向导：提供商菜单加「➕ 添加 OpenRouter 提供商」，base_url 默认
  `https://openrouter.ai/api/v1`；`_fetch_models_for_provider` 对 openrouter 返回
  None 走手动输入。`draw_api` 询问只对 openai 类型生效，openrouter 天然跳过。
- **`config.example.yml`**：type 注释加 `openrouter`，附 openrouter_main 样例。
- 配置写法：`providers: { openrouter_main: { type: "openrouter", api_key: "sk-or-...", base_url: "https://openrouter.ai/api/v1" } }`，
  `model_providers.draw = "openrouter_main"`。无需改 `config.py`（`get_provider_type`
  无 type 时本就回退 provider 名）。
- 参考图不可用时只影响形象一致性（日志 warning），链路本身不报错——与既有行为一致。

---

## 近期关键改动（截至 2026-08-07）

### 💬 QQ 聊天记录（合并转发）入站直接解析 + 引用消息 ID 修正

**1. 合并转发展开（`adapters/onebotv11/forward.py`，新增）**

以前收到 QQ 聊天记录只给一个 `[合并转发:id]` 占位，模型要么忽略、要么得自己想起来调工具。
现在入站就展开成结构化文本，模型直接读：

```
[聊天记录 id=xxx]
Alice: 今天天气不错
Bob: 确实[图片]
  [聊天记录]
  Carol: 嵌套记录按层缩进
  [/聊天记录]
[/聊天记录]
```

- **嵌套递归展开**，`INBOUND_MAX_DEPTH=3` 层封顶，超出写 `[嵌套聊天记录: 层级过深已省略]`。
- **记录内的图片不解析**，只留 `[图片]`/`[视频]`/`[语音]`/`[文件]` 占位——不下载、不进 ImageStore、
  不触发 `get_image`。模型只知道"那里有张图"，PROMPT.md 里明确要求不要编造内容。
- **入站按预算截断**（20 节点 / 1500 字 / 单条 300 字），截断时尾部提示可用工具取全文。
- 节点解析对实现差异做了兼容：`data.content` / `messages` / `message` 都认，发送者名按
  `nickname → card → user_id` 逐级回退；部分实现把节点内联在 forward 段里，能省一次 API 调用。
- 挂载点是 `media.py:_segment_to_nora_text` 的 `forward` 分支，同时兜住平铺的 `node` 段。
  渲染失败退回占位标记，不让一条聊天记录打断整条消息处理。

**2. 新工具 `onebotv11_get_chat_history(record_id)`（`adapters/onebotv11/tools.py`）**

按记录 id 或"包含记录的那条消息 id"取完整内容（`FULL_MAX_*`：5 层 / 200 节点 / 20000 字）。
先当 forward id 试，拿不到再走 `get_msg` 找里面的 forward 段。原来的
`onebotv11_get_forward_msg` 保留但降级为"取原始 JSON"，PROMPT.md 引导优先用新工具。

**3. 引用消息 ID 传错的三个根因（用户反馈"她引用的消息不对"）**

都不是模型的问题，是它拿到的 ID 本身就是错的或不完整的：

- **被回复的历史消息 ID 混进了"当前入站消息 ID"**（`core/scene_context.py`）。
  OneBot 会把它塞进 `platform_message_ids` 且**排在第一位**（`main.py:506`，供媒体反查用），
  模型据此 `[reply:]` 就回到了一条很旧的消息上。新增 `_own_inbound_message_ids()` 按
  `reply_to_message_id` 剔除，历史引用仍由单独一行描述。
- **聚合轮只给一串裸 ID，不说哪个对应哪句**。`101, 102, 103` 配上被换行拼成一整段的正文，
  模型只能猜。现在 part 带 `text_preview`（`adapters/aggregator.py`），场景块逐条列出
  `ID 101: 第一句话`。单条消息不展开，避免噪音。
- **合并/折叠分支丢 ID**：媒体轮折叠只留后到文本的 ID（旧那条图的 ID 从未进快照，
  已在 `core/back_brain.py:489` 的 `back_brain_input_context` 补上 `platform_message_ids`）；
  群聊提升批次 `_group_batch_context` 从 `events[-1]` 复制，只带最后一条的 ID。两处都改为按顺序合并。
- 附带修掉聚合器的不对称：`reply_target_message_id` 原本只看 `parts[0]`，而
  `reply_to_message_id` 取最新一条，两者来源逻辑不一致。现在都取最新。

> ⚠️ 仍未做：`[reply:ID]` 只校验格式（`adapters/message_controls.py:42`），
> 不校验该 ID 是否真实存在于当前会话。模型幻觉出的 ID 仍会直达平台（静默失败或引错）。
> 要根治需在发送前用 `platform_message_ids` 做交叉比对。

---

## 近期关键改动（截至 2026-08-06）

### 🖼️ 形象系统 + 生图链路（APPEARANCE.md / 参考图 / `[DRAW:]`）

给 Nora 加了「她长什么样」这件事，并让她能按这个形象生图发给用户。核心设计是
**文字在前、图在后**：`APPEARANCE.md` 是唯一形象依据 → 依据它生成参考图 → 参考图锚定日常生图。

**1. 身份文件 `appearance/APPEARANCE.md`（`brain/prompts.py`）**

- 新增四个路径常量 + `LEGACY_APPEARANCE_FILE`，纳入 `_ensure_workspace_identity_files()` 的
  首启复制 mapping，并 `makedirs` 出 `appearance/refs/`。
- `load_identity_context()` 用 `<appearance>` 包裹注入，位置在 `<soul>` 之后、`<user_profile>` 之前
  ——形象是"她自己"的一部分，紧跟 SOUL 语义最连贯。
- 仓库根 `APPEARANCE.md` 是默认模板（分基本体征/头部/常穿/固定特征），
  末尾写清维护约定：改了要告知主人、参考图只能用工具管、先写文字再生成图。
- 仓库根新增 `STYLE.md`：出图风格偏好（写实真人、手机摄像头视角、4:3 比例），与
  `APPEARANCE.md` 并列纳入 `_ensure_workspace_identity_files()` 首启复制。
  三份输入各管一件事——`APPEARANCE.md` 管长什么样，`STYLE.md` 管画成什么样，
  每次 `[DRAW:要求]` 管画什么（只写视角/场景/表情）。前脑被禁写外貌与画风进要求。
  `STYLE.md` **不注入 system prompt**——只在两条生图链路里读，前脑不需要它。
- `system.jinja` §5 加两行表格 + 一节「`appearance/`：你的形象」，说明文字与图的先后关系、
  覆盖参考图会改变形象需先征得同意。

**2. provider 文生图能力（`brain/interface.py` + 两个 provider）**

- `BaseLLM.generate_image(prompt, reference_images) -> {"bytes", "mime_type"}`，
  **非抽象**、默认 `raise NotImplementedError`——现有 provider（含 anthropic）不受影响。
- Gemini：走 REST `:generateContent` + `responseModalities: ["TEXT","IMAGE"]`
  （和 `_video_stream_via_rest` 同一个理由：SDK + 自定义 endpoint + inline_data 组合不可靠）。
  部分端点不认 `responseModalities`，400 时去掉该字段重试一次。
- OpenAI：**两种接口形态**，由 `llm.draw_api` 决定（默认 `images`；只对 openai 类型 provider
  有意义，gemini 只有 `:generateContent` 一条路，所以 CLI 只在 draw 绑 openai 时才问）：
  - `images`（默认）：无参考图 → `images.generate`；有参考图 → `images.edit`
    （file-like 的 `.name` 后缀决定端点判 mime，必须带）。多图签名在部分 SDK/兼容端点不被接受，
    失败退回单图；`dall-e-*` 需要 `response_format="b64_json"` 而 `gpt-image-1` 不认该参数，
    也做了降级重试。返回 url 而非 b64 时自行下载。
  - `chat`：`/v1/chat/completions` + `modalities: ["text","image"]`，图片以 data URI 内嵌在
    `message.content` 的 markdown 里（`![image](data:image/jpeg;base64,...)`）。**多数中转站
    把 gemini image 模型挂成 openai 协议时只实现这条**，走 images 会 404。参考图复用普通多模态
    输入的 image_url data URI。老 SDK 不认 `modalities` 关键字（TypeError）时退 `extra_body` 透传。
    取图不硬编码字段名——先看 `content`（str / parts 列表），再把整个 message `model_dump()`
    序列化后正则兜底，容忍厂商自定义字段（`multi_mod_content` 之类）与被换行折断的 base64。
    只回文本时明确报错并带上文本前 300 字，便于判断是被拒还是端点不支持。
- 新增两个模型别名 `draw`（文生图）/ `draw_desc`（文本），**成对可选**：CLI 向导、
  `/model` 菜单、`config.example.yml`、两份 locale 都加了；`draw_models_configured()`
  要求两者都有才算启用。
- **提示词写法可选 `llm.draw_prompt_style`**（`natural` 默认 / `tags`），与 `draw_api` 正交：
  `draw_api` 是协议（选错 404），`draw_prompt_style` 是写法（选错不报错、只是画不对）。
  自然语言式面向 Gemini / nano-banana、Grok、GPT-Image，强调空间关系与整句描述，并显式禁用
  `masterpiece, best quality` 这类对它们无效的画质咒语；标签式面向 SD / NovelAI，按
  主体→外观→服装→表情动作→场景→光线→构图→画质的顺序排 30-50 个短标签。
  `draw_desc.jinja` 的 **system 与 user 两个 block 都要传 `prompt_style`**（`render_template`
  逐 block 渲染、不共享 context）；参考图工具不过 `draw_desc`，在 `brain/tools.py` 里自己判。
  CLI 里 `draw_api` 只在 provider 为 openai 时问，`draw_prompt_style` 配了 `draw` 就问
  ——同一个端点后面既可能挂 nano-banana 也可能挂 SD。

**3. 后脑参考图工具 `generate_appearance_reference`（`brain/tools.py`）**

- 参数 `requirement / view / usage / replace / source_images`。依据 `APPEARANCE.md` 生成，存
  `refs/{view}.png` 并更新 `manifest.json`（file / view / usage / updated_at）。
- **生成新视角时自动带上已有参考图作锚**，否则"正面"和"侧面"会是两个不同的人。
- **`source_images` 允许传入图片**（逗号分隔本地路径，从 `view_media` 的 `File:` 行取）：
  用户发图说"你就长这样"时用。来源图与已有参考图语义不同，在提示词里**分开点名**——
  来源图排附图列表最前、写"与文字描述冲突以图为准"，已有参考图排后、写"保持同一个人只改视角"，
  否则模型分不清哪张该照抄。配额（3 张）**来源图优先**，占满就不带锚（用户意图是重定形象，
  旧锚反而拉回旧样子）。路径过 `_resolve_workspace_path` + `_is_path_safe`，
  读不到**直接报错不生图**——明确给了图却按文字凭空画，比不出图更糟。
- 同名 view 已存在时**不覆盖**，返回提示要求先征得主人同意再用 `replace=true`。
- 不经 `draw_desc`：`requirement` 已是后脑显式撰写的描述，`APPEARANCE.md` 也是结构化文本，
  再套一层提示词改写只会引入形象漂移。
- 仅在配置了 `draw` 时注册（别让模型看到调不动的工具）。
- `_is_path_safe()` 新增**唯一一条目录级规则**拦 `appearance/refs/`（此前全是文件名黑名单）：
  参考图是形象一致性的锚，被通用文件工具改掉会让形象突变，而且是二进制文件本就不该走文本工具。
  副作用：`refs/` 下的图也不能当 `source_images` 传（想复用已有参考图，工具本来就自动带）。

**4. `[DRAW:要求]` 旁路生图（`core/routing.py` + `core/appearance_gen.py`）**

- `core/routing.py` 加 `_DRAW_PATTERN`，**两个前脑解析器都改**：主解析器提取
  `draw_request` 并剥离；审查解析器只剥离不触发（否则会作为字面文本泄漏给用户）。
  `sanitize_adapter_output_text()` 再加一层兜底剥离，覆盖后脑最终文本/主动消息等所有其它路径。
- 新增 `AppearanceGenMixin`：`draw_desc` 读 APPEARANCE.md + 要求 + 当前消息段 + 当前时间 +
  SCHEDULE.md + SOUL.md（`brain/templates/draw_desc.jinja`，**无人设**、空历史，仿
  `media_analysis.jinja`），输出 `[DRAW_PROMPT]`（英文）与 `[DRAW_REFS]`（**由它自己挑**参考图）
  两个区块 → 加载参考图 → `draw` 出图 → 落 `appearance/generated/YYYY-MM-DD/` →
  入 Mongo `appearance_images` → `_send_platform_message(key, "[image: path]")` 追发。
- 触发点在 `core/message_handler.py`，**位置有两侧约束**：必须在 send_front_reply 块之后
  （要用那里算出的 `send_target`，图得追到文字实际去的目标）、`_apply_presence_markers()`
  之前（`presence_ended` 会提前 return）。投递目标被拒绝时不生图。
- 与 `[NEED_BACKEND]` 完全独立，可共存；单轮只一张；per-runtime_key 只保留最新一张
  （新请求 cancel 旧的）；**失败只记日志，不追发任何安慰或报错文本**（用户明确要求）。
- 生图是旁路任务不在 `generation_tasks` 里，`/stop` 与 `shutdown()` 单独调
  `cancel_appearance_image_task()`。
- 主动消息轮（`scheduler_mixin.py`）不触发生图，只记一行日志。

**5. 独立记录库（`memory/appearance_store.py`）**

不复用 `ImageStore`：那套 schema 围绕"用户发的媒体 + LLM 打标签"设计（`tag_status`/`tags`/
Qdrant 向量/20+ scope 字段），而这里按用户要求只记 request（前脑原文）、draw_prompt、
路径、时间四项。不写 Qdrant、不建文本索引，Mongo 不可用时整体降级为只落盘不入库。

**6. 前脑提示词（`front_brain.jinja`）**

`[DRAW:]` 说明与两个示例都用 `{% if draw_enabled %}` 包住，未配置模型时完全不注入。
说明里明确区分两件事：用户要**看照片** → `[DRAW:]`；用户要**改形象设定** → 
`[TASK_INSTRUCTION]`+`[NEED_BACKEND]` 让后脑改 md + 调参考图工具。

**测试**：`tests/test_routing.py` 加 5 个（正常解析 / 标记缺失 / 审查轮剥离 / 与 backend 共存 /
sanitizer 兜底）。`tests/test_cli_first_run_config.py` 的确认序列因新增可选模型从 3 项补到 4 项。

---

### 🔍 view_media 检索/分析链路整修 + 日志时区 + 跨轮重复调用误报

一次修六件事，主线是「回查媒体这条链路上，模型看到的东西和用户以为的不一致」。

**1. 日志时间戳走配置时区（`brain/logging_config.py`）**

同一件事在日志里有两套时间：日志行写 `02:37:00`，APScheduler 写 `scheduled at 14:37:00+08:00`。
原因是 `logging.Formatter` 的 `%(asctime)s` 默认用**系统本地时区**，而调度器用
`memory.message_history.timezone`（`Asia/Shanghai`）。读日志时会误判成"任务提前 12 小时触发"。

- 新增 `_resolve_display_timezone()`：读 `memory.message_history.timezone`，失败/留空回落系统本地。
- 新增 `_DisplayTimezoneMixin`：覆盖 `Formatter.converter`，`ColoredFormatter` / `SafeFormatter`
  都继承它；`setup_logging()` 里把解析结果赋给两个类的 `display_tz`。
- `apscheduler.executors` / `apscheduler.scheduler` 降到 WARNING：群监听 tick 是每分钟一次的
  cron job，executor 对每次触发打两条 INFO（Running job / executed successfully），
  把日志刷成流水账。真正需要关注的调度信息由 `core/scheduler.py` 自己打点。
- **`cron[minute='']` 不是 bug**：`core/scheduler.py` 注册的是 `CronTrigger(minute="*")`，
  在本仓库 `.venv`（APScheduler 3.11.2）实测渲染为 `cron[minute='*']`，空值是终端复制产物。

**2. `view_media` 的 keyword 改成搜索引擎式检索（`memory/image_store.py`）**

旧实现：keyword 只走 Qdrant 整句向量，空结果才回退 MongoDB `$text`。两条路都漏——
向量是整句语义，多词部分命中会被摊平；`$text` 不对中文分词，`"橘猫 沙发"` 这种无空格
CJK 组合基本打不中。

- 新增词法检索层（模块级）：`tokenize_keyword_query()` 切词 + CJK bigram 扩展
  （≥3 字的中文串再拆二元组），过滤中英停用词；`score_media_lexical()` 字段加权打分
  （`tags` 3.0 / `description` 2.0 / `ocr_text` 1.5，扩展词 0.4×，整句命中额外 1.5×，
  最后按主词覆盖率缩放 `0.35 + 0.65 * 覆盖率`）。
- 新增 `ImageStore.search_by_lexical()`：`$or` regex 召回 → Python 侧打分排序 → 切片。
  候选池 `max(60, min((limit+offset)*6, 400))`，既留排序空间又不被极宽泛查询拖垮。
- 新增 `ImageStore._search_keyword_fused()`：词法 + 语义两路 RRF 融合（`k=60`，
  词法权重 1.0 / 语义 0.9），两路皆空才回退 `$text`。
- **两路都不自己跳 offset**（各取 `limit+offset` 条，融合排序后统一切片）——
  和 `text_query + keyword` 合并路径同一个坑：各跳一次会页码错位。
- `search_images` 的两个 keyword 分支（合并路径 + keyword-only）都改走融合检索。

**3. 回查媒体的分析轮剥离人设（新增 `brain/templates/media_analysis.jinja`）**

现象：`view_media` 带 `question` 回查一张图，返回的结果是 Nora 的口吻——把 `question`
当成用户在搭话，回一句"好可爱呀～"，而不是做客观分析。

根因是分析轮复用了正常推理的上下文：`get_system_prompt()` 里 `system.jinja` 第一行就是
人设/SOUL，再叠上完整对话历史，模型自然按对话角色回应。

- 新模板两个 block。`media_analysis_system` 把模型定义为"媒体内容分析引擎"，明确声明
  它**没有**人设/名字/性格，输出是系统内部数据、用户看不到，因此禁止打招呼、第一人称抒情、
  emoji、向用户提问；并专门写明"待分析的是从媒体库**回查出的历史文件**，不是用户刚发来的"
  （防止它把回查结果当新消息回应）。
  带 `question` 时按「直接结论 → 支撑证据 → 相关补充」组织，信息不足要明确写"无法确定"；
  不带 `question` 时按「整体概述 / 主体与细节 / 场景与环境 / 文字内容（原样转录）/ 异常细节」。
  准确性红线禁止推测编造、禁止参考其他媒体"补全"当前这份，并显式禁止输出
  `[IMAGE_TAGS]` / `[IMAGE_OCR]` / `[IMAGE_DESC]` / `[VIDEO_TAGS]`（那些只用于用户**新发**媒体入库）
  与 `[SPLIT]` / `[reply:]` / `[at:]`，也不许调工具。
  `media_analysis_user` 说明文件数量、输出结构与截断提示，末尾要求直接输出正文。
- `core/back_brain.py` 配套三件事，**缺一件人设都会漏回来**：
  1. 新增 `tool_media_analysis_round` 判定（`turn > 1` 且本轮媒体带 `from_tool`），
     命中时 `turn_system_prompt` / `turn_user_prompt` 整体换成上面两个 block；
  2. `turn_history = []` —— 丢掉对话历史，否则模型从上文自己学回 Nora 的语气；
  3. 该轮不给工具（折进 `video_turn_no_tools`），且流式产出**既不发用户也不进 `temp_history`**
     （`[SPLIT]` 分支里 `if tool_media_analysis_round: continue`），只由 `media_turn_raw_parts`
     收集后回灌。渲染失败时回落旧的内联"客观分析"提示。
- 回灌措辞改写：原来把结果当"分析报告"交回去，正常轮容易复述成"分析显示……"。
  现在要求"把它当作你自己亲眼看到的事实……不要说'分析显示/报告指出'之类的转述腔"。

**4. 纯文本紧随图片/视频轮时折进同一轮（`core/message_handler.py`）**

现象：先发一张图（直接走后脑 image 轮），紧接着补一句文字，结果图片和文本各回一次。

根因不在聚合器——图片是以 `[image: path]` 文本形式过聚合器的，3 秒窗口内到达的图+文
本来就能合并。问题在窗口外：合并逻辑只在 `backend_busy_or_queued` 为真时生效，
一旦 image 轮在后续文本到达前跑完，`:969` 的媒体快路径和合并分支双双跳过，
文本就成了全新一轮。（`[Image #2]` 是模型自己的措辞，不是代码插入的标记。）

- 新增 `_media_turn_fold_target()`：查 `back_brain_input_context` 快照，判断正在跑的那一轮
  是否带媒体输入、能否把这条纯文本折进去；命中则把 `backend_runtime` 指向该轮，
  走原有的 cancel + 合并 + 重启路径。
- 合并分支条件从"仅有新媒体"扩为"新媒体 **or** `media_fold_runtime`"，
  并补上**视频合并**（原来只合 `multimodal_images`，视频被丢）。
  抽出 `_merge_media_payloads(prior, incoming, id_key)` 复用去重逻辑。
- `core/back_brain.py` 的输入快照增加 `in_polling` / `user_id` / `started_at` /
  `sent_user_message` 四个字段；`_send_reply()` 里调 `_mark_back_brain_sent_user_message()`。
- **五道闸门不折**（每一道都对应一种会出错的场景）：轮询模式（前脑审查驱动自己的重启节奏）、
  已对用户发过可见回复（折 = cancel + 重写，用户会先看半截再看完整版）、
  该轮已执行过工具（工具副作用会被重跑）、超出 `MEDIA_TURN_TEXT_FOLD_WINDOW`（90 秒）、
  群聊里发送者不是同一个人。

**5. 工具回灌媒体的数量上限（`core/back_brain.py`）**

`view_media` 搜到 10+ 张图时，旧实现有几条 MediaTag 就全量回灌，单轮请求爆 token
且模型注意力被摊薄，"同时查看+提问"的准确率反而下降。

- 新增 `MAX_TOOL_IMAGES_PER_TURN = 4` / `MAX_TOOL_IMAGES_PER_TURN_WITH_QUESTION = 3`
  （带 question 时聚焦性更重要）/ `MAX_TOOL_VIDEOS_PER_TURN = 1`，
  与 `_cap_tool_media_payloads()` 一起做截断并打日志。
- 被截断时在 `tool_result` 末尾追加提示：只加载了前 N 个，建议收窄查询 / 降低 `limit` / 用 `page`
  翻页；`media_truncated_count` 随载荷带进分析轮 prompt，让模型知道自己看的是子集。
  debug 模式额外打一行 `✂️`。

**6. 前脑审查 continue 后工具被误判为重复调用（`core/back_brain.py`）**

现象：前脑审查返回 continue 时，上一轮用过的工具在新一轮**第一次**调用就被提示"3 次调用"。

- 根因：`sessions[chat_id]` 是 controller 级别的**长期** per-chat 字典，而
  `last_loop_key` / `tool_loop_call_count` / `file_edit_counts` 记录的是「本次后脑连续调用」。
  `core/polling.py` 的 continue 分支只 `context.copy()`，session 原样带进下一轮，
  于是计数从第 4、5 次起算，直接命中 hint/force 干预。
- 新增 `_reset_tool_loop_tracking()` 在 `_generate_response` 入口调用，
  一并清掉 `_reset_repeated_tool_call()` 从不清的 `last_loop_key`。

**测试**：新增 `tests/test_media_turn_text_fold.py`（9 用例，覆盖可折叠 + 五道闸门 + 群聊同发送者）。
`tests/test_image_memory.py` 的 `test_search_images_offset_forwarded_to_backends` 断言随
keyword 融合路径更新（词法/语义两路 `offset=0`、`limit`/`top_k` = `limit+offset`；
两路皆空回退 `$text` 时才原样透传 offset/limit）。
全量 `pytest tests/` → 517 passed / 11 failed，同一棵树 stash 后基线为 516 passed / 12 failed，
失败集合均为已知基线（adapter_selection 单跑通过，属测试顺序污染；
context_pricing / cost_tracker / message_history_retry_queue / timezone）。

---

## 近期关键改动（截至 2026-08-04）

### 🔌 Qdrant 支持 Cloud / 远程实例（url + api_key）

此前 Qdrant 只支持本地 `host:port`，所有连接点都写死 host/port，无法接入 Qdrant Cloud。

- **新增 `memory/qdrant_conn.py`**：共享构造器 `build_qdrant_client(mem_cfg)`。
  - `memory.qdrant.url` 存在 → `QdrantClient(url=..., api_key=...)`（Cloud/远程，HTTPS + API key）；
  - 否则回落 `host` + `port`（本地实例，行为与之前完全一致）。
- **4 处连接点全部改走共享构造器**：`memory/vector.py`、`memory/image_store.py`、
  `cli.py` 的 `clean_qdrant()` / `test_qdrant()`。新增构造器后无其他 `QdrantClient(...)` 调用残留。
- **展示适配 `describe_qdrant_target(mem_cfg)`**：与 `build_qdrant_client` 同一套判定，
  避免「显示的地址 ≠ 实际连接的地址」。修掉了 4 处只会打印 host:port 的位置：
  `show_config_summary()`、`show_status()`（原用 `vs.host/vs.port`）、`clean_qdrant()`
  高危确认（新增目标实例展示，防误删远程数据）、`VectorStore` 连接成功日志。
  Cloud 分支只显示 URL 不含 api_key（有用例断言不泄漏）。
  `VectorStore.host/.port` 已无外部引用且在 Cloud 下有误导性，一并删除。
- **`/debug_cleanup`（`adapters/telegram/cleanup.py`）与 `test_rag`** 走 `VectorStore`/`ImageStore`/
  `RAGEngine`，随上述改动自动适配，无需单独修改。
- **配置向导 `StepDatabase`**（`cli.py`）：已有 `url` 时先确认 URL 并（掩码）更新 API key，
  再问本地 host/port；保存时 url 非空则一并写入 `url`/`api_key`，否则维持 host/port。
  i18n 新增 `wizard.qdrant_url` / `wizard.qdrant_api_key`。
- **`config.example.yml`**：`memory.qdrant` 增加 `url`/`api_key` 注释示例；
  `.env.example` 删除从未被代码读取的死配置 `QDRANT_URL`。
- **网络**：Cloud endpoint 需代理。`config.py` 的 `network.proxy` 会注入
  `HTTP(S)_PROXY` 环境变量，qdrant-client 基于 httpx 自动走代理，无需额外处理。
- **注意**：Cloud 首次连接会按 `embedding.dimensions` 自动建 collection；换 embedding 模型导致
  维度变化时需删旧 collection（`cli.py clean_qdrant` 可清）再重建。
- **测试**：新增 `tests/test_qdrant_conn.py`（15 个用例）：本地/Cloud 双模式、url 优先、
  **旧 config.yml 兼容性**（qdrant 段缺失、空 dict、memory 段为 None、残留 `url: ""` / `url: null`、
  port 为字符串——全部正确回落本地分支）、以及 `describe_qdrant_target` 不泄漏 api_key。
  全量 `pytest tests/` → 514 passed，9 个失败均为已知基线（context_pricing/cost_tracker/retry_queue/timezone）。

---

### 🖥️ CLI 配置管理：总览 + 分区修改（不再强制重跑整个向导）

`python cli.py configure`（等价于菜单「🔧 配置管理」）现在先展示当前配置状态，再选择操作：

- **`show_config_summary()`**（`cli.py`）：逐分区打印总览，标记「已配置 ✅ / 缺失 ⚠️ / 可选未配置（斜体）」：
  - 工作区、网络代理、主人识别、LLM 提供商与模型角色（smart/fast/coder/image/video/security/fast-image/summary 全列，
    `video`/`security`/`fast-image` 未配置只标「可选」）、记忆与存储（消息历史/Qdrant/MongoDB/Embedding）、
    CUSTOM 注入范围、Nora 偏好、Tavily、成本跟踪、安全审查、主动消息调度、交互配置（含群聊监听开关）。
  - **API Key/Token/Secret 一律掩码**：`_mask_api_key()` / `_mask_secret()` 按键名（key/token/secret/password）递归掩码，
    保留首 4 + 若干 `*` + 尾 2 用于辨认（如 `sk-A******ET`）。
- **`show_adapter_configs_summary()`**（`cli.py`）：扫描 `adapters/*/config.json` 显示各平台私有配置状态（同样掩码敏感值），
  与全局 `config.yml` 的配置分开展示。
- 菜单提供「✏️ 修改单项配置」「🔌 查看平台适配器配置」「🔄 重新运行完整配置向导」；向导各步骤默认值均预填当前配置。
- **「✏️ 修改单项配置」**（`_edit_single_item()`）：列出工作区/网络代理/LLM 提供商/数据库与记忆/Embedding/Tavily/时区/
  模型角色/输出 token 上限/成本跟踪 10 个独立条目，选一项只运行对应 Step 编辑器并立即保存
  （复用 `_save_wizard_checkpoint()` 增量合并），**其他配置原样保留**，无需重跑整个向导。
  单项编辑前后各做一次备份/清理，异常时不残留备份文件。
- `configure` 子命令新增 `--no-wizard`：无 `config.yml` 时不自动进入向导，直接退出（纯查看模式）；
  缺失时默认仍会启动向导完成首次配置。

**配置向导兜底（每步自动保存）**：
- `run_wizard()` 启动时把现有 `config.yml` 备份为 `config.yml.wizard-backup`；
- 每成功完成一个步骤立即调用 `_save_wizard_checkpoint()` 把当前进度**增量合并**写回 `config.yml`
  （复用 `_build_final_config()`，与最终保存同一份组装逻辑，未涉及的配置分区原样保留）；
- 步骤失败（如请求失败）、Ctrl+C、异常中止时：已完成的步骤进度**保留在 config.yml 不丢失**，
  打印提示可用 `config.yml.wizard-backup` 手动回退；
- 向导全部完成并确认保存后自动清理备份文件；
- 步骤列表提取为模块级常量 `WIZARD_STEPS`，便于测试注入。

**测试**：`tests/test_cli_config_menu.py` 新增检查点保存、失败保留进度、备份清理、`_build_final_config`、单项编辑（含无配置文件提示）等 → 12 passed。
全量 `pytest tests/` → 499 passed，9 个失败均为已知基线（context_pricing/cost_tracker/retry_queue/timezone）。

---

### 🗓️ 群聊定时 ONLINE 监听 + `view_media` 翻页 + 群内主动开口的保守化

三件事一起做，主线是「让 Nora 能自己安排去群里旁听，但开口要克制」。

**1. `view_media` 支持页码**

- `brain/tools.py` 的 `view_media` 新增 `page` 参数（默认 1），换算成 `offset=(page-1)*limit`
  传给 `ImageStore.search_images`。`limit` 语义改为「每页条数」，仍是 1-50。
- `memory/image_store.py` 的四个检索方法（`search_by_time_range` / `search_by_keyword` /
  `search_by_ocr_text` / `search_by_semantic`）与 `search_images` 全部加 `offset`：
  Mongo 走 `.skip()`，Qdrant 取 `limit=top_k+offset` 后在上下文过滤之后再切片。
- **合并路径（text_query + keyword 同时给）不能让两路各自 skip**，否则去重合并后页码错位；
  改为两路都取 `limit+offset` 条，合并去重后统一 `merged[offset:offset+limit]`。
- 输出加 `[Page]` 行说明当前页与「可能还有下一页」；翻过头时返回专门的
  "No media found on page N" 提示而不是笼统的 no media found。结果序号延续全局位置。
- 指定 `image_id` 或 `local_path` 时 `page` 无效（精确查询/本地文件没有分页语义）。
- 提示词：`brain/templates/system.jinja` §3 补了翻页规则——第一页没找到就翻页而不是加大 limit，
  翻页时除 `page` 外参数必须一致。

**2. 群聊定时 ONLINE 监听（`workspace/GROUP_LISTEN.json`）**

机制与私聊主动消息对称：私聊是 `SCHEDULE.md` → 00:00 生成 `cache/daily_plan.json` → 到点发消息；
群聊是 `GROUP_LISTEN.json` 的 `groups`/`preferences` → 00:00 在同一文件里生成当日 `entries`
→ 到点把该群切 ONLINE 开始被动监听（**只是开始听，不是开始说**）。

- 新增 `core/group_listen_plan.py`：单文件存候选群池 + 偏好 + 当日计划 + 已触发标记，
  Nora 可直接用文件工具读写维护。关键函数 `read_plan` / `write_plan` / `ensure_plan_file` /
  `candidate_groups` / `normalize_entries` / `due_entry` / `mark_entry` / `expire_stale_entries` /
  `save_generated_entries`。
- **一个时间点只触发一个群**：`normalize_entries` 按 `HH:MM` 去重，`due_entry` 一次最多返回一个条目
  （多个同时到点取最晚的那个，更贴近当下）。
- **目标群已 ONLINE 则跳过**：`_activate_scheduled_group_listen` 先查 `mode_for`，已在线就
  记 `skipped_already_online` 并标记 fired，不重试——与私聊 proactive 在对方正在聊天时跳过一致。
  系统忙碌时同样跳过（`skipped_system_busy`）。
- `core/scheduler.py` 新增两个 job：`group_listen_plan_cron`（00:00 生成）与
  `group_listen_tick_cron`（每分钟检查到点）。**用 tick 轮询文件而不是预注册 DateTrigger**，
  这样 Nora 中途改文件立刻生效，不需要 reload。到点后 10 分钟宽限窗口内仍可补触发，
  超过则由 `expire_stale_entries` 标 `missed`，不会永远卡在待触发。
- `core/scheduler_mixin.py` 新增 `_generate_group_listen_plan_via_llm`（读候选池 + 作息 +
  SOUL/MEMORY，渲染 `schedule.jinja#group_listen_plan_*`）与 `_activate_scheduled_group_listen`。
  **模型返回的 platform/group_id 会再过一遍候选池白名单**，防止编造群号。
- `mark_entry` / `save_generated_entries` 都是就地重读再写回，不会覆盖 Nora 手工编辑的
  `groups` / `preferences`。
- `core/controller.py` 启动时 `ensure_plan_file()` 生成骨架并注入两个回调；
  `config.example.yml` 的 `schedule.enabled` 注释说明群聊定时监听同属该开关。

**3. 群内主动开口：允许，但保守**

- `core/group_listener.py` 的 `GroupRuntimeState` 新增 `online_trigger_kind`，
  `set_mode` 时 `reason == "scheduled_group_listen"` 记为 `scheduled`，其余为 `interaction`；
  经 `listening_stats()` 透出，注入 fast 分类器 prompt 与前脑的群监听状态块。
  **定时开启意味着没人叫过 Nora**，两处 prompt 据此再提高一档门槛。
- `brain/templates/group_listener.jinja#passive_system`：把「可以自主 REPLY」明确成
  **克制的许可而非鼓励**，给出四条必须同时成立的判据（具体 / 及时 / 缺位 / 简短），
  任一条只是「大概成立」就 KEEP_LISTENING；新增 `trigger_kind=scheduled` 时的更高门槛规则与边界示例。
- `brain/templates/front_brain.jinja` 新增「群里主动开口：可以，但要保守」小节，同四条判据，
  外加「少说一次几乎没有代价，多说一次会显得话多抢话」的取向、明确不该开口的情形、
  自主接话用 `[reply:MESSAGE_ID]` 锚定、定时监听不要发「我来了」之类的宣告。
- `brain/templates/system.jinja` §5 身份文件表加入 `GROUP_LISTEN.json`，并补一节说明结构、
  「一个时间点一个群」「已 ONLINE 则跳过」「群号不能编造，用 `onebotv11_get_group_list()` 查」
  「进去只是开始听不是开始说」。
- `adapters/onebotv11/PROMPT.md` 群聊语境补充 ONLINE 监听下的开口标准。

**测试**

- 新增 `tests/test_group_listen_plan.py`（11 项）：骨架文件字段、一个时间点一个群、宽限窗口与
  fired 标记、多条同时到点只取一个、过期日期/禁用计划跳过、`mark_entry` 不覆盖手工字段、
  `expire_stale_entries` 标 missed、候选池过滤，以及触发侧的
  已 ONLINE 跳过 / 正常激活带 `scheduled_group_listen` reason / 缺平台拒绝。
- `tests/test_image_memory.py` 新增 7 项：page→offset 换算、默认第一页、page<=0 夹到 1、
  翻过头的提示、schema 含 `page`、offset 透传各后端、合并路径先取足再切片。
- `tests/test_front_brain_prompt.py` 补断言：四条判据、`trigger_kind=scheduled`、
  「克制的许可」「少说一次几乎没有代价」「系统按计划自动开启的」。
- 全量：`.venv/Scripts/python.exe -m pytest -q` → **490 passed, 11 failed**；
  11 个失败与 stash 后的基线**完全一致**（`test_context_pricing` 4、`test_cost_tracker` 2、
  `test_message_history_retry_queue` 2、`test_timezone` 1、`test_adapter_selection` 2——
  后者仅在全量下因跨测试污染失败，单独跑改动前后均通过），与本次改动无关。

---

## 近期关键改动（截至 2026-07-29）

### 🐛 修复长期存在的"AI 说用户发了两次消息"

根因不是某个平台/场景的 bug，而是设计层面的错配：写库侧 `add_message` 会**改写**内容
（时间戳前缀、跨地点来源标签、表情包描述，群聊批次还会整体重排），而去重侧靠**字符串相等**
比较内存里的原文与历史尾部的 content。每新增一种内容加工，比较就重新失配，历史里的当前消息
没被剔除，模型看到同一句话两遍。时间线上 02-08 引入「存库→读回→又单独传」结构，02-09 加时间戳前缀，
03-14 才补上剥离但正则只认分钟精度，03-16 默认格式换成带秒与星期（`%Y-%m-%d %H:%M:%S %A`）后彻底失效至今。

**主修：按行 id 去重（内容怎么加工都不影响）**
- 新增 `core/message_dedup.py`：`record_persisted_user_message` / `persisted_user_message_ids` /
  `clear_persisted_user_messages` / `merge_persisted_user_messages` / `drop_current_user_message`。
  id 列表整体重绑而非原地 append（context 常被浅拷贝传递）。
- 全部 6 处 `role="user"` 的 `add_message` 调用点改为接住返回的行 id 并记入 context：
  `core/back_brain.py`、`core/message_handler.py`（ONLINE 群留存 / busy+媒体 / busy+文本 / 前脑优先常规路径 / 排队确认）。
- 两处比较点（`core/front_brain.py`、`core/back_brain.py`）改用 `drop_current_user_message`：
  有 id 记录时按 id 精确剔除（任意位置、任意条数），无记录时才退回旧的尾部内容比较，兼容旧调用方与既有测试。
- 生命周期打通：群聊 promoted 批次合并各 ONLINE 事件的 id；图片合并重启携带上一轮 id；
  轮询 continue 分支把 `context["text"]` 改写成"继续处理"后清除 id 记录。

**顺带：时间戳正则收敛**
- `core/routing.py` 的宽正则提升为公开 `strip_timestamp_markers`，成为唯一实现；
  `front_brain` / `back_brain` / `interrupt_handler` 里三份只认分钟精度的窄副本删除，classmethod 改为委托。
  这同时修掉了原始时间戳泄漏进模型可见历史的问题。

**顺带修的两个群聊缺陷**
- `core/message_handler.py` busy 路径的媒体分支与文本分支此前无条件写库，忽略 `_message_saved`，
  ONLINE 群消息会被写两条；现已加守卫。

**回归测试（此前完全没有覆盖，才让 bug 活了几个月）**
- `tests/test_message_dedup.py`：单元覆盖 id 记录/浅拷贝隔离/合并/清除/按 id 剔除（含群聊多条批次）/字符串兜底。
- `tests/test_current_message_dedup_e2e.py`：用真实 `MessageHistory`（临时目录、生产时间戳格式
  `%Y-%m-%d %H:%M:%S %A`）走一遍前脑，断言当前消息在 history 中出现 0 次、历史里无裸时间戳前缀。
  已验证：把去重退回旧行为（窄正则 + 纯字符串比较）后这 3 个测试全部失败。
- 全量：`.venv/Scripts/python.exe -m pytest tests/ -q` → 457 passed, 9 failed，
  失败全部是既有基线（`test_context_pricing` 4、`test_cost_tracker` 2、`test_message_history_retry_queue` 2
  缺 pytest-asyncio、`test_timezone` 1），与本次改动无关。

---

## 近期关键改动（截至 2026-07-28）

### 👥 群 fast 完整上下文、sticker 隔离与 OneBot 交互

- 群聊 passive/append fast 现在接收 SOUL、USER、MEMORY、最近三天 daily memory，以及按 `place_scope_id` 隔离的完整当前未封闭 segment；判定 prompt 只重复焦点元数据，ONLINE 单条历史不再被 promoted batch 二次写入。
- sticker 仅在配置 `fast-image` 时生成一句话描述；表情包 payload 在主图片模型和 ImageStore 管线前被过滤，不创建 stub、不参与标签/OCR/描述及补齐重试，普通图片保持原行为。
- 通用策略统一为：回应具体消息使用 reply+at，仅提醒用 at，只引用且不打扰作者用 reply。OneBot 原生 at 后强制一个 ASCII 空格。
- 新增 `[poke:USER_ID]`：OneBot/NapCat 作为独立 `send_poke` side effect 执行，支持 marker-only、分段内去重和失败不阻断文字；入站目标机器人 poke 作为普通群/私聊消息进入既有 dispatch，并与同会话消息保持顺序。
- scheduler followup 在任务创建 sink 拒绝所有群聊并清理遗留 timer；群聊后续完全由 group listener 管理。
- 聚焦回归：`.venv/Scripts/python.exe -m pytest tests/test_scope_runtime_coordination.py tests/test_group_listener.py tests/test_front_brain_prompt.py tests/test_image_memory.py tests/test_multimodal_input.py tests/test_adapter_message_controls.py tests/test_onebotv11_adapter.py -q` → 148 passed。

---

## 项目概览
- 仓库：N.O.R.A.Core（branch: main）
- 目标：多平台智能体内核，包含 LLM 适配、工具/技能体系、成本追踪、记忆/RAG。
- 当前状态：可运行，自测通过；已完成图片记忆链路（入库/检索/回查再分析）、`view_media` 工具增强、CLI 高危清理保护、**跨平台接力 + 主人/访客分离（五层身份模型，全 5 Phase 完成）**。

## 近期关键改动（截至 2026-07-27）

### 👤 群聊入站 @ 成员昵称上下文

- `AdapterEvent` 与 `MessageAggregator` 新增 `mentioned_users`：按原生提及顺序保存 `user_id` / `display_name`，聚合时按用户 ID 有序去重，并在 `native_reference_parts` 保留每段映射；既有 `mentioned_user_ids` 与 `[at:USER_ID]` 协议不变。
- Telegram 仅从带可靠 `entity.user` 的 `text_mention` 提取 profile display name（姓名优先、回退 `@username`）；普通 `@username` 不猜 ID，图片、视频、文档、贴纸、相册与 ONLINE 群直达路径均保留映射。Telegram Bot API 不提供 QQ 式群名片。
- OneBot v11 对群聊中的数字 QQ 提及 best-effort 调用 `get_group_member_info`，显示名优先 `card`、回退 `nickname`；按 `(group_id, user_id)` 做有界正/负 TTL 缓存，查询失败、自身、`qq=all` 与非数字目标均不阻断消息处理。
- 前脑与后脑复用的 scene block 会显示“昵称（平台用户 ID）”，并明确昵称仅帮助理解；原生 @ 仍必须使用可靠的 `[at:USER_ID]`，禁止从昵称或跨平台历史猜测 ID。

---

### 🎭 fast-image 可选模型：表情包/sticker 快速描述

新增可选模型别名 `fast-image`，用于快速生成表情包/sticker 的一句话描述，注入上下文供 fast 模型和 followup 理解。未配置则完全不解读表情包。

**配置层：**
- `core/controller.py`：`__init__` 与 `_reload_llm_clients` 新增 `self.fast_image_llm`（可选，异常时为 `None`，对齐 `video_llm` 模式）。
- `cli.py` `StepModels`：循环新增 `'fast-image'` + `questionary.confirm` 可选询问（"是否配置表情包快速描述模型？"）。
- `config.example.yml`：`llm.models` 与 `llm.model_providers` 新增 `fast-image` 注释示例。
- `locales/zh_CN.yml`：`roles.fast-image` 与 `roles_usage.fast-image` 翻译条目。
- `adapters/telegram/commands.py`：`/model` keyboard alias 列表新增 `"fast-image"`。
- `adapters/telegram/callbacks.py`：可选清除元组新增 `"fast-image"`（获得"🗑 清除（不使用）"按钮）。

**表情包字节解析：**
- `brain/multimodal.py`：新增 `_STICKER_TAG_PATTERN`，`extract_image_payloads` 同时匹配 `[image:...]` 和 `[sticker:...]`，sticker 标签加载的图片 payload 携带 `is_sticker=True`。新增 `get_sticker_images()` 过滤 helper。`_load_single_image()` 重构为共享加载逻辑（含去重、mime 校验、GIF→PNG、大小限制）。
- **OB11**（`adapters/onebotv11/media.py`）：`_segment_to_nora_text` 对 `image` 段检查 `subType`/`sub_type`==1（表情包）输出 `[sticker:...]`；`mface`/`marketface` 段一并归为 sticker。
- **OB11**（`adapters/onebotv11/message.py`）：`segment_to_onebot_text` 同步对 image 段 type=1 区分为 `[表情包:...]`。
- **TG**（`adapters/telegram/incoming.py`）：`_handle_sticker` 输出改为 `[sticker: emoji from set]\n[sticker: <path>]`（带可解析路径的标签），视频贴纸（.webm）由 multimodal 按扩展名自动跳过字节加载。

**描述生成：**
- `core/back_brain.py`：`_describe_images` 重命名为 `_describe_stickers`，模型改为 `self.fast_image_llm`，仅描述 `is_sticker=True` 的图片，未配置时返回空字符串（零开销）。system prompt 改为"描述表情包表达的情绪或含义"。调用点注入 `[表情包: {desc}]` 标签（区别于原 `[图片内容:]`）。
- `core/message_handler.py`：`_persist_online_group_message`（ONLINE 群静默留存路径）新增表情包描述注入——若 `fast_image_llm` 已配置且文本含 `[sticker:]`，解析字节并调用 `self._chat_stream_wrapper(fast_image_llm, ...)` 生成描述，`[表情包: {desc}]` 拼入持久化 content。

**文档/提示词：**
- `adapters/onebotv11/PROMPT.md`：补充 `[sticker:]` 标记说明。
- `adapters/telegram/PROMPT.md`：补充 `[sticker:]` 标记说明。
- `docs/onboarding/QUICK_REFERENCE.md`：模型别名表新增 `fast-image`（可选）。

---

## 近期关键改动（截至 2026-07-27）

### 👥 群聊监听改为全局单活跃群

- 群状态仍以 `platform:chat_id` 精确识别，但全局最多一个群可为 ONLINE；新群进入 ONLINE 时自动接管并将旧活跃群切回 SEMI_ONLINE，同时清理旧群 pending、idle/passive 与生成运行态。
- 未登记群和非法状态固定按 SEMI_ONLINE 处理；废弃 `interaction.group_listener.default_mode`，旧的 `default_mode: online` 会被忽略并告警。
- 启动时若 `group_presence_state.json` 遗留多个 ONLINE 记录，只保留 `updated_at` 最新群，其余原子降级。

---

## 近期关键改动（截至 2026-07-26）

### 👥 群聊独立 ONLINE / SEMI_ONLINE 监听

- 新增 `core/group_listener.py` 与 `core/group_presence_store.py`：群模式按 `platform:chat_id` 隔离并持久化，但 `MessageHistory`、RAG、压缩和摘要继续使用共享 `memory_scope_id`。
- ONLINE 群内每个标准化事件立即写共享历史；累计 `evaluation_batch_size` 条（默认 2）或末条消息静默 90 秒后由 fast 决定 `KEEP_LISTENING / REPLY / SEMI_ONLINE`，运行窗口最多 20 条；对当前话题有及时、相关且非重复的自然贡献时也可进入前脑。
- smart 生成中每 `evaluation_batch_size` 条（默认 2）普通消息由 fast 决定追加；该配置与 passive 判断共用。一个逻辑周期只允许一次普通中断。显式 @ 可立即 priority replacement，并携带 pending 中紧邻的最多 5 条消息作为上下文；reply-only 不走立即中断。
- Telegram 与 OneBot v11 的 ONLINE 群事件绕过 sender aggregator；SEMI_ONLINE 的 @/回复继续使用原有 3 秒聚合。Telegram 相册保持一个多 ID 原子事件。
- `[ONLINE]` 在群聊中让当前群接管唯一 ONLINE 槽位；`[SEMI_ONLINE]` 只切当前群，标记均不会显示给用户。全局 `AIPresence` 不等同于群监听状态。
- 配置位于 `interaction.group_listener`；架构、竞态与排障见 `docs/architecture/group-chat-listener.md`，核心测试入口为 `tests/test_group_listener.py`。

## 近期关键改动（截至 2026-07-11）

### ✉️ OB11 主动发送私聊/群聊消息工具 — 2026-07-11

- 按 OneBot 11 标准文档新增 `onebotv11_send_private_msg` 与 `onebotv11_send_group_msg` 后脑工具，分别调用 `send_private_msg` / `send_group_msg`。
- 两个工具支持文本 `message` 与 `auto_escape`；群消息未显式提供 `group_id` 时可使用当前群聊上下文，空消息会在本地拒绝。
- 两个工具均标记为中风险且需要主人确认目标与内容；普通当前会话回复仍走 adapter 正常发送链路，避免工具造成重复回复。
- 同步补充 OneBot 平台 prompt 和 adapter 单元测试。

---

## 近期关键改动（截至 2026-07-10）

### 💬 跨平台双向精确 reply / 群聊 @ 控制标记 — 2026-07-10

- 新增平台无关 canonical 协议：`[reply:MESSAGE_ID]` 与 `[at:USER_ID]`。入站原生 reply/@ 会转换为模型可见标记，出站标记由 OneBot v11 / Telegram adapter 还原为平台原生能力。
- 行为改为保守显式控制：普通模型回复不自动引用、不自动 @；`platform_message_id`、入站 `reply_to_message_id` 与应用选择的 `reply_target_message_id` 含义严格分离，聚合器不再自动推导出站 reply 目标。
- `adapters/message_controls.py` 统一有序解析、ID 校验和清理：每个语义 `[SPLIT]` part 只接受首个有效 reply，可保留多个有序 mention；无效、不支持或场景不符的标记必须移除，不能泄漏给用户。
- OneBot v11：入站 reply 与群聊中非 Nora 自身的 at segment 转 canonical 标记，并保留 `[回复内容]...[/回复内容]`；出站转换为 OneBot `reply` / `at` 数组段，兼容 `[reply]`、`[at:current]`、`[at:reply]` 等旧别名，但新 prompt 只推荐显式 ID。
- Telegram：入站 reply 与带可靠 `User.id` 的 `text_mention` 转 canonical 标记；普通 `@username` 不猜数字 ID，bot 自身 mention 不注入。出站群聊 mention 使用 sender 生成的安全 `tg://user?id=...` 链接，reply 使用 Bot API `reply_to_message_id`；私聊 mention 和非法数字 ID 安全忽略。
- 控制标记按 `[SPLIT]` part 局部生效；平台超长切块或文本+媒体发送时，fallback reply 只用于首个实际发送项。adapter 前清洗保留控制标记，最终用户可见/不支持 adapter/异常降级路径统一剥离。
- `AdapterEvent` 与聚合器新增 reply/mention 元数据及有序 `native_reference_parts`，保留每个聚合 part 的原生引用语义。

> 更正 2026-06-09 的旧记录：当时描述的“聚合器默认从当前 `platform_message_id` 锁定 `reply_target_message_id`、代码层自动给普通回复附加引用”已不再成立。当前实现只有显式应用目标或 canonical `[reply:ID]` 才会产生原生引用。

---

## 近期关键改动（截至 2026-06-09）

### 🧷 OB11 群聊多人并发 reply 目标锁定 — 2026-06-09

- 修复同一个 QQ 群里多位群友同时 @ Nora 时，Nora 回复文本属于 A、但 QQ 引用/回复段漂到 B 消息的问题：
  - `MessageAggregator` 在群聊中按 `platform:chat_id:user_id` 建缓冲，同一群不同用户不再合并成一个上下文；同一用户连续多句仍会合并。
  - 聚合上下文新增 `reply_target_message_id`，优先锁定本轮输入自己的 `platform_message_id`，避免后续 `existing.update(context)` 或媒体 ID 合并覆盖回复目标。
  - `core.message_handler.reply_target_message_id()` 统一从 context 取显式 reply 目标，前脑回复、忙碌中断回复、入队确认、后脑回复、轮询审查回复都会把 `reply_to_message_id` 传给 adapter。
  - `OneBotSenderMixin.send_message(..., reply_to_message_id=...)` 会自动在 OB11 数组消息前加 `reply` 段；裸 `[reply]` 也优先使用调用方传入的显式 id，不再默认取“群最近一条 incoming message”。
  - 用户可见文本不需要 Nora/LLM 自己写 `[reply:message_id]`；代码层自动携带，降低模型猜错/漏写风险。

**验证**：
- `PYTHONPATH=.venv\Lib\site-packages` + 临时 `HOME/USERPROFILE=.pytest_home` 后，使用 bundled Python 跑：
  - `pytest tests\test_front_brain_prompt.py tests\test_conversation_identity_and_aggregator.py tests\test_routing.py tests\test_scope_runtime_coordination.py tests\test_message_handler_ack.py tests\test_onebotv11_adapter.py tests\test_message_handler_stop_storage.py tests\test_polling_loop.py -q` → 78 passed。
  - `py_compile adapters\aggregator.py adapters\onebotv11\sender.py core\message_handler.py core\back_brain.py core\polling.py core\interrupt_handler.py tests\test_conversation_identity_and_aggregator.py tests\test_onebotv11_adapter.py tests\test_polling_loop.py` 通过。

### 🧭 OB11 私聊/群聊并发场景隔离 — 2026-06-09

- 修复 QQ/OneBot v11 私聊与群聊同时和 Nora 对话时，前脑/后脑容易把“另一个窗口”的任务进度当成当前群聊/私聊话题的问题：
  - 新增 `core/scene_context.py`，为模型注入内部“当前会话场景 / 回复目标”块，明确当前平台、当前窗口（QQ 私聊/QQ群聊）、回复目标 runtime、当前说话人与群聊边界。
  - 前脑、后脑、前脑审查、忙碌中断判断、后脑入队二次判断都接入该场景块；这是内部 prompt，不会在用户可见消息前强制加 `[私聊]` / `[群聊]` 前缀。
  - 当前窗口是群聊或访客时，跨窗口后脑状态与队列只暴露“另一个窗口有任务在跑/排队”，隐藏私聊任务原文、任务指示、文件和进度细节，避免群聊看到私聊下载任务。
- 修复 `BackendTaskQueue.enqueue()` 对历史包装格式 `{"context": real_context, "text": ...}` 的二次包装问题：
  - 入队时统一规范化为内部 item，出队时拿到的 `item["context"]` 始终是真实平台 context，降低队列任务回到错误窗口的风险。

**验证**：
- `.venv\Scripts\python.exe -m pytest tests\test_front_brain_prompt.py tests\test_conversation_identity_and_aggregator.py tests\test_routing.py tests\test_scope_runtime_coordination.py -q` → 46 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_message_handler_ack.py tests\test_onebotv11_adapter.py tests\test_message_handler_stop_storage.py tests\test_polling_loop.py -q` → 28 passed。
- `.venv\Scripts\python.exe -m py_compile core\scene_context.py core\front_brain.py core\back_brain.py core\interrupt_handler.py core\message_handler.py core\worker_status.py tests\test_front_brain_prompt.py` 通过。

### 🧭 接手核查：媒体发送者、people、OB11 表情、跨端 followup、安全审查 — 2026-06-09

- 媒体入库现状：
  - `memory/image_store.py` 的图片/视频占位与标签补写会保存 `user_id`、`platform`、`chat_id`、`storage_id`、`platform_message_id`、`memory_scope_id`、`place_scope_id`、`owner_id`、`relationship_id`、`actor_display_name`。
  - 也就是说“哪个平台、哪个平台 chat、哪个发送者 id、发送者显示名/昵称”已经入库；暂无单独字段存群名片/QQ 昵称的原始拆分，当前统一进 `actor_display_name`。
- people 目录现状：
  - `brain/prompts.py` 定义并确保 `workspace/data/memory/people/` 存在，主系统提示允许 Nora 给非主人/访客建人物笔记。
  - 当前不会自动全量注入 people 文件；只注入 `SOUL.md` / `USER.md` / `data/memory/MEMORY.md` / 可选 `SCHEDULE.md`。这避免把所有访客笔记塞进上下文。
- OneBot v11 / QQ 表情：
  - `adapters/onebotv11/sender.py` 支持 `[face:ID]` 与 `[emoji:ID]`，转成 OneBot `{"type":"face","data":{"id":"ID"}}` 消息段。
  - `adapters/onebotv11/PROMPT.md` 已告知模型：表情 ID 必须来自已知表情表或用户明确给出的 ID，不能凭空编。
- 跨平台 followup 漂移修复：
  - 普通上下文仍按 `memory_scope_id` 跨平台共享。
  - 但 followup / wrapup 会主动发消息，`core/scheduler_mixin.py` 现在只取当前 `place_scope_id` 的当前段消息做检测与生成，避免把 A 平台内容追问到 B 平台。
- security 配置修复：
  - 之前只配置 `llm.models.security` 不等于启用审查，`security.enabled` 为 false 时不会生效。
  - `cli.py` 的模型配置步骤现在会同步写 `security.enabled`；`brain/tools.py` 启动日志会明确提示 security alias 已配置但审查未启用，或启用后是否 fallback 到 fast。

**验证**：
- `.venv\Scripts\python.exe -m pytest tests\test_onebotv11_adapter.py tests\test_scope_runtime_coordination.py tests\test_cli_first_run_config.py tests\test_tool_security_config.py -q --basetemp .pytest-tmp` → 33 passed。
- `.venv\Scripts\python.exe -m py_compile adapters\onebotv11\sender.py core\scheduler_mixin.py brain\tools.py cli.py tests\test_onebotv11_adapter.py tests\test_scope_runtime_coordination.py tests\test_cli_first_run_config.py tests\test_tool_security_config.py` 通过。
- 额外跑 `tests\test_message_history.py tests\test_image_memory.py tests\test_image_store_platform_message_lookup.py tests\test_delivery_target.py` 时 71 passed / 1 failed；失败为当前机器 C 盘 `Free=0` 导致 `OSError: [Errno 28] No space left on device`，非断言失败。

---

### ⚡ OneBot v11 WebSocket 入站延迟优化 — 2026-06-09

- 修复 OneBot v11 正向/反向 WebSocket 入站消息偶发延迟数秒到十几秒的问题：
  - 根因：WS `listen()` 收到 event 后直接 `await adapter.handle_onebot_event()`；事件处理中又会调用同一条 WS 的 `get_msg` / `get_group_info` / `get_file` 等 API，API echo 响应也依赖 `listen()` 继续读包，形成“接收循环等待事件处理，事件处理等待接收循环”的阻塞。
  - `adapters/onebotv11/client.py` 改为收到 event 后创建后台任务处理，接收循环立即继续读取后续包和 API echo；这与 nonebot2 onebot adapter 的事件投递方式一致。
  - 为避免后台化带来同一群/私聊消息乱序，客户端按会话键（group/user）串行事件任务：同一 chat 保序，跨 chat 并行，WS 收包不被阻塞。
  - 关闭连接/重连时会取消并收敛后台事件任务，异常统一记录日志，避免静默丢错。

**验证**：
- `.venv\Scripts\python.exe -m pytest tests\test_onebotv11_adapter.py -q` → 17 passed。
- `.venv\Scripts\python.exe -m py_compile adapters\onebotv11\client.py tests\test_onebotv11_adapter.py` 通过。

---

### 💬 OneBot v11 群上下文 + QQ 回复/艾特标记 + 来源标签防泄漏 — 2026-06-09

- 修复跨平台来源标签被模型复述到用户侧的问题：
  - `core/routing.py::sanitize_user_visible_text()` 现在会剥离 `[来自 platform:chat_id / 昵称]` 这类内部来源标签。
  - `adapters/PROMPT.md` 明确来源标签只供模型理解消息出处，禁止原样复述。
- OneBot v11 入站群聊上下文补齐到接近 Telegram：
  - 群聊/私聊均继续从 `sender.card` / `sender.nickname` 写入 `user_name`（发送人显示名）。
  - 群聊会调用并缓存 `get_group_info`，注入 `chat_title` / `group_title` / `group_display_name` / `group_member_count`，在线人数标记为 OneBot v11 标准不可用。
- OneBot v11 出站新增 QQ 控制标记：
  - `[reply]` 回复最近一条入站 QQ 消息；`[reply: message_id]` 回复指定 OneBot message_id。
  - `[at: QQ号]` 艾特指定 QQ；`[at:current]` 艾特当前说话人；`[at:reply]` 艾特被当前消息 reply 的对象。
  - `adapters/onebotv11/PROMPT.md` 已教模型使用这些标记，sender 会转成 OneBot 数组消息段，不要求模型手写 CQ 码。
- 修复 OneBot v11 与 Telegram 群聊中“单独艾特 Nora 没有正文”不响应的问题：
  - 根因是触发用的 `@Nora` / `@NoraBot` 被剥离后，剩余文本为空，随后被空消息过滤逻辑直接 `return`。
  - OB11/TG 现在会把这种 mention-only 触发转换为内部文本 `[群聊单独艾特: 用户只艾特了你，没有附加文字。请自然地回应。]`，继续进入聚合器和 controller；未艾特的群聊空消息仍不会触发。
- 修复 OneBot v11 群聊中“回复图片/媒体并 @Nora”时拿不到被回复媒体的问题：
  - 触发判断阶段仍轻量调用 `get_msg` 补 `reply_to_user_id/name`，确认消息确实要进入 Nora 后，才解析被回复消息内容，避免普通群 reply 也触发媒体下载。
  - 被回复消息里的图片/视频/语音/文件会转换为 `[image: ...]` / `[video: ...]` / `[file: ...]` 注入当前输入；图片仅有 `file` 无 `url` 时会尝试 `get_image`，视频/文件尝试 `get_file`，语音尝试 `get_record`。
  - 当前 context 会把被回复媒体消息 id 放进 `platform_message_ids`，便于图片入库和后续按原始 QQ message_id 反查。
  - 兼容 NapCat/OneBot 事件变体：reply id 不再只从数组消息段取，也会从 `raw_message` 的 `[CQ:reply,id=...]`、顶层 `reply_to_message_id/reply_message_id/...`、嵌套 `reply/source/quote` 中提取；当 `message` 数组漏掉 reply 段但 `raw_message` 仍有 CQ reply 时，会补入 reply 段再走 NoneBot2 类似的 `get_msg` 流程。
  - mention-only fallback 现在不会覆盖 reply 场景；如果仍掉进单独艾特，debug 日志会输出该 OneBot event 的前 2000 字符，便于继续兼容具体实现字段。
- 修复 OneBot/NapCat 回复视频已落盘但无法理解的问题：
  - NapCat 可能把视频下载成 `video_xxx.bin`，此前 `brain.multimodal._load_video_bytes()` 只靠扩展名/MIME 判断，导致真实视频被判定为“文件并非视频”并跳过。
  - 多模态层现在会对通用扩展名文件做视频容器头探测，支持 MP4/WebM/AVI/FLV/WMV；`[video: ...bin]` 只要文件内容是视频，就会进入视频模型。
  - OneBot reply 媒体系统备注不再包含字面量 `[image: ...]` / `[video: ...]`，避免被多模态正则误当成真实媒体标签加载。

**验证**：
- `.venv\Scripts\python.exe -m pytest tests\test_routing.py -q` → 24 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_onebotv11_adapter.py -q` → 13 passed。
- `.venv\Scripts\python.exe -m py_compile core\routing.py tests\test_routing.py adapters\onebotv11\main.py adapters\onebotv11\sender.py tests\test_onebotv11_adapter.py` 通过。
- `.venv\Scripts\python.exe -m pytest tests\test_onebotv11_adapter.py tests\test_telegram_adapter_modules.py -q` → 36 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_onebotv11_adapter.py -q` → 16 passed。
- `.venv\Scripts\python.exe -m py_compile adapters\onebotv11\main.py adapters\onebotv11\media.py tests\test_onebotv11_adapter.py` 通过。
- `.venv\Scripts\python.exe -m pytest tests\test_onebotv11_adapter.py -q` → 20 passed。
- `.venv\Scripts\python.exe -m py_compile adapters\onebotv11\main.py adapters\onebotv11\message.py adapters\onebotv11\media.py tests\test_onebotv11_adapter.py` 通过。
- `.venv\Scripts\python.exe -m pytest tests\test_multimodal_input.py tests\test_onebotv11_adapter.py -q` → 23 passed。
- `.venv\Scripts\python.exe -m py_compile brain\multimodal.py adapters\onebotv11\main.py tests\test_multimodal_input.py` 通过。

---

### 🩹 前脑内部备注泄漏 + OpenAI Responses Stream 兼容 + CLI 本地数据清理补齐 — 2026-06-09

- 修复前脑/审查上下文中供模型阅读的 `[内部备注·上一轮路由记录]`、`[系统备注]` 偶发被模型原样复述给用户的问题：
  - `core/routing.py` 新增 `sanitize_user_visible_text()`，统一剥离内部备注块与时间戳。
  - `parse_front_brain_response()` / `parse_front_brain_review()` 现在在解析后统一清洗用户可见文本。
  - `core/message_handler.py::_send_split_message()` 与 `core/controller.py::_send_platform_message()` 再加一层发送前清洗，作为跨 Telegram / OneBot v11 的最终兜底。
- 修复 OpenAI Responses API 流式调用在当前 SDK（本地为 `openai 2.17.0`）下报错 `AsyncResponses.stream() got an unexpected keyword argument 'stream'`：
  - `brain/providers/openai.py` 的 Responses 分支改为 `responses.create(stream=True)`。
  - 新增按函数签名过滤 kwargs 的兼容层，避免不同 SDK / OpenAI 兼容端点的参数差异再次触发异常。
- CLI 补齐“清空本地数据文件”能力：
  - 新增 `python cli.py history clear-data`，覆盖 `message_history.db`、`message_log.db`、`context_compression.db`，并可选清理 `cost_tracker.db`。
  - 历史管理菜单新增“🗃️ 清空本地数据文件”，采用二次确认（输入 `DELETE LOCAL DATA`）。
  - `docs/CLI_USAGE.md` 与 `docs/onboarding/QUICK_REFERENCE.md` 已同步更新，修正旧 `--show-history` / `--clear-history` 口径。

**验证**：
- `.venv\Scripts\python.exe -m pytest tests\test_routing.py tests\test_openai_tools.py tests\test_cli_data_cleanup.py tests\test_message_history_clear_cascade.py -q` → 35 passed。
- `.venv\Scripts\python.exe -m py_compile core\routing.py core\message_handler.py core\controller.py brain\providers\openai.py cli.py tests\test_routing.py tests\test_openai_tools.py tests\test_cli_data_cleanup.py` 通过。

---

## 近期关键改动（截至 2026-06-08）

### 🔌 Telegram 配置迁移 + OneBot v11/NapCat Adapter — 2026-06-08

- Telegram `bot_token` 从全局 `config.yml` 迁移到 `adapters/telegram/config.json`；CLI 配置向导不再询问 Telegram token。
- 新增 adapter 私有配置约定：每个 adapter 使用自己目录下的 `config.json` + `config.example.json`，文档已更新。
- 新增 `adapters/onebotv11/`：支持 OneBot v11 正向 WebSocket 与反向 WebSocket、私聊/群聊消息接收、@/reply 群聊触发、媒体下载到 `workspace/data/onebotv11/`、文本/媒体消息发送、撤回。
- OneBot v11 adapter 暴露标准工具：登录/状态/版本、群信息/群列表/群成员/群成员列表/群荣誉/好友列表/消息/合并转发查询，以及点赞、撤回、禁言、踢人、全员禁言、管理员、群名片、群名、退群、专属头衔等高风险群管工具。
- `adapters/onebotv11/config.json` 中 `enable_napcat_api=true` 时额外暴露 `onebotv11_napcat_*` 工具：戳一戳、已读、好友历史、自定义表情、AI 语音角色/生成/发送、合并转发等 NapCat 扩展 API。
- `main.py` 新增 `--adapter telegram|onebotv11`，默认 Telegram。
- 后脑 adapter tool 上下文注入从 Telegram 特例泛化：当前平台工具会按签名自动补 `chat_id`/`group_id`，reply 场景自动补 `user_id`。

**验证**：见本次会话最终记录。

---

### 🛠️ Adapter 动态工具 + Telegram 群管工具 — 2026-06-08

为 adapter 增加可向后脑暴露平台 API 的工具标准，并先在 Telegram 落地群聊查询/群管工具：

- `adapters/base.py` 新增 `AdapterToolSpec`、`BaseAdapter.get_adapter_tools()`，`PlatformFeatures` 增加成员查询、群管、成员 tag 能力字段。
- `brain/tools.py` 启动时注册当前 adapter tools，并通过 `ToolManager.get_tool_intros()` 把动态工具简介注入前脑/后脑。
- `adapters/telegram/tools.py` 新增 Telegram Bot API 工具：成员总数、管理员列表、单成员查询、禁言/解除禁言、封禁/解封、管理员头衔、普通成员 tag。
- Telegram reply context 现在补充 `reply_to_user_id/reply_to_user_name/reply_to_message_id`，后脑调用 Telegram 群管工具时会自动注入当前 `chat_id`，reply 场景会自动补目标 `user_id`。
- Prompt/文档明确：Telegram Bot API 不支持枚举完整普通成员名单；高风险真实平台操作必须先向主人口头确认，若 `security.enabled=true` 仍会走安全审查模型。

**验证**：
- `.venv\Scripts\python.exe -m py_compile adapters\base.py adapters\telegram\main.py adapters\telegram\message.py adapters\telegram\tools.py brain\tools.py core\back_brain.py core\front_brain.py` 通过。
- `.venv\Scripts\python.exe -m pytest tests\test_adapter_base.py tests\test_telegram_adapter_modules.py tests\test_adapter_prompt_loading.py tests\test_telegram_adapter_tools.py -q` → 39 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_nora_prefs_telegram.py tests\test_conversation_identity_and_aggregator.py tests\test_back_brain_tool_context.py -q` → 17 passed。
- 全量 `.venv\Scripts\python.exe -m pytest -q` → 228 passed / 16 failed；失败项仍为既有基线（CLI/CostTracker/pytest-asyncio/routing/timezone）。

---

### 🌐 多平台消息段 / Followup / 打断全局协调 — 2026-06-08

在“同一个 Nora 在不同地方说话”的人格模型上，保持**消息段按 `memory_scope_id` 全局闭合**，不引入 `scene_scope_id`、不按平台拆段：

- `core/controller.py` 新增 scope 级运行时 helper：按 `memory_scope_id` 聚合 runtime、查全局忙碌后脑、共享后脑队列、判断全局消息段是否 idle。
- `core/scheduler_mixin.py` 的 `_transition_to_semi_online` 增加全局闭段 gate：单个平台 followup END 只清理本 runtime，若同 scope 仍有前脑/后脑/队列/followup/近期 activity，则保持 `ONLINE` 且暂不 `close_session`。
- `core/message_handler.py` / `core/interrupt_handler.py` / `core/polling.py` 改为 scope 级后脑注意力与队列：任一平台可 stop/change/queue 当前共享后脑任务，队列跨平台可见，出队任务回到其原始 runtime。
- `core/front_brain.py` 的后脑运行时状态注入改为 scope 级，让任一平台的前脑都能看到另一个平台正在处理的后脑任务。
- 聚合器保持 `platform:chat_id` 隔离，不同平台输入不会在 3 秒缓冲内硬合并；共享连续性仍由 `ConversationIdentity.memory_scope_id` 负责。

**验证**：
- `.venv\Scripts\python.exe -m py_compile core\controller.py core\message_handler.py core\interrupt_handler.py core\polling.py core\scheduler_mixin.py core\front_brain.py core\back_brain.py`
- `.venv\Scripts\python.exe -m pytest tests\test_scope_runtime_coordination.py tests\test_conversation_identity_and_aggregator.py tests\test_message_handler_stop_storage.py tests\test_followup_end_grace_delay.py tests\test_message_handler_ack.py tests\test_back_brain_tool_context.py tests\test_delivery_target.py tests\test_default_chat_target_store.py tests\test_message_history.py -q` → 43 passed, 1 known warning。
- 全量 `.venv\Scripts\python.exe -m pytest -q` → 200 passed / 16 failed；失败项与既有基线一致（CLI/CostTracker/pytest-asyncio/routing/timezone）。

---

### 🌉 Adapter 统一化 + Telegram 渐进拆分 — 2026-06-08

参考 NoneBot2 的 Adapter/Event/Message 分层思想，但不引入完整 driver/Bot/API hook 系统，完成 NORA 轻量 adapter 标准化：

- `adapters/base.py` 新增 `AdapterMetadata`、`AdapterPlatformInfo`、`AdapterEvent`、`MessageSegment`、`AdapterMessage`，并保留原 `BaseAdapter.run/send_message/start_typing/stop_typing/edit/delete` 契约。
- 新增 `adapters/PROMPT.md` 通用平台适配协议；`brain/prompts.py` 现在按“通用 adapter prompt → 平台专属 prompt”顺序注入。
- `adapters/telegram/PROMPT.md` 收敛为 Telegram 专属格式/长度/群聊/媒体规则；新增 `adapters/telegram/metadata.json` 说明真实 Telegram 平台、能力、限制和隐私提示。
- `adapters/telegram/main.py` 对外入口保持 `TelegramAdapter` 不变，内部拆分为 `constants.py`、`formatting.py`、`message.py`、`sender.py`、`incoming.py`、`reply.py`、`commands.py`、`callbacks.py`、`cleanup.py`。
- 新增 `docs/architecture/adapter-system.md` 和更新 `adapters/ADAPTER_GUIDE.md`，作为后续 QQ/OneBot/Web 等新 adapter 的统一开发参考。

**验证**：
- `python -m py_compile adapters/base.py adapters/telegram/*.py brain/prompts.py` 通过。
- `.venv\Scripts\python.exe -m pytest tests/test_adapter_base.py tests/test_adapter_prompt_loading.py -q` → 6 passed。
- `.venv\Scripts\python.exe -m pytest tests/test_nora_prefs_telegram.py tests/test_conversation_identity_and_aggregator.py -q` → 11 passed。

---

## 近期关键改动（截至 2026-06-06）

### 🌐 跨平台接力 + 主人/访客分离 — 2026-06-06（全 5 Phase 完成）

把 N.O.R.A 从"多平台各自一段上下文"改成"**同一个 Nora 在不同地方说话**"，并把"用户即主人"拆成**主人 (owner)** 与**说话人 (actor)**。完整设计见 [跨平台接力与五层身份模型](./architecture/cross-platform-relay.md)。

- **Phase 1 身份层**：`core/conversation_identity.py` 扩五层身份（`owner_id`/`relationship_id`/`memory_scope_id`/`place_scope_id`/`actor_display_name`/`is_owner`）；新建 `core/owner_registry.py` + `data/owner_bindings.json` 最小主人识别（私聊首位自动绑定、群聊非绑定即访客、config 预声明）。
- **Phase 2 共享上下文 + 迁移**：`memory/message_history.py` / `memory/context_store.py` 三表 + 镜像/压缩库新增 `memory_scope_id`/`place_scope_id`/`actor_display_name` 列，读取按 `memory_scope_id` 分区、旧键 fallback；启动自动 `ALTER TABLE` 迁移，旧行补默认 scope，非破坏性。`close_session` 改按共享 scope 关闭连续对话。
- **Phase 3 RAG/媒体/工具上下文**：`memory/rag.py`/`vector.py`/`image_store.py` payload 加同套 scope 字段，检索按 `memory_scope_id` 过滤、旧记录回退；`view_media` 注入 `memory_scope_id` 跨平台找回图，精确回查仍走 `platform+platform_message_id+chat_id` 不串图。
- **Phase 4 主动消息投递**：新建 `core/delivery_target_store.py` + `data/delivery_target_state.json`，**仅记私聊端**（群聊活跃不更新，绝不自动发进群）；`ProactiveScheduler`/`TriggerManager` 注入 `resolve_delivery_callback`，闹钟原样投递不重定向、proactive/trigger 默认投最近活跃端回退 `default_chat_id`（语义降级为 fallback）。
- **Phase 5 Prompt + 文档**：`brain/templates/system.jinja` 新增 §0.5 跨平台接力准则 + §5 主人/访客边界 + `data/memory/people/` 自主人物记忆区；`brain/prompts.py` 的 `load_identity_context`/`get_system_prompt` 注入 `<current_speaker>`（主人私聊不注入、主人群聊提示得体性、访客提示边界），`_ensure_workspace_identity_files` 确保 `people/` 目录；架构文档新增本文 + 更新 message_history/image-memory 主键语义 + identity_files 主人/访客 + 修复根 `onboarding/` 空目录误导。

**涉及文件**：`core/conversation_identity.py`、`core/owner_registry.py`、`core/delivery_target_store.py`、`memory/message_history.py`、`memory/context_store.py`、`memory/rag.py`、`memory/vector.py`、`memory/image_store.py`、`core/back_brain.py`、`core/front_brain.py`、`core/message_handler.py`、`core/scheduler.py`、`triggers/manager.py`、`core/controller.py`、`brain/prompts.py`、`brain/templates/system.jinja`、`config.py`、`config.example.yml` + 测试与文档。

**部署注意**：Phase 2/3 涉及 SQLite/Mongo/Qdrant schema 迁移——上生产 `sp.wris.me` 前必须备份 `message_history.db`/`message_log.db`/`context_compression.db`。`data/owner_bindings.json`、`data/delivery_target_state.json` 是纯新增运行时状态，无迁移、非破坏性。

**测试基线**：全量 `pytest` = 183 passed / 16 failed；16 个失败为既有基线（CLI 定价 ×3、context_pricing ×5、cost_tracker ×2、message_history_retry_queue ×2[缺 pytest-asyncio]、routing ×3、timezone ×1），与本次改动无关。

## 早期关键改动（截至 2026-05-25）

### 🛠️ 八项修复与增强 — 2026-05-25

1. **视频回复不再重新生成 VIDEO_TAGS**
   - `core/back_brain.py`：视频标签生成逻辑对齐图片——`from_tool=True` 的视频跳过占位入库、标签提取和存储。`expected_video_ids` 现在过滤 `from_tool` 视频，整个标签提取块用 `if expected_video_ids:` 守护。

2. **回复图片/视频不再重复提取 tag（确认）**
   - 图片侧已有 `from_tool` 过滤（2026-05-07 修复），本次补齐视频侧同等逻辑。

3. **告知 Nora 可自行修改 trigger 过滤规则**
   - `brain/templates/system.jinja`：新增"外部触发器"段，说明 `triggers/` 子系统、邮件过滤模板路径、修改方式。

4. **传图片/视频时附带文件名**
   - `adapters/telegram/main.py`：视频消息现在附带 `(文件名: xxx)` 信息（当 Telegram 提供 `file_name` 时）。

5. **消息聚合适配图片+视频混合媒体组**
   - `adapters/telegram/main.py`：视频处理器新增 `media_group_id` 支持，与图片共用同一缓冲区。`_flush_media_group` 改为直接拼接预格式化的媒体标签（`[image: ...]` / `[video: ...]`），不再硬编码 `[image: ...]` 包装。

6. **修复 custom scope 按钮逻辑**
   - `adapters/telegram/main.py`：`_handle_custom_scope_callback` 中 "none" 按钮改为 toggle（当前是 none 则恢复 all，否则设为 none），避免用户取消所有 scope 后意外变成 "all"。

7. **告诉 Nora 查看媒体文件时必须真的调用后脑**
   - `brain/templates/system.jinja`：强化 `view_media` 强制规则，明确 IMAGE_TAGS 只是索引标签不能替代视觉分析。
   - `brain/templates/front_brain.jinja`：新增"再次查看媒体 = 必须后脑"规则，禁止前脑凭记忆回答。

8. **/context 指令修复**
   - `adapters/telegram/main.py`：新增 `CommandHandler('context', self._context_command)` 注册，解决 `/context` 被 `~filters.COMMAND` 过滤导致无响应的问题。

**涉及文件**：
- `core/back_brain.py`
- `adapters/telegram/main.py`
- `brain/templates/system.jinja`
- `brain/templates/front_brain.jinja`

**验证**：`python -m py_compile` 所有改动文件通过；Jinja 模板解析通过。

---

## 近期关键改动（截至 2026-05-07）

### 🛠️ 七项修复 — 2026-05-07

1. **`AttributeError: _followup_suspended_until_idle` 崩溃修复**
   - `core/controller.py`：在 `__init__` 中初始化 `self._followup_suspended_until_idle: Dict[str, bool] = {}`，scheduler_mixin._mark_scheduler_idle 不再 AttributeError。

2. **default_chat_id 兜底注入 trigger / scheduler**
   - `core/scheduler_mixin.py::start_scheduler`、`core/controller.py::start_triggers`：启动时若 `default_chat_id` 为空，再次从 `default_chat_id_store.load_default_chat_id()` 读取持久化值，并把 `TriggerManager.default_chat_id` 同步给 `_triggers` 列表中已注册的子 trigger（如 EmailTrigger）。
   - `core/message_handler.py`：首次用户消息触发持久化时，同样把 `storage_id_for_scheduler` 同步到子 trigger 实例。

3. **/model 返回按钮真正返回上一级**
   - `adapters/telegram/main.py`：抽出 `_build_model_alias_menu`；provider 选择菜单的"取消"旁加 `⬅ 返回 → model_pick:back`，`_handle_model_pick_callback` 处理 back 重渲染顶层 alias 菜单；模型分页菜单的取消按钮替换为 `⬅ 返回 → model_pick:{alias}`（回到 provider 列表）+ 取消。

4. **prompt：后脑非静默操作必须告知用户**
   - `brain/templates/front_brain_review.jinja`：在"上下文驱动的打扰判断"段顶部新增"底线规则"，明确：非静默操作（即非 SECRET/MEMORY/USER/SCHEDULE 等内部记录或后台同步）必须给用户回执；只有静默后台任务或上下文已完整覆盖时才允许 `[NO_REPLY]`。

5. **回复历史图片不再重新生成 IMAGE_TAGS**
   - `core/back_brain.py`：把图片标签校验入口从 `(expected_ids or multimodal_images)` 收紧为仅 `expected_ids`（即必须存在"非工具回查"的新用户图）；`from_tool=True` 的库内图片不会再触发标签生成 + 校验 + 失败重试链路。

6. **前脑生成阶段允许新消息打断重做（聚合器语义）**
   - `core/controller.py`：新增 `self.front_brain_tasks: Dict[str, asyncio.Task]` + `self.front_brain_partial: Dict[str, str]`（流式实时缓冲）。
   - `core/front_brain.py`：`_run_with_retries` 在每次流式 chunk 写入 response_text 时同步写入 `self.front_brain_partial[chat_id]`；成功完成或异常退出时清理。生成开始处会读取并注入 `prior_partial`（上一次被打断时未发出的草稿），作为"仅模型可见的系统备注"附加到 `user_prompt` 末尾，让模型基于草稿 + 新消息重新组织回复。
   - `core/message_handler.py`：handle_new_message 入口若发现前脑任务仍在生成，则 `cancel + wait`，**故意不清理 `front_brain_partial[chat_id]`**——这样下一次前脑生成时能拿到这段草稿；前脑调用本身改用 `asyncio.create_task` 跟踪，CancelledError 静默丢弃本轮结果。
   - 行为对齐聚合器：① 前脑还未输出任何文本时被打断 → 草稿为空字符串，新消息从零开始（等价于聚合后的合并）；② 前脑已生成部分文本时被打断 → 草稿被带入下一轮，模型综合草稿与新消息重新生成。

7. **/status 提供更细粒度状态**
   - `core/message_handler.py::_cmd_status`：前脑分支区分 🟡 正在生成 / 正在审查 / 正在轮询 / 🟢 空闲，并展示活跃 followup 定时器数量。

8. **后脑忙碌时分开发的多张图片不再触发"在忙"硬编码提示**
   - `core/message_handler.py`：后脑忙碌且收到带真实图片的新消息时，原本会发一条机械的 "后端忙碌，已把图片需求排队，稍后处理哦～"。改为静默入队（仅写用户消息到 DB），让前脑后续基于上下文自然反应，行为对齐 MessageAggregator 的精神。

**涉及文件**：
- `core/controller.py`
- `core/scheduler_mixin.py`
- `core/message_handler.py`
- `core/back_brain.py`
- `adapters/telegram/main.py`
- `brain/templates/front_brain_review.jinja`

**验证**：`python -m py_compile core/controller.py core/scheduler_mixin.py core/message_handler.py core/back_brain.py core/front_brain.py adapters/telegram/main.py` 通过。

---

## 近期关键改动（截至 2026-05-04）

### 🖼️ view_media 提问参数 + 移除持续强制 image 模型规则 — 2026-05-04

**背景**：旧设计里 `view_media(use_image_model=true)` 或上游 `[USE_IMAGE_MODEL]` 会让后脑持续使用 image 模型，直到 `crop_image_for_llm` 输出后才解除。用户希望删除这个持续规则，改为让 `view_media` 自带“提问参数”，由后脑对特定图片发起一次性图片模型分析。

**修复：**
- `core/back_brain.py`
   - 删除 `force_image_model_until_crop_done` 状态机。
   - 后脑模型路由改为：只有当前轮真正携带 `multimodal_images` 图片字节时才使用 `image` 模型，否则回到 `coder`。
   - `view_media` / `crop_image_for_llm` 返回 `MediaTag` 时，下一轮注入图片并临时走 image 模型；该批图片消费后不再持续强制 image。
   - 若 `view_media(question=...)` 返回图片，下一轮 image prompt 会注入该问题，要求只围绕该问题观察回答。
- `brain/tools.py`
   - `view_media` 新增 `question: str = ""` 参数。
   - `question` 非空时自动强制 `return_image=true`，确保下一轮 image 模型能读取真实图片；`return_image=false` 仅用于元数据/标签/OCR 检索。
   - 移除 `use_image_model` 参数与输出提示。
   - `local_path` 直读本地图片现在不再依赖 ImageStore 初始化；无图片记忆库时也可用于手动识别本地图片。
- `brain/templates/system.jinja` / `brain/templates/front_brain.jinja`
   - 更新工具使用规则：需要图片模型回答具体问题时使用 `view_media(question=...)`，不要再使用持续强制模型标记。
- `docs/onboarding/QUICK_REFERENCE.md`
   - 更新 `view_media` 速查说明。

**验证：**
- `python -m py_compile core/back_brain.py brain/tools.py core/routing.py` 通过。
- `pytest tests/test_image_memory.py tests/test_multimodal_input.py -q` → 34 passed。

**涉及文件**：
- `core/back_brain.py`
- `brain/tools.py`
- `brain/templates/system.jinja`
- `brain/templates/front_brain.jinja`
- `docs/onboarding/QUICK_REFERENCE.md`
- `tests/test_image_memory.py`

---

### 🖼️ 用户回复历史图片不再误报 + followup 告别后绝不再发问候 — 2026-05-04

**背景**：用户反馈两个问题：
1. 当用户在 Telegram **reply** 一张历史图片消息时，系统总是冒出硬编码提示 "没有读取到图片内容，可能文件路径或下载失败了，能再发一次原图吗？"，体验非常机械；同时担心图片接收失败时模型会凭空想象图片内容。
2. 用户和 Nora 告别后（如 "晚安"/"拜拜"），followup 循环还会再发一两条问候才真正结束对话。

**修复 1：reply_photo 占位符不再触发硬编码"图片缺失"消息**

- `adapters/telegram/main.py :: _extract_reply_info`：
  - 历史中没找到对应消息时，回退分支不再返回 `[image: reply_photo]`（这个伪路径会触发上游 `has_image_input` 但又永远加载失败）。
  - 改为返回纯描述文本 `"(用户引用了一张历史图片，但本轮未重新附带图片内容)"`，让模型从语义上理解。
- `core/message_handler.py :: handle_new_message`：
  - 把图片检测拆成两个语义清晰的标志：`has_image_marker`（文本里疑似带图片标记）vs `has_real_image`（真的成功加载到图片字节）。
  - **`image_input_detected` 现在只在真有图片字节时为 True**——也就是只有真正能交给后脑 image 模型处理的输入才会走 image 分支。
  - 当出现"标记但没字节"时，不再硬编码弹 `"没有读取到图片内容…"`，而是在 `context["image_load_failed"] = True` 标记后，让消息走正常的前脑流程，由模型基于上下文自然回应。
  - 后端忙碌时图片入队的判断也同步收紧为"真有图片字节才入队"。
- `core/front_brain.py :: _generate_front_chat_response`：
  - 当 `context["image_load_failed"]` 为真时，向 user_prompt 追加一段系统备注，告诉模型 "本轮系统没真正接收到图片数据"，**绝对不能假装看到图片或编造图片内容**，并给出更自然的语气示例。
- `brain/templates/system.jinja §9 富媒体发送`：
  - 新增 **🚫 图片/媒体接收的诚实底线** 段，统一覆盖前脑/后脑：网络/系统问题导致没收到图片时，不许编造视觉细节、不许臆想内容；可调用 `view_media` 检索历史图片；宁可如实说"图片好像没传过来"也不要瞎编。

**修复 2：followup_loop 在双方告别后不再发任何收尾消息**

- `core/scheduler_mixin.py`：新增告别检测三件套
  - `_GOODBYE_PATTERNS`：晚安 / 再见 / 拜拜 / 早点睡 / 先这样 / 先不聊了 / 下线 / 有空再聊 / 不打扰你了 / `[SEMI_ONLINE]` / `[TASK_DONE]` / `good night` 等。
  - `_looks_like_farewell(content)`：检查文本尾部是否命中任一告别短语。
  - `_conversation_already_closed(chat_id)`：基于当前消息段最近 6 条 user/assistant 消息，判断是否已自然告别（AI 末条已是收尾 / 用户告别且 AI 已回 / 用户最后一条就是告别）。
- `_followup_loop` 加两道安全闸：
  1. **入口闸**：定时器到期、初次 sleep 醒来后，如果检测到对话已自然告别 → 直接 `_transition_to_semi_online`，**不发任何消息**。
  2. **每轮决策后闸**：每次 `_detect_followup_intent` 给出 `FOLLOWUP/WAIT/END` 后，再用 `_conversation_already_closed` 兜底，命中即静默退出。
- 修复 `WAIT` 超时分支：以前**总是**发 `_send_wrapup_message`，现在只在 `count >= 2`（AI 真的追话过且无回应）且没检测到告别时才发，否则静默收尾。
- 修复 `FOLLOWUP` 概率连续跳过 → END 的兜底：同样加上 `_conversation_already_closed` 检查。
- 保留 END 分支原有的"`count >= 2` 才发 wrapup + grace delay"语义（仍能通过 `tests/test_followup_end_grace_delay.py` 的正则断言）。

**验证**：
- `python -m py_compile` 所有改动文件通过。
- `pytest tests/test_followup_end_grace_delay.py tests/test_message_handler_ack.py tests/test_multimodal_input.py tests/test_image_memory.py tests/test_message_history.py tests/test_lexicon_manager.py tests/test_lexicon_global_prompt.py tests/test_email_trigger_filter.py tests/test_link_sanitize.py tests/test_split_delay_parser.py tests/test_search_tool.py tests/test_tool_plan.py tests/test_schema_fix.py` → 59 passed, 1 skipped。
- 其他若干测试因本地 `config.yml` 解析错误（与本轮改动无关）无法 collect，已跳过。

**涉及文件**：
- `adapters/telegram/main.py`
- `core/message_handler.py`
- `core/front_brain.py`
- `core/scheduler_mixin.py`
- `brain/templates/system.jinja`

---

## 近期关键改动（截至 2026-05-02）

### 🔇 NO_REPLY 升级为常规决策 + SEMI_ONLINE 自觉性再加固 — 2026-05-02

**背景**：用户反馈 ① 后脑工作完成后的前脑审查应基于上下文判断"是否值得打扰用户"，并允许直接 `[NO_REPLY]` 静默收尾；② Nora 应当在任意合适场景主动使用 `[NO_REPLY]`，而不仅限于"内部备注/任务指示"；③ `[SEMI_ONLINE]` 主动输出的自觉性仍偏弱。

**修复（纯 prompt 调整，未改动 Python 逻辑——routing.py 已支持解析）：**

1. **`brain/templates/front_brain_review.jinja`**
   - 在"你的职责"段新增第 2 条核心：**"基于上下文判断是否值得打扰用户"**——把它提升为审查阶段的核心决策项，而不是兜底。
   - 新增 **🌟 上下文驱动的"打扰判断"** 整段：列出"倾向 `[NO_REPLY]` 的典型场景"（任务结果对用户无感知价值 / 结果已被前脑预告 / 用户已离开 / 用户消息只是承接 / 失败但用户没追问 / 重复信息）、"仍应该发声的典型场景"，以及"判断流程（短版）"四步法。
   - 把"决策信号 / 可选：不向用户回复"重写为 **🔇 主要决策：`[NO_REPLY]` — 静默不打扰**，并明确"这是常用决策，不是兜底"。
   - SEMI_ONLINE 段加上"它是你的主动权——只要审查判断对话该收尾了，直接加上即可，不需要等用户开口"。
   - 新增四个示例：静默写入 USER 偏好、SECRET 内部吐槽、SCHEDULE 后台同步、用户已离开时的失败结果——全部 `[NO_REPLY][TASK_DONE][SEMI_ONLINE]` 收尾。

2. **`brain/templates/front_brain.jinja`**
   - "你的职责"第 3 条"不黏人，知道何时安静"扩写为**每轮发言前的内部自检三问**（对话是否消散 → SEMI_ONLINE / 内容是否噪音 → NO_REPLY / 用户是否离开 → SEMI_ONLINE），强调"输出 SEMI_ONLINE 不需要任何特殊触发条件"。
   - 重写"🚨 优先规则：`[NO_REPLY]` 静默工作"段：明确"这是常用工具不是兜底"，列举 7 类应该使用的场景（包含"任何你判断'此刻发声 = 噪音'的时刻"），并给出与 `[NEED_BACKEND]` / `[SEMI_ONLINE]` 的组合用法。
   - SEMI_ONLINE 段开头加上"它是你的主动权"措辞。
   - 删除原来重复的"### 可选：不向用户回复"段（已被优先规则段覆盖）。
   - 新增三个示例：用户偏好静默落盘 `[NO_REPLY][NEED_BACKEND]`、用户长时间沉默后简单回应 → `[NO_REPLY][SEMI_ONLINE]`。

**未改动**：
- `core/routing.py`：已具备 `[NO_REPLY]` / `[SEMI_ONLINE]` 解析与互斥兜底（2026-04-27 已完成），本轮无需修改。
- `core/polling.py`：已读取 `should_reply`/`force_semi_online` 字段，本轮无需修改。
- 其他 Python 逻辑：无变更。

**验证**：
- `front_brain.jinja` system block 渲染：长度 6415，关键短语 `NO_REPLY` / `SEMI_ONLINE` / `该闭嘴` / `主动权` 全部命中。
- `front_brain_review.jinja` system block 渲染：长度 4810，关键短语 `NO_REPLY` / `上下文驱动` / `主动权` / `静默后台` 全部命中。
- 当前环境 pytest 未安装，但本轮纯文本/模板修改，不影响既有解析与流程。

**涉及文件**：
- `brain/templates/front_brain.jinja`
- `brain/templates/front_brain_review.jinja`

---

## 近期关键改动（截至 2026-04-27）

### 🌙 SEMI_ONLINE 自觉性提升 — 2026-04-27

**背景**：用户反馈 Nora 进入 `[SEMI_ONLINE]` 的自觉性不高。检查发现 prompt 里 SEMI_ONLINE 段位置靠后、夹在多条并列规则里，且示例存在自相矛盾（"晚安"示例没带 `[SEMI_ONLINE]` 与上方"必须加"规则冲突），同时审查阶段缺"任务完成默认收尾"的明确表述，路由层也没做 SEMI_ONLINE 与 NEED_FOLLOW 的互斥兜底。

**修复：**

1. **`brain/templates/front_brain.jinja`**
   - 在"你的职责"段新增第 3 条核心原则："不黏人，知道何时安静"——把 SEMI_ONLINE 提升为行为基调而非细则。
   - 重写 SEMI_ONLINE 段：开头先点明"默认就该收尾，不该追问"；分四类清单（必须用 / 倾向用 / 不要用 / 使用约束），明确写出"任务交给后脑后默认收尾""仅承接消散应收尾"等场景。
   - 修复矛盾示例："晚安" 示例补上 `[SEMI_ONLINE]`；新增"任务交给后脑后用户回承接语 → SEMI_ONLINE"示例。

2. **`brain/templates/front_brain_review.jinja`**
   - 重写"立即进入半在线"段，开头明示默认行为；强调"任务完成且无需追问 → 直接 `[TASK_DONE][SEMI_ONLINE]`"，避免任务完成后还触发 follow-up。
   - 明确与 `[NEED_FOLLOW]` 互斥、可与 `[NO_REPLY]` / `[TASK_DONE]` 组合。

3. **`core/routing.py`**
   - 在 `parse_front_brain_response` 与 `parse_front_brain_review` 中加互斥兜底：当 `force_semi_online=True` 时，强制把 `need_follow` 与 `has_continue` 压回 False，避免模型偶发同时输出冲突标记导致逻辑错乱。

**验证：**
- `python -m py_compile core/routing.py` 通过。
- 单元手测互斥兜底：`[NEED_FOLLOW][SEMI_ONLINE]` → need_follow=False, force_semi_online=True；`action: continue [SEMI_ONLINE]` → action 回退为 chat 而非 continue。
- `front_brain.jinja` / `front_brain_review.jinja` 渲染正常，关键短语 "不黏人，知道何时安静" / "默认就该收尾" / "[TASK_DONE][SEMI_ONLINE]" / "晚安哦 主人～好梦 (˘ω˘)♡ [SEMI_ONLINE]" 都在。

**涉及文件：**
- `brain/templates/front_brain.jinja`
- `brain/templates/front_brain_review.jinja`
- `core/routing.py`

### 🛡️ 后脑忙碌时的"重复任务"防护 — 2026-04-27

**背景**：用户反馈，第一条消息（如 "明天早上英语..."）触发后脑后，第二条简单承接（如 "嗯嗯"）在后脑仍忙碌时，被前脑误判为 `[NEED_BACKEND]`，并通过 `_auto_confirm_backend_enqueue` 入队，导致后脑重复执行同一组工具（read_file MEMORY.md 等），白白消耗 token 与时间。

**根因**：`task_instruction` 只通过运行时 `context` 传给后脑，**从未持久化进 `message_history`**。前脑下一轮重建上下文时只看得到清洗后的纯文本回复，看不到自己刚下达过的任务指示，也不知道后脑当前正在做什么——于是面对承接语时再次误标 `[NEED_BACKEND]`，且 `_auto_confirm_backend_enqueue` 的 prompt 既没传当前任务指示也没指引"重复对比"，默认 enqueue。

**修复（不引入硬编码黑名单，全部基于上下文驱动）：**

1. **持久化前脑路由元信息**（`core/message_handler.py`）
   - 保存前脑 assistant 消息时，把本轮 `task_instruction` + `needs_backend` 写入 `metadata.routing`。

2. **重建上下文时回填路由备注**（`core/front_brain.py`）
   - 新增 `_inject_routing_notes(history, db_context)`：对带 `metadata.routing` 的 assistant 消息，按位置对齐后在 content 末尾追加一行**仅模型可见**的内部备注（"已下达后脑任务指示: ..."）。
   - `_generate_front_chat_response` 与 `_front_brain_review` 都用上。
   - 这样下一轮前脑/审查能从上下文里看到"自己上一轮已经下过哪些后脑任务"。

3. **运行时后脑状态注入前脑 system prompt**（`core/front_brain.py`）
   - 新增 `_build_backend_status_block(chat_id)`：基于 `worker_status` + `task_queues` 实时构造一段块（仅在 busy 或 queue 非空时注入），告诉前脑"现在正在做 X、已排队 Y"，并提示"承接语不要再 NEED_BACKEND"。
   - 不写死任何承接词清单——靠上下文比对让模型自己判断。

4. **WorkerStatus 增加 `task_instruction` 字段**（`core/worker_status.py`）
   - `start(query, max_turns, task_instruction="")` 新增可选参数；`get_summary()` 与 `BackendTaskQueue.get_queue_summary()` 都展示任务指示。
   - `core/back_brain.py` 启动时把 `context["task_instruction"]` 透传进来。

5. **`_auto_confirm_backend_enqueue` 接受当前任务上下文**（`core/message_handler.py`）
   - 新增 `current_original_query` / `current_task_instruction` 参数（默认空，向后兼容现有测试），来自 `worker_status`。
   - 同时透传给 `queue_confirmation.jinja` → `auto_decide_user`。

6. **重写 auto_decide_user 模板**（`brain/templates/queue_confirmation.jinja`）
   - 新增字段：`【后脑当前正在处理的请求】` / `【后脑当前正在执行的任务指示】`。
   - 判定方法改为基于上下文比对的四步：① 对比新请求 vs 当前/排队任务（语义实质等价 → skip）② 承接 / 闲聊检测（强调"长度不是关键，有没有新可执行目标才是"）③ 新任务 → enqueue ④ 不确定 → enqueue。
   - **不再使用任何硬编码示例**，全部靠模型基于运行时上下文判断。

**验证：**
- `python -m py_compile` 通过。
- `queue_confirmation.jinja:auto_decide_user` 渲染验证：新增 `current_original_query` / `current_task_instruction` 字段正常输出。
- `routing.parse_front_brain_response` 兜底逻辑（NEED_BACKEND 但无 TASK_INSTRUCTION → 降级）测试通过。
- `tests/test_message_handler_ack.py` 现有用例使用旧签名调用（新参数有默认值），向后兼容。
- 本地 `pytest` / 部分依赖（google-generativeai 等）未安装，全量测试套件无法在当前环境跑。

**涉及文件：**
- `core/message_handler.py`（保存 metadata.routing；扩展 `_auto_confirm_backend_enqueue` 入参）
- `core/front_brain.py`（新增 `_inject_routing_notes` / `_build_backend_status_block`，并在生成与审查路径调用）
- `core/back_brain.py`（透传 task_instruction 给 worker_status）
- `core/worker_status.py`（新增 `task_instruction` 字段；queue summary 展示任务指示）
- `brain/templates/queue_confirmation.jinja`（重写 auto_decide_user 模板，基于上下文比对）

> ⚠️ 历史回顾：本任务最初尝试用硬编码正则（`_LIGHTWEIGHT_ACK_PATTERN`）做承接语短路，被指出违背"上下文驱动"原则后已移除。现行方案完全不依赖任何关键词清单。

### ♻️ 消息压缩失败持久化重试队列（方案 B）— 2026-04-27

- 为消息级压缩、归档总结、对话段摘要、段压缩刷新新增统一的**持久化重试队列**：`compression_retry_queue`。
- 压缩/总结请求失败时，不再只打日志等待“下次碰巧触发”，而是将失败任务落库，记录：
   - `platform` / `chat_id`
   - `task_type`（`compress` / `archive` / `session_summary` / `context_refresh`）
   - `payload`
   - `attempts`
   - `next_retry_at`
   - `last_error`
   - `status`（`pending` / `dead`）
- `MessageHistory` 新增后台补偿 worker：
   - 启动时在事件循环中自动启动
   - 周期扫描到期任务并补跑
   - 成功后自动出队
   - 达到最大尝试次数后标记为 `dead`，避免无限刷日志/刷 API
- `ContextCompressor` 已接入统一回调：段压缩刷新失败也会进入同一套重试队列，而不是静默丢失。
- 当前默认策略：基础延迟 60 秒，指数退避，最大 8 次，扫描间隔 300 秒。

**涉及文件：**
- `memory/message_history.py`
- `memory/context_store.py`
- `core/controller.py`
- `tests/test_message_history_retry_queue.py`
- `docs/architecture/message_history.md`
- `docs/architecture/message-compression.md`

**当前状态 / 注意：**
- 已完成代码接入与静态检查。
- 本地终端环境下 `pytest` 命令不可用（未识别），所以新增测试暂未在当前会话内实际执行通过。

### 🎛️ Nora 偏好设置 + Follow-up / Proactive 概率控制 — 2026-04-26

- 新增独立 `nora_preferences` 配置块，用于集中管理 Nora 的行为与风格偏好：
   - 行为层：`nora_followup_probability`、`proactive_message_probability`
   - 风格层：`split_reply_probability`、`short_reply_preference`、`verbosity_preference`、`pause_between_splits_seconds`
   - 语气层：`warmth_level`、`playfulness_level`、`emotional_expressiveness`、`assertiveness_level`
- 新增 `brain/templates/user_preferences.jinja`，由 `brain/prompts.py` 单独注入到 prompt 链路，不再把偏好描述硬塞进 `system.jinja`。
- 对话延续（follow-up）逻辑改为：
   - 先照常检测 `FOLLOWUP / WAIT / END`
   - 若检测结果为 `FOLLOWUP`，再按 `nora_followup_probability` 决定是否真正发送
   - 当概率未命中时，不继续维持强 follow-up 候选状态，而是按 `WAIT` 处理；连续若干次未命中后直接 `END`
- 主动消息计划新增 `message_kind`：
   - `explicit`：用户明确要求的定时提醒/固定督促
   - `autonomous`：Nora 自主决定的陪伴型主动消息
- `proactive_message_probability` 仅作用于 `autonomous`，不会随机跳过 `explicit` 或 `alarm`。
- Telegram 新增 `/nora_prefs` 指令，使用 inline keyboard 直接调整上述偏好，并实时写回 `config.yml`。
- `/schedule_today` 现在会展示计划项类型（明确提醒 / 自主消息）。

**涉及文件：**
- `config.py`
- `config.example.yml`
- `brain/prompts.py`
- `brain/templates/user_preferences.jinja`
- `brain/templates/system.jinja`
- `brain/templates/schedule.jinja`
- `core/scheduler.py`
- `core/scheduler_mixin.py`
- `core/message_handler.py`
- `adapters/telegram/main.py`
- `tests/test_nora_preferences.py`
- `tests/test_nora_prefs_telegram.py`

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

4) `brain/tools.py` / `view_media`
- `view_media` 增加 `return_image: bool`。
- `return_image=false`：返回元数据。
- `return_image=true`：额外返回 `MediaTag: [image: absolute_path]`，用于直接发送或后续多模态分析。

5) `view_media(return_image=true)` 后自动切 image 模型
- Controller 会解析工具输出中的图片标签并在下一轮注入 `multimodal_images`。
- 下一轮自动切换到 `image_llm` 进行图片分析。

6) Telegram 下载路径迁移
- `adapters/telegram/main.py` 收到图片/文档/贴纸时改为落盘到：`workspace/data/telegram/`。
- 传入上下文的路径改为 workspace 相对路径（如 `data/telegram/xxx.jpg`）。

7) Prompt / 文档同步
- 新增模板：`brain/templates/image_tags.jinja`（关键词标签策略）。
- `system.jinja` 补充 `view_media` 参数约束（禁止空参数调用、优先 keyword/image_id/time、可启用 `return_image=true`）。
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
     - 群聊暂时仅响应 @机器人；单纯回复机器人不触发。已触发消息仍会提取 reply 内容；私聊始终响应。
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
- `brain/tools.py`：工具注册与实现（含 `view_media(return_image)`）。
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
- Telegram 适配：确认机器人 username 可获取；群聊暂时仅响应 @，单纯回复不触发；私聊已修复未响应问题。
- MessageAggregator 接口已改为 `add_message(chat_id, text, context)`，下游回调签名 `on_complete(context)`。
- 身份文件主路径已迁移到 workspace（`workspace/SOUL.md`、`workspace/USER.md`、`workspace/data/memory/MEMORY.md`），旧路径仅作回退兼容。
- 每日记忆（`workspace/data/memory/YYYY-MM-DD.md`）默认不自动注入；如业务需要可在 prompt 组装层手动恢复。
- 聊天记录默认 DB 路径为 `workspace/data/memory/message_history.db`；若配置了 `memory.message_history.db_path` 则按配置优先。
- Telegram 媒体下载路径为 `workspace/data/telegram/`；多模态解析支持 `data/...` 路径映射。
- `view_media` 若用于“找回并继续看图”，应传 `return_image=true`；否则只返回元数据。
- CLI 清理 RAG 数据是高危操作：会删除所有 collections，且必须二次确认（`DELETE ALL`）。

## 快速检查清单
- `config.cost_tracking.enabled` 是否开启。
- `config.cost_tracking.custom_prices` 是否覆盖预期（无则成本为 0）。
- CLI 模型列表价格展示取决于自定义价格；缺价会提示手输。
- 日志中是否能看到 `[Cost] provider/model: tokens = $cost`。
- 后端忙碌时，新消息是否被 fast_llm 正确判定并回复（stop/change/queue）。
- Telegram 私聊是否响应，群聊是否仅在 @ 机器人时响应；已触发消息的 reply 是否正常提取；消息是否带用户名；RAG/历史是否按 storage_id 隔离。
- 生成阶段开始时 Telegram 是否立即出现 typing（而不是等首个 chunk）。
- workspace 首次运行时是否自动复制 `SOUL.md`/`USER.md`/`MEMORY.md`。

---

### 🔄 私聊与群聊在线状态拆分 — 2026-07-26

将全局 `AIPresence` 从"私聊在线状态 + 系统忙碌状态"混用，拆分为三层独立状态：

**架构变更：**
- **全局 `AIPresence`**（`core/scheduler.py`）：重定义为系统级忙碌标志（`is_generating` / `is_backend_busy`），不再表示任何私聊的在线状态。
- **私聊 `PrivatePresenceStore`**（`core/private_presence_store.py`，新建）：按 `memory_scope_id` 存储私聊的 `ONLINE` / `SEMI_ONLINE` 状态，持久化到 `private_presence_state.json`，重启后过期（6 小时）自动重置。
- **群聊 `GroupPresenceStore`**（`core/group_presence_store.py`）：保持不变，按群 `runtime_key` 独立存储。

**关键改动：**
- `core/message_handler.py:35` `_update_message_entry_presence`：私聊消息入口改为写 `PrivatePresenceStore`（不再写全局 `AIPresence`）。
- `core/scheduler_mixin.py:244` `_update_scheduler_state(busy=True)`：后脑启动不再无条件设全局 `AIPresence.ONLINE`；私聊后脑启动写 `PrivatePresenceStore.ONLINE`，群聊后脑启动不写私聊状态。
- `core/scheduler_mixin.py:552` `_apply_presence_markers`：私聊分支补全 `force_online`（之前被忽略），路由到 `PrivatePresenceStore`。
- `core/scheduler_mixin.py:582` `_transition_to_semi_online`：改为写 `PrivatePresenceStore.SEMI_ONLINE`（不再写全局 `AIPresence`）。
- `core/scheduler_mixin.py:452` `_followup_loop` 退出守卫：从 `get_ai_presence() != ONLINE` 改为按当前 runtime 的 `chat_type` 路由查状态（私聊查 `PrivatePresenceStore`，群聊查 `GroupPresence`）。
- `core/scheduler.py:139` `should_skip_proactive_for_target`：新增函数，按目标用户的 `memory_scope_id` 查询私聊在线状态决策是否跳过主动消息。
- `core/scheduler.py:596` `_on_event_trigger`：主动消息触发时解析目标用户的 `memory_scope_id`，使用 `should_skip_proactive_for_target` 判断是否跳过。

**日志改进：**
- 全局状态日志改为 `global=semi_online`（不再叫 `presence=online`）。
- 消息入口状态日志增加 `private=online` / `group=online` 区分。
- 启动过期清理日志：`启动过期清理: N 个私聊 ONLINE 记录已重置为 SEMI_ONLINE`。

**测试改写：**
- `tests/test_presence_entry.py:22` `test_private_message_entry_writes_private_presence`：断言 `PrivatePresenceStore` 而非全局 `AIPresence`。
- `tests/test_scope_runtime_coordination.py:94` `test_transition_waits_until_whole_scope_is_idle`：断言 `PrivatePresenceStore` 按 scope 共享。
- `tests/test_scope_runtime_coordination.py:128` `test_backend_start_writes_private_presence`：断言私聊后脑启动写 `PrivatePresenceStore`。

**新增测试：**
- `tests/test_private_presence_store.py`：覆盖默认值、set/get 往返、持久化、冗余 set 无日志、all() 查询、启动过期清理。

**验证**：
- `py_compile core/private_presence_store.py core/scheduler.py core/message_handler.py core/scheduler_mixin.py core/controller.py` 全部通过。
- `pytest tests/test_presence_entry.py tests/test_scope_runtime_coordination.py tests/test_private_presence_store.py -v` → 16 passed。
- 全量 `pytest tests/` → 419 passed（已知环境问题：CLI 测试需要 Windows console、`test_cost_tracker` 文件锁定、`test_message_history_retry_queue` 需要 pytest-asyncio、`test_timezone` 段落逻辑问题）。

---

## 交接
- 记录人：GitHub Copilot
- 日期：2026-03-08
