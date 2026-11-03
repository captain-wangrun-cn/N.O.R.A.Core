# N.O.R.A.Core

N.O.R.A.Core 是一个以个人陪伴为主、兼顾一点点效率提升的多平台 AI 助手框架。虽然不敢说完美，但在长期记忆和拟人化交互方面做了一些微小的尝试。

我想做的不只是一个“问一句、答一句”的机器人：她能一边干活一边陪你聊天，记得你的习惯和之前聊过的事，也能在合适的时候主动来找你。下面这些功能，都是围绕这个想法一点点加出来的。

> \[!CAUTION]
> 本项目为 \*\*VibeCoding\*\* 项目（氛围编程）。
>
> \*\*99% 的代码为 AI 生成\*\*，本人仅提供架构设计与部分指导。
>
> 如果阅读本项目的源码给你带来了包括 \*心跳加速\*、\*高血压\* 等不适，本人不负任何责任。
>
> \*\*本项目为个人项目，本来只是个人用，代码内可能包括但不限于各种离谱设计、史山代码、不统一的匪夷所思的 commit 信息等。甚至在某些文档里写了自己的一些偏见。但是请各位大佬手下留情。\*\*
>
> 个人的详细声明可以阅读 \[架构文档索引](docs/architecture/README.md)。

## 必读与架构设计

* **日常怎么用**：[NORA 使用指南（实战版）](docs/AI_CHAT_GUIDE.md)
* **为什么这样设计**：[架构文档索引](docs/architecture/README.md)
* **如何配置与维护**：[CLI 使用指南](docs/CLI_USAGE.md)

## NORA 特点

* 一边干活一边聊天，不用等任务做完才能说话。
* 长期记忆。消息段机制，更好的记忆与互动
* 可以连续发多条消息，不必问一句、等一句。
* 任务途中可以随时叫停、补充要求或换个任务。
* 聊到一半突然不说话，她会看情况追问，像朋友一样。
* 记住你的喜好、习惯、作息、重要约定，以及你给她的设定。
* 每天凌晨自动总结前一天的对话，方便以后回顾。
* 根据你的作息主动来问候，也可以定闹钟提醒你。
* 以前发过的图片可以再找出来看，不是只在发送那一轮有效。
* 能裁剪图片看细节，也支持视频理解。
* 有自己的虚拟形象，可以给你发自拍或“此刻在做什么”的照片。
* 可以发语音条，也可以在 Telegram 里带着人设和记忆接电话。
* 技能可以下载、自己写，或者让 Nora 编写，逐步扩展能做的事。
* 收到邮件等外部事件时，可以判断是否需要提醒你。
* 支持 Telegram、QQ 官方机器人、OneBot v11，同时在线、跨端接力。
* 用多份 Markdown 文件分别管理人设、用户档案、日程和记忆。
* 词库可扩展，不仅理解口语词、缩写和内部梗，也能在聊天中使用。

## 项目特点

### 双脑协同与可打断交互

* **前脑 + 后脑**：前脑负责日常对话、即时回复和路由判断；后脑负责深度推理、工具、技能与检索。两者通过异步任务协作，让“正在工作”和“可以聊天”不再互斥。
* **前后脑轮询审查**：后脑把工作结果交给前脑审查、转述；如果还没满足要求，前脑可以先告诉你再等一会，让后脑继续工作，而不是把第一份结果直接当成最终答案。
* **消息聚合与抢占**：连续消息先聚合；任务执行中可以识别停止、改需求和排队意图。需要新增后脑任务时，会结合当前进度与队列进行确认。
* **自然的消息节奏**：支持分段发送、段间停顿和对话追问；对话自然结束后进入半在线（`SEMI\_ONLINE`），封闭当前消息段并触发摘要，而不是一直追着用户说话。

### 多 Markdown 文件与长期记忆

不把所有设定都塞进一条固定提示词。人设、用户信息、作息和记忆分开保存，能查看、校正，也能在允许的范围内通过对话让 Nora 更新。

下列路径以**工作区**为基准，仓库根目录的同名文件主要用于首次初始化：

|文件|用途|
|-|-|
|`SOUL.md`|Nora 的人设、性格、语气和边界|
|`USER.md`|主人的长期偏好、背景和习惯，不混入访客信息|
|`SCHEDULE.md`|用户作息、固定日程和提醒安排，也是主动消息计划的依据|
|`data/memory/MEMORY.md`|跨会话仍有用的事实、重要约定、决策和经验|
|`data/memory/YYYY-MM-DD.md`|每日对话总结与当天记忆，后续按需回查|
|`data/memory/people/<昵称>.md`|Nora 对其他熟人、访客的人物笔记，与主人档案分开|
|`appearance/APPEARANCE.md`|自身外貌、体型、五官、穿着等形象设定|
|`appearance/STYLE.md`|生图画风与拍摄偏好，只用于生图，不作为聊天人设|
|`CUSTOM.md`|用户维护的自定义指令，可选择注入哪些模型角色；Nora 不自行修改|
|`SECRET.md`|Nora 的私人加密记事本，通过专用工具读写，不是普通明文记忆文件|

* **记忆不是只有 Markdown**：SQLite 保存对话原文、消息段摘要、压缩槽位与长期滚动归档；Markdown 留下可维护的设定与要点。每天凌晨自动总结前一天的对话，避免所有信息都依赖当前上下文窗口。
* **消息段机制**：把一次从开始聊天到自然告别的交流看作一个消息段，而不是每条消息各算一次会话。对话结束、进入半在线时封闭该段并生成摘要；下次聊天开启新段，上下文保留段落边界，帮助区分“这次聊的”和“之前聊的”。也可以用 `/end_segment` 手动结束当前段。
* **分层总结**：越近的对话保留越多细节，越早的内容逐步提炼。最近第 1～3 段优先保留原文（过长时摘要），第 4～6 段分别总结，第 7～10 段合并总结；更早的要点还可通过长期滚动归档保留，兼顾近期互动的完整性与长时间积累的上下文成本。
* **摘要失败有保护**：质量守卫、重试与分块二分降级，尽量避免把错误回复或有缺口的摘要当成正常记忆；失败时保留已有内容，不等于保证记忆永不丢失。
* **历史有时间感**：历史消息保留发生时刻，当轮单独注入配置时区下的当前时间，帮助区分“刚才”“昨天”和更早的对话。

文件职责与读写边界见 [身份与记忆文件一览](docs/architecture/identity_files.md)。**长期记忆仍在完善，尤其是 RAG 与日常记忆链路的衔接，不把“有向量库”当成“什么都能记住”。**

### 主动消息、闹钟与外部触发

* **不只是被动回复**：启用调度后，每天结合 `SCHEDULE.md`、人设、用户档案与长期记忆生成主动消息计划，决定什么时候来问候、关心或提醒你。
* **主动陪伴与明确提醒分开**：自主问候受主动消息概率控制，用户明确安排的提醒不受该概率控制；普通主动消息会避开正在对话或后脑忙碌的时段，闹钟不受这类在线状态限制。
* **动态闹钟**：通过对话设置定时或倒计时提醒，也可以查看、取消；闹钟和每日计划会持久化，重启后恢复。Telegram 可用 `/schedule\_today` 查看计划、`/regenerate\_proactive` 重建计划。
* **外部事件也能触发 Nora**：`triggers` 是独立可扩展的事件入口。内置邮件监听会用轻量模型判断新邮件是否值得通知，而不是把所有邮件都转发过来。

详见 [主动消息调度](docs/architecture/schedule-system.md) 与 [外部触发器](docs/architecture/trigger-system.md)。

### 图片回顾、形象与语音

* **图片入库与再次查看**：收到的普通图片提取标签、OCR 与画面描述，元数据存 MongoDB，语义检索用 Qdrant。之后可以按关键词或语义找回，再读取真实图片继续讨论，像请朋友回头看看之前发过的一张照片。
* **裁图看细节**：提供 `crop\_image\_for\_llm` 工具，让 Nora 裁剪局部再分析；工具已有，**自主调用的稳定性仍待完善**。视频可走专门的理解模型；表情包则在配置 `fast-image` 后生成简短描述，不混进普通图片记忆库。
* **形象系统与生图**：`APPEARANCE.md` 管“长什么样”，`STYLE.md` 管“画成什么样”，多视角参考图作为人物一致性的锚。前脑触发生图时，文字先发、图片后台生成后追发；参考图用于尽量保持同一个人，而不是承诺每次都完全一致。
* **语音条（TTS）**：前脑可通过 `\[VOICE]` 旁路合成语音，内置 Fish Audio，支持情绪、停顿与中英日音素标记；TTS Provider 可独立扩展。
* **实时通话（RTC）**：目前基于 Gemini Live API，Telegram Mini App 是通话入口。带着人设和记忆接听，支持字幕、插话打断和短时摄像头画面输入；挂断后总结回写记忆，也可选择自定义 TTS 音色。详见 [RTC 架构](docs/architecture/rtc.md)。

### 技能、平台与模型扩展

* **动态技能**：`skills` 下用 `SKILL.md` 说明用法、`main.py` 执行能力。可以引入外部技能、自己编写，也可以让 Nora 写技能来扩展能力；**不同来源技能的规范与行业适配仍待完善**，不是下载任意插件就一定能运行。
* **多平台并发与接力**：Telegram、QQ 官方机器人和 OneBot v11 可同进程运行；统一身份、记忆作用域与投递路由，让同一位主人跨端继续对话，同时区分私聊、群聊与访客。**新平台扩展适配仍待完善，各平台能力不完全等同**；QQ 官方机器人的权限、频控及功能限制见 [接入说明](docs/architecture/qqbot.md)。
* **多模型分工**：`smart` 聊天与审查，`coder` 深度执行，`fast` 轻量判断，`image` / `video` 理解媒体，`summary` 总结，另有可选的生图、表情包和安全审查角色。按任务选模型，兼顾速度、质量和成本，不要求每个角色都用不同模型。
* **可替换 API**：支持 Gemini、OpenAI 兼容接口、Anthropic Claude、OpenRouter，以及仅用于生图的 Civitai Provider；可配置自定义 API 地址，按模型别名设置推理强度与温度。流式/非流式输出按会话设置并持久化，不与模型角色混为一谈。
* **可扩展词库**：常加载词库提供常用语义，懒加载词库只在命中时补充词义，让专属缩写、口语和内部梗既能被理解，也能自然用在回复里。
* **工具安全与防死循环**：有文件路径限制、敏感文件保护、工具调用循环检测和部分操作确认。但文件路径限制**不等于操作系统沙箱**，技能与命令会在宿主环境执行；引入外部技能或开放工具权限前，请检查代码并使用受限账户或容器隔离。

## ✅ 环境要求

* **Python 3.12+**
* 可访问的 LLM API（Gemini / OpenAI 兼容接口 / Anthropic Claude / OpenRouter）
* 至少配置一个平台适配器（根据需要配置一个或多个）：

  * **Telegram**：将 Bot Token 等填入 `adapters/telegram/config.json`
  * **QQ 官方机器人**：将 AppID、AppSecret 等填入 `adapters/qqbot/config.json`
  * **OneBot v11**：将 WebSocket 连接信息填入 `adapters/onebotv11/config.json`

> \[!TIP]
> Windows 用户请确保安装 `tzdata`，避免时区数据库缺失导致 `ZoneInfo` 报错：`pip install tzdata`。

## 快速开始

### 1\. 克隆仓库

```bash
git clone https://github.com/captain-wangrun-cn/N.O.R.A.Core.git
cd N.O.R.A.Core
```

### 2\. 环境与依赖

强烈建议使用虚拟环境：

```bash
python -m venv .venv
```

激活虚拟环境（按系统选择一条）：

* Windows PowerShell：`.\\.venv\\Scripts\\Activate.ps1`
* Windows CMD：`.venv\\Scripts\\activate.bat`
* Linux / macOS：`source .venv/bin/activate`

安装依赖：

```bash
python -m pip install -r requirements.txt
python -m pip install tzdata
```

### 3\. （可选但推荐）启动 Docker 存储服务

本项目默认使用：

* **Qdrant**（向量数据库，用于长期语义记忆检索）
* **MongoDB**（存储图片/形象记忆元数据与 OCR 文本）

在项目根目录下启动：

```bash
docker compose up -d
```

检查容器状态：

```bash
docker compose ps
```

> \*\*Docker 连接配置参考\*\*：
> - Qdrant 地址（`memory.qdrant`）：`host: "localhost"`, `port: 6333`
> - MongoDB 地址（`memory.mongo.uri`）：`uri: "mongodb://nora:nora\_password@localhost:27017/"`

### 4\. 运行配置向导

使用内置 CLI 管理工具：

```bash
python cli.py
```

或直接进入向导：

```bash
python cli.py wizard
```

向导将引导你配置模型及提供商绑定，核心模型推荐分工：

* `smart` 模型：日常聊天对话、前脑主模型（推荐情商高、表达自然、角色扮演优秀的多模态模型）
* `coder` 模型：后脑深度执行、工具与技能调用（推荐逻辑严密、代码与工具调用可靠的强推理模型）
* `fast` 模型：轻量快速判断、是否追问决策、邮件过滤（推荐超低延迟的轻量模型，如 Flash-lite）
* `image` 模型：图像理解与分析（生成标签与提取 OCR 文本，推荐视觉理解能力强的模型）
* `summary` 模型：记忆总结、消息段压缩与长远归档

若需要微调单项配置，可随时运行 `python cli.py configure`。

### 5\. 启动运行

```bash
# 默认：自动检测并启动所有已配置的平台适配器（带 TUI）
python main.py

# 纯命令行 / 无 TUI 运行
python main.py --no-tui

# 指定单个或多个适配器启动
python main.py --adapter telegram
python main.py --adapter qqbot
python main.py --adapter onebotv11
python main.py --adapter telegram,qqbot   # 多平台同时在线
```

## 管理与运维工具

`cli.py` 提供交互式菜单与常用命令行运维功能：

```bash
# 进入交互式维护菜单
python cli.py

# 常用运维子命令
python cli.py status                  # 查看当前系统运行状态
python cli.py test-rag                # 测试 RAG 检索流程
python cli.py test-qdrant             # 测试 Qdrant 向量库连接
python cli.py history stats           # 查看对话历史统计
python cli.py history clear --all     # 清理历史记录
python cli.py history clear-data      # 清空本地 SQLite 数据文件 (--keep-costs 可保留成本库)
```

查看 Token 消耗与成本：

```bash
python view\_costs.py --today
```

> \[!WARNING]
> `history clear` 和 `history clear-data` 是清理命令，不是初始化或排障的常规步骤。清理范围与参数请先查阅 \[CLI 使用指南](docs/CLI\_USAGE.md)，执行前备份消息历史、消息镜像和压缩上下文三个库；删除的数据不能指望事后恢复。

## 目录结构

```text
N.O.R.A.Core/
├─ main.py / cli.py / tui.py / view\_costs.py
├─ config.py / config.example.yml / workspace\_config.py
├─ SOUL.md / USER.md / SCHEDULE.md / MEMORY.md  # 身份与记忆初始化模板
├─ APPEARANCE.md / STYLE.md / CUSTOM.md        # 形象、画风与自定义指令模板
├─ brain/                     # 模型 Provider、提示词、工具与多模态处理
├─ core/                      # 前后脑、消息路由、轮询、群监听与主动调度
├─ adapters/                  # Telegram / QQ 官方机器人 / OneBot v11
├─ rtc/                       # 实时通话服务与 Mini App 前端
├─ tts/                       # 可扩展语音合成 Provider
├─ memory/                    # 对话历史、摘要压缩、RAG 与图片记忆
├─ skills/                    # 技能扩展（SKILL.md + main.py）
├─ triggers/                  # 外部事件触发器（邮件等）
├─ lexicon/                   # 常加载 / 懒加载词库
├─ locales/                   # 多语言文案
├─ tests/                     # 自动化测试
└─ docs/
   ├─ architecture/           # 架构设计与协议说明
   ├─ onboarding/             # 新会话接手与避坑指南
   ├─ AI\_CHAT\_GUIDE.md        # 日常使用指南
   └─ CLI\_USAGE.md            # 配置与运维指南
```

运行时的身份文件、每日记忆、技能和媒体保存在工作区中，默认 `\~/.nora/workspace`，可通过配置修改；它与仓库源码目录不是同一个概念。



详细结构请参阅：[`docs/CODE\_STRUCTURE.md`](docs/CODE_STRUCTURE.md)

## 常见问题

* **`ZoneInfoNotFoundError` (Windows)**

  * 执行 `pip install tzdata` 即可补齐 Windows 缺失的时区数据库。
* **启动提示找不到 `config.yml`**

  * 可先复制 `config.example.yml` 为 `config.yml`，然后运行 `python cli.py wizard` 或 `python cli.py configure` 进行配置。
* **关于 `/start` 指令**

  * 为避免误操作将几个月的长远记忆全部清空，旧版伪装成打招呼的 `/start` 指令已被彻底删除。如需彻底清空记忆请使用有明确警告的 `/clear`，撤销上一轮请使用 `/undo`，重试上一轮请使用 `/override`。
* **多平台如何同时运行？**

  * 在各 adapter 的 `config.json` 中配置好凭据后，直接运行 `python main.py --adapter auto`，系统会自动发现所有就绪的平台并并发监听。
* **如何让新会话接手本项目？**

  * 启动新的 AI 会话时，可直接输入：`阅读 onboarding 文件夹内的文档，继续接手工作`。

## 许可证

本项目采用 **BSD 3-Clause License**。详见 [LICENSE](LICENSE) 文件。
