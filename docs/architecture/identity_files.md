# 身份与记忆文件一览（openclaw 启发）

> 本项目的身份/记忆文件设计受到 openclaw 启发，在此集中列出各文件的定位、写入原则与权限边界，便于维护。

## 文件总览

| 文件 | 作用 | 写入/读取规则 | 备注 |
| --- | --- | --- | --- |
| `SOUL.md` | AI 的人设、语气、边界 | 仅当用户明确要求调整人设/语气时更新，并告知用户；前脑发现变更需求需 `[NEED_BACKEND]` 交后脑写入 | 禁止写入用户信息 |
| `appearance/APPEARANCE.md` | AI 的**外貌形象**（体征、发型、常穿、固定特征） | 形象设定变化时更新并告知用户；前脑标记 `[NEED_BACKEND]` 交后脑写入 | 只写外貌；性格语气仍归 `SOUL.md`，画风归 `STYLE.md`。它是所有生图的文字锚 |
| `appearance/STYLE.md` | **出图风格偏好**（写实/动漫、质感、镜头、光线、比例、排除项） | 仅用户明确要改画风时；AI 不自主改 | **不注入 system prompt**，只在两条生图链路里读（`read_style_text()`）。管"画成什么样"，不管"长什么样" |
| `appearance/refs/` | 形象参考图 + `manifest.json` | **只能**用 `generate_appearance_reference` 工具生成/覆盖 | 通用文件工具读写被 `_is_path_safe` 目录级拦截；覆盖会改变形象，须先征得用户同意 |
| `appearance/generated/YYYY-MM-DD/` | `[DRAW:]` 日常生图产物 | 由生图链路自动落盘，元数据入 Mongo `appearance_images` | 只记 request / draw_prompt / 路径 / 时间；不入 Qdrant |
| `USER.md` | **主人**画像：偏好、背景、联系方式、习惯 | 任何对话中听到的**主人**信息可主动更新（无需用户说“记一下”）；前脑标记 `[NEED_BACKEND]` 后脑写入 | 禁止写 AI 人设/日程；**只写主人，不写访客** |
| `SCHEDULE.md` | 作息与日程安排，驱动主动消息调度 | 用户提到作息/日程变化或与记录不符时提醒并更新；前脑标记 `[NEED_BACKEND]` | 仅写作息/日程，禁止其他内容 |
| `data/memory/MEMORY.md` | 长期记忆：决策、约定、持久事实、经验 | 重要信息随时记录；前脑标记 `[NEED_BACKEND]` | 保持精炼，定期整理 |
| `data/memory/YYYY-MM-DD.md` | 每日记忆（不自动注入） | 需要时显式读取/写入，用于当天事件日志 | 可用于整理后再汇总入 MEMORY |
| `data/memory/people/<昵称>.md` | **其他人**（非主人/访客）的自主人物笔记 | Nora 想记住某位访客/熟人时按昵称建文件，记显示名、平台 id、印象等 | 她的自主记忆区；与 USER.md 互不混淆 |
| `CUSTOM.md` | 用户维护的全局自定义指令 | **仅用户**编辑；系统自动注入提示；AI 禁止用文件/命令工具读取或修改 | AI 只需遵守内容 |
| `SECRET.md` | AI 私人加密记事本（想法/偏见/反思） | 仅用专用工具 `read_secret_vault` / `write_secret_vault`；首次写入生成 `workspace/data/secret.key`（Fernet）；前脑需标记 `[NEED_BACKEND]` 路由后脑写入 | 回复中不得泄露内容；通用读写/命令工具均被阻止 |

## 使用与边界
- 前脑不得直接读写文件；只做路由，需写入时在回复末尾标记 `[NEED_BACKEND]` 交由后脑执行。
- 敏感文件（`SECRET.md`/`secret.key`/`CUSTOM.md`/`.env`/`config.yml` 等）被通用读写/命令工具阻止，SECRET/CUSTOM 仅走专用或人工路径。
- CUSTOM 由用户自定义，系统自动注入全局提示；AI 不得修改。
- SECRET 用于 AI 自我记录，不对用户暴露；写入默认追加且可附时间戳。

## 主人 vs 访客（跨平台接力后）
- `USER.md` 描述的是**主人**，当前说话人不一定是主人。每轮上下文注入 `<current_speaker>` 块
  （`actor_display_name` + `is_owner`），由 `brain/prompts.py::_build_current_speaker_block` 构造。
- **主人 + 私聊**：默认场景，不注入额外块。**主人 + 群聊**：提示注意场合得体性。
  **访客（非主人）**：提示别把 USER.md 当对方，对访客有边界。
- 访客笔记写 `data/memory/people/<昵称>.md`，不写进 USER.md。详见
  [跨平台接力与五层身份模型](./cross-platform-relay.md)。

## 形象系统（APPEARANCE + 生图）

文字在前、图在后：`APPEARANCE.md` 是唯一的形象依据，参考图依据它生成，日常生图再以参考图锚定一致性。

两条生图路径，共用 `brain/appearance.py` 的原语（读 md / 载参考图 / 落盘 / 维护 manifest）：

- **参考图（后脑工具）**：`generate_appearance_reference(requirement, view, usage, replace, source_images)` → 直接用 `APPEARANCE.md` + 已有参考图作锚生成，存 `refs/{view}.png`。不经 `draw_desc`——requirement 已是显式描述，再套一层改写只会引入漂移。`STYLE.md` 在这条路上**只取渲染风格那一层**（写实/插画、质感），明确忽略拍摄方式与比例——参考图要中性。用户发图说"你就长这样"时，把图的本地路径传 `source_images`：来源图优先占配额、排在附图最前，提示词写"与文字描述冲突以图为准"。
- **日常生图（前脑标记）**：`[DRAW:要求]` → `draw_desc` 读 APPEARANCE.md + STYLE.md + 要求 + 当前消息段 + 当前时间 + SCHEDULE.md + SOUL.md，产出英文提示词并**自己挑**参考图 → `draw` 出图 → 落盘 + 入库 + 追发。文字先发、图后到；单轮只一张；失败只记日志不追发安慰文本。

三份输入职责不重叠：`APPEARANCE.md` 管长什么样，`STYLE.md` 管画成什么样，`[DRAW:要求]` 只管画什么（视角/场景/动作/表情）。前脑被明确禁止把外貌和画风写进要求——重复写会与权威文件打架，也让 `draw_desc` 分不清谁服从谁。

两个模型别名 `draw`（文生图）/ `draw_desc`（文本）都配上才启用；否则 `[DRAW:]` 说明不注入前脑提示、参考图工具不注册。OpenAI 与 Gemini 端点都支持（`BaseLLM.generate_image`）。

## 关联实现
- Prompt 注入：`brain/prompts.py` 加载 SOUL/APPEARANCE/USER/MEMORY/SCHEDULE 和 CUSTOM（只读）；`<appearance>` 紧跟 `<soul>` 注入；前脑模板支持 SCHEDULE/CUSTOM 块；路由规则在 `brain/templates/front_brain.jinja`。
- 专用工具：`read_secret_vault` / `write_secret_vault`、`generate_appearance_reference`（`brain/tools.py`）。
- 生图链路：`core/appearance_gen.py`（AppearanceGenMixin）、`brain/appearance.py`、`brain/templates/draw_desc.jinja`、`memory/appearance_store.py`；标记解析在 `core/routing.py` 的**两个**前脑解析器里。
- 敏感防护：`brain/tools.py` 在 `_is_path_safe` / `write_file` / `exec_command` 层阻断对敏感文件的通用访问；`_is_path_safe` 还有唯一一条目录级规则，拦 `appearance/refs/`。

## 借鉴
- 部分文件的设计思路受到 openclaw 启发，并结合本项目的双脑架构与安全隔离需求进行调整。
