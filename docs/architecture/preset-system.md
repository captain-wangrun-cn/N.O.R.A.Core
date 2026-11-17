# 全局预设系统（Preset System）

预设是用户维护的提示词资产，不是新的人格或独立会话。文件位于 `workspace/presets/*.yml` / `*.yaml`；框架只创建空目录，不内置任何预设，也不增加拒绝检测、自动重发或历史清洗。

## 激活与命令

- `/preset` 或 `/preset list`：列出可用预设与全局状态。
- `/preset use <文件名>`：激活；扩展名可省略。
- `/preset off` 或 `/preset clear`：停用，恢复默认注入行为。
- `/preset reload`：重新读取磁盘文件。
- CLI：`python cli.py preset [list | use <文件名> | off | clear | reload]`。

聊天命令在 Core 使用本次入站身份验证主人权限，不依赖某个群的最后一位发送者；QQ 适配层也验权限。主人解析器未注册时沿用既有兼容降级。命令在多模态解析、presence 更新、followup 取消和生成抢占之前返回，不入库、不打断当前请求。

一个工作区全局共用激活状态，各平台、私聊、群聊的**后续匹配作用域请求**生效；已经开始的请求不重启。

优先级：`presets/.active` → `config.yml` 的 `preset.active` → `none`。明确的 `none` 持久化后不会因重启重新激活配置默认值。无效/不存在的激活文件不注入，但保留所选名称，修复文件并 reload 后恢复。标记原子写入失败不会假报切换成功。

`.active` 每次取预设读取，使独立 CLI 的切换也能被运行进程看到；显式 reload 写 `.reload` 通知其它进程在下次请求重读文件。不监控普通文件修改：手工修改文件后需要 reload。

## 文件规格

| 字段 | 默认与约束 |
|---|---|
| `name` | 展示名，缺省为文件名 |
| `description` | 用途说明，默认空 |
| `scopes` | 缺省 `[forebrain, hindbrain]`；空列表不注入；非法作用域拒绝文件 |
| `head_prompt` | 空则不注入；非空时置于人设/SOUL 之前 |
| `depth_injection.depth` | 默认 1，必须为非负整数 |
| `depth_injection.role` | 默认 `system`，允许 `system` / `user` |
| `depth_injection.content` | 深度指令正文，空则不注入 |
| `parameters.temperature` | 可选，0～2 的有限数字；0 是有效覆盖 |
| `parameters.top_p` | 可选，0～1 的有限数字 |

不支持顶层 `enabled`。坏 YAML、错误字段类型、非法采样数值和同 stem 的 `.yml/.yaml` 歧义均跳过并记录警告；目录外符号链接、路径穿越和 `.active` 等隐藏名称不可用。只从目录中加载文件，命令不会创建或覆盖预设资产。

## 作用域边界

- `forebrain`：日常对话、前脑审查转述、主动消息、追话及收尾。
- `hindbrain`：后脑常规执行与续跑、既有图片标签补齐轮。
- 不注入：无人设媒体分析、表情包描述、生图提示词、摘要/压缩/归档、JSON 日程与群监听判定、快速打断/入队意图判定、RTC 独立通道。

作用域按**请求用途显式 opt-in**，不根据模型别名或 provider 猜测。同一模型同时用于日常对话与 JSON 判定也不会误注入。强势预设仍可能干扰路由标记、工具使用、标签结构或群聊得体性，框架不承诺所有自定义指令都兼容。

## Head 与 Depth 的真实位置

Head 在统一 wrapper 置顶，因此前脑自己渲染模板的两条路径也覆盖；`get_system_prompt(preset_scope=...)` 提供独立组装入口，默认不注入。`system.jinja` 的条件前缀在未启用时保持原始开头，无额外占位块。

Depth 按完整逻辑请求中倒数的 **user 对话消息**计数（当前 user 也算）：

- `depth: 1`：当前 user 消息之前；无当前 user 时为最后一条历史 user 之前。
- `depth: 2`：倒数第二条 user 之前，依此类推。
- 深度超过 user 数量：放历史开头；无历史也照常创建临时指令。
- `depth: 0`：通过 `request_tail` 在实际当前 user 之后注入；无当前 user 时在历史末尾。

不以 list 长度近似轮数，工具调用/结果不计为 user 对话，也不拆开工具配对。只创建内存副本，不修改 session、SQLite 消息或压缩记忆。`system` 深度角色只有 OpenAI 系能原生保留；Gemini/Anthropic 转为 `user` 指令，绝不误当 assistant/model 发言。

## 参数覆盖与提供方限制

预设参数覆盖当次请求的模型配置，不修改共享 provider 实例，跨平台并发不串温度。取消预设后自动回到原有配置。

- OpenAI / OpenRouter：Chat Completions 和 Responses 顶层 `temperature` / `top_p`。
- Gemini：SDK `temperature` / `top_p`，REST `temperature` / `topP`；流式与非流式、媒体与文本通道均传递预设显式参数。非流式未传预设参数时保留原来不发送配置温度的行为，不借本次改造改变默认链路。
- Anthropic：思考态不下发这两个采样参数；普通态 temperature 上限裁剪为 1，并采用二选一：有 temperature 时保留它、忽略 top_p 并警告，仅 top_p 时正常传递。
- Civitai：仅生图，不支持对话预设。

模型/网关不接受采样字段时仍受各 provider 原有错误与降级契约约束；预设不增加内容拒绝重试，不保证每个模型支持全部采样参数。

## 代码与验证

- `core/preset_manager.py`：资产、不可变数据、全局持久化与跨进程重载。
- `core/preset_injection.py`：无副作用的请求拼装。
- `core/preset_commands.py`：CLI/聊天共用操作。
- `core/controller.py`：实际请求边界与作用域选择。
- `tests/test_presets.py`、`tests/test_preset_integration.py`、`tests/test_preset_provider_parameters.py`：临时目录和假 SDK/REST 验证，不需要真实预设或凭据。
