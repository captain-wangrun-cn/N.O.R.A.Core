# 形象系统与生图链路（Appearance System）

> Nora 有一份自己的外观设定 + 一组参考图，前脑用 `[DRAW:...]` 标记就能让她当场"拍一张"发给用户。
> 设计目标：**形象长期一致**（不是每次随机生成一个陌生人），且**不占用后脑工具循环**。

---

## 1. 组成

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 文字设定 | `workspace/appearance/APPEARANCE.md` | 身份文件之一，自动注入系统提示的 `<appearance>` 块。Nora 自主维护 |
| 参考图 | `workspace/appearance/refs/<view>.png` | 视觉锚点。**只能**通过 `generate_appearance_reference` 工具管理 |
| 参考图索引 | `workspace/appearance/refs/manifest.json` | 每张参考图的 `view` / `usage` / `updated_at` |
| 生成图 | `workspace/appearance/generated/YYYY-MM-DD/draw_xxxxxxxx.png` | 按日期分目录落盘 |
| 生成记录 | MongoDB `appearance_images` collection | 只记 `image_id` / `file_path` / `request` / `draw_prompt` / `created_at` |

两个新模型别名（都可选，缺任一则整个生图能力不注册）：

- `draw` — **文生图**模型，真正出图。
- `draw_desc` — 普通文本模型，把"拍张自拍"这种要求翻译成完整英文提示词，并挑选本次要带哪些参考图。

---

## 2. 两条生图路径

| | 前脑 `[DRAW:...]` | 后脑 `generate_appearance_reference` |
| --- | --- | --- |
| 触发者 | 前脑标记，用户对话中 | 后脑自己决定调工具 |
| 提示词来源 | `draw_desc` 模型改写 | 工具的 `requirement` 参数直接用（**不过 draw_desc**） |
| 参考图 | `draw_desc` 从 manifest 里挑 | `source_images`（用户给的来源图）优先，剩余配额补已有参考图作锚 |
| 落盘位置 | `generated/<日期>/` | `refs/<view>.png` |
| 是否入库 | 是（`appearance_images`） | 否（manifest 即索引） |
| 发送方式 | 系统自动追发 `[image: path]` | 工具返回 MediaTag 文本，由后脑决定是否带进回复 |

参考图工具**刻意不走 `draw_desc`**：`requirement` 已经是后脑自己写的明确描述，`APPEARANCE.md`
也是结构化文本，再插一层提示词改写只会引入漂移。

### 鸡生蛋问题

第一张参考图从哪来？两条路：

1. **纯文字起步** —— `generate_appearance_reference` 只依赖 `APPEARANCE.md`，先写文字设定，
   再让工具生成 `face`，之后生成 `side` / `front` 时会把已有参考图作为锚传给 `draw`，
   保证各视角是同一个人。
2. **照着用户给的图起步** —— 用户发一张图说"你就长这样"，后脑先用 `view_media` 拿到本地路径，
   再把路径传给工具的 `source_images` 参数。

### `source_images`：让参考图照着一张现成的图长

`source_images` 收逗号分隔的本地图片路径（png/jpg/jpeg/webp），语义与已有参考图**不同**，
提示词里也分开点名，否则模型分不清哪张该照抄、哪张只是保持一致：

- **来源图（Source images）** —— 排在附图列表**前面**，提示词要求"跟它走，与文字描述冲突时以图为准"。
- **已有参考图（Existing reference images）** —— 排在**后面**，提示词要求"保持同一个人，只改视角"。

配额仍是 `MAX_REFERENCE_IMAGES = 3`，**来源图优先占用**，剩下的名额才补已有参考图；
来源图占满 3 张时不再带锚（用户的意图是"重新定形象"，旧锚反而会拉回旧样子）。

路径先过 `_resolve_workspace_path()` 再过 `_is_path_safe()`——所以 `appearance/refs/` 下的图
不能当来源图传（想复用已有参考图，工具本来就自动带）。读不到就**直接报错、不生图**，
不静默降级：用户明确给了图却按文字凭空画，比不出图更糟。

改了形象之后记得同步改 `APPEARANCE.md` 的文字描述，否则文字和图会打架——
提示词（`system.jinja` / `front_brain.jinja`）里都写了这一条。

---

## 3. `[DRAW:...]` 标记

格式：`[DRAW:要求]`，要求写人话，例如 `[DRAW:自拍，书桌前，笑着]`。

- **与 `[NEED_BACKEND]` 完全独立**，不需要也不应该同时给出——生图不进后脑工具循环。
- **文字先发、图片后到**：标记所在的回复文本正常立即发送，生图在后台跑完再追发一张图。
- **单张**：一轮只出一张图。同一 `runtime_key` 若已有生图在跑，新的会**取消旧的**
  （与 `_followup_timers` 语义一致），避免用户连发两条刷图。
- **主动消息轮不生图**：`_send_proactive_message` 里出现该标记只记一条 INFO 就剥掉。
- 失败**直接不发图**，不追发安慰或报错文本。

### 双解析器陷阱 ⚠️

`core/routing.py` 有**两个**近乎相同的前脑解析器：

- `parse_front_brain_response()` — 主链路，**提取** `draw_request` 并剥离标记。
- `parse_front_brain_review()` — 轮询审查链路，**只剥离**、不触发（审查轮不该再出图）。

只改一个 → 另一条链路里 `[DRAW:...]` 会作为**字面文本发给用户**。
`sanitize_adapter_output_text()` 也加了兜底剥离。新增任何标记都必须同时改这三处。

---

## 4. 调用链

```
前脑输出 "…等我拍一张！[DRAW:自拍，书桌前]"
  ↓ parse_front_brain_response() 提取 draw_request、剥离标记
  ↓ core/message_handler.py 发送文字回复（拿到 send_target）
  ↓ start_appearance_image_task(draw_key, request, identity)   ← 不 await
  ↓ [后台] _resolve_draw_prompt()
  │    draw_desc(system=APPEARANCE.md + SOUL + manifest,
  │              user=要求 + 当前时间 + SCHEDULE + 当前消息段)
  │    → [DRAW_PROMPT]英文提示词[/DRAW_PROMPT] + [DRAW_REFS]face.png[/DRAW_REFS]
  ↓ [后台] load_reference_images() → draw.generate_image(prompt, refs)
  ↓ [后台] 落盘 generated/<日期>/ → AppearanceStore.save_generated()
  ↓ [后台] _send_platform_message(key, "[image: abs_path]")
```

**插入点为什么在那一行**（`core/message_handler.py`，send 块之后、`_apply_presence_markers` 之前）：
放前面拿不到 `send_target`（图会追发到错误的会话）；放后面会被 `presence_ended` 的提前
`return` 跳过（用户说晚安那轮就永远不出图）。

---

## 5. 安全边界

`appearance/refs/` 是 `_is_path_safe()` 里**第一条目录级规则**（此前只有文件名黑名单）：
通用文件工具（`read_file` / `write_file` / `edit_file` / `search`）一律拒绝访问该目录。

原因：参考图是形象的锚，一旦被随手覆盖，之后所有生成图都会变成另一个人。改动必须走
`generate_appearance_reference`，该工具对已存在的 `view` 默认**拒绝覆盖**，需显式
`replace=true`，且提示词要求先征得用户同意。

`APPEARANCE.md` 本身不设限——它就该由 Nora 用普通文件工具维护。

---

## 6. 关联实现

| 文件 | 职责 |
| --- | --- |
| `brain/appearance.py` | 共享原语：读 `APPEARANCE.md`、manifest 读写、参考图加载/落盘、任意本地图加载（`load_image_file`）、生成图落盘、时区 |
| `brain/prompts.py` | 路径常量、workspace 初始化、`<appearance>` 注入（紧跟 `<soul>`） |
| `brain/templates/draw_desc.jinja` | `draw_desc` 的 system / user prompt（无人设） |
| `brain/interface.py` | `BaseLLM.generate_image()`，默认 `NotImplementedError` |
| `brain/providers/gemini.py` / `openai.py` | 各自实现 `generate_image`（Gemini inline_data / OpenAI images.generate·edit 或 chat+modalities，见 `llm.draw_api`） |
| `brain/tools.py` | `generate_appearance_reference` + `_is_path_safe` 目录规则 |
| `core/routing.py` | `_DRAW_PATTERN`、双解析器、sanitize 兜底 |
| `core/appearance_gen.py` | `AppearanceGenMixin`：任务管理 + `draw_desc` 调用 + 生图追发 |
| `core/message_handler.py` | `[DRAW:]` 触发点 |
| `memory/appearance_store.py` | `AppearanceStore`，MongoDB 不可用时整体 no-op |

`draw_desc` 那一轮**不带对话历史**，理由同 `media_analysis.jinja`：历史里满是 Nora 的
说话方式，会把这一轮拽回对话口吻，输出寒暄而不是提示词。
