# N.O.R.A.Core

N.O.R.A.Core 是一个多平台 AI 智能体框架，旨在提供高质量的个人陪伴与全能型效率助手能力。虽然不敢说完美，但在双脑架构、长期记忆沉淀、消息抢占机制与拟人化深度交互方面做了一些扎实有趣的探索。

> [!CAUTION]
> 本项目为**VibeCoding**项目 (氛围编程)   
> **99%的代码为AI生成**，本人仅提供架构设计与部分指导   
> 如果阅读本项目的源码给你带来了包括*心跳加速*、*高血压*等不适，本人不负任何责任   
> **本项目为个人项目，本来只是个人用，代码内可能包括但不限于各种离谱设计、史山代码、不统一的匪夷所思的 commit 信息等。甚至在某些文档里写了自己的一些偏见。但是请各位大佬手下留情**   
> 个人的详细声明可以阅读 [架构文档索引](docs/architecture/README.md)

## 架构设计

更多系统设计细节请查阅 [架构文档索引](docs/architecture/README.md)。

## 必读

[NORA 使用指南（实战版）](docs/AI_CHAT_GUIDE.md)

### NORA 特点

- **双脑协同与真人化交互**：传统机器人工作与对话只能单线程进行（工作完对话，对话完工作）。Nora 采用前脑（即时对话与路由审查）+ 后脑（深度工具调用、技能执行、检索）协同架构，一边干活一边聊天，随时可打断、可插话、可修改需求。
- **消息抢占与流式分段**：打破死板的“一问一答”模式，用户可以一次性连发多条消息，Nora 会智能聚合并在忙碌时进行意图识别。
- **拟人化追问与在线状态**：和 Nora 对话时若突然没有动静，她会像真人朋友一样适时追问；对话结束后自动转为半在线（`SEMI_ONLINE`）并封闭当前消息段，触发记忆总结。
- **自主形象系统与生图**：Nora 拥有自己的虚拟形象外貌设定（身高五官体型穿着等，维护于 `APPEARANCE.md`，配多视角参考图）。在前脑聊天中触发 `[DRAW:要求]` 即可依据人设、当前时间与对话上下文自主生图并发给你，始终保持同一人物形象。
- **动态技能与自主进化**：通过 `skills` 系统支持从外部获取或让 Nora 自行编写技能代码，即时加载并扩展能力（如查天气、搜图、执行自动化脚本等）。
- **外部事件与主动提醒**：支持可扩展的 Trigger 触发器系统（如邮件监听、定时器），支持依据日程计划和用户作息主动向用户问候或提醒。
- **实时语音与视频通话（RTC）**：基于 Gemini Live API 服务端桥接与 Telegram Mini App 瘦前端，支持随时语音/视频通话，具备低延迟打断、关键帧画面感知、字幕实时转写，通话结束自动生成摘要沉淀进记忆。
- **多平台并发支持**：已深度适配 Telegram、QQ 官方机器人（支持私聊与全群事件、安全按钮、分片媒体及回执机制）以及 OneBot v11，支持跨平台统一身份模型与多端接力。
- **模块化语音合成（TTS）**：支持前脑 `[VOICE]` 旁路语音生成，内置 Fish Audio 等 Provider，精确控制中英日音素与情感表达。
- **多模态与视觉记忆**：发送给 Nora 的图片自动提取标签与 OCR 文本存入数据库，支持后续按关键词语义回顾；动图与表情包智能抽帧转静态图，视频理解全面适配。
- **轻量扩展词库**：支持常加载与懒加载词库，可自由扩展专属口语词、缩写与内部梗。

### 项目核心能力

- **前后脑双进程协同**：前脑秒级响应用户输入，审查后脑执行成果并转述；后脑专注于工具执行、技能循环与长文本推理。
- **上下文分段与多级记忆系统**：
  - 消息历史持久化（SQLite）+ 图片多模态双写（MongoDB + Qdrant 向量检索）。
  - 四层存储：原始消息、段落摘要（`summary`）、压缩槽位（`context_store`）以及长期滚动归档（`Archive`）。
  - **产物质量守卫与分块二分降级重试（Chunked Fallback）**：针对长对话摘要可能触发的模型安全拦截或长度截断，引入自动分块二分重试与质量判定，杜绝记忆上下文断裂。
- **精准时间注入**：当轮对话根据用户配置的时区实时注入当前时间，历史记录规范时间戳前缀，杜绝跨轮与通话摘要的时间错乱。
- **多模型灵活路由**：
  - 分离多达 10 种模型角色别名：`smart`、`fast`、`coder`、`image`、`video`、`summary`、`draw`、`draw_desc`、`security`、`fast-image`。
  - 全面支持 Gemini（SDK 与官方 REST 模式，自动转义命名空间模型名）、OpenAI 兼容接口、Anthropic Claude、OpenRouter 以及 Civitai 生图接口。
  - 支持按别名设置独立的推理强度（`effort`）、采样温度（`temperature`）与输出模式（`/set_stream` 流式持久化）。
- **外部触发系统（Triggers）**：支持独立事件驱动（如 `EmailTrigger` 基于 IMAP 轮询和轻量模型智能过滤）。
- **安全与防死循环机制**：工具调用死循环检测、工作区路径沙箱隔离、敏感操作确认机制。

## ✅ 环境要求

- **Python 3.12+**
- 可访问的 LLM API（Gemini / OpenAI 兼容接口 / Anthropic Claude / OpenRouter）
- 至少配置一个平台适配器（根据需要配置一个或多个）：
  - **Telegram**：将 Bot Token 等填入 `adapters/telegram/config.json`
  - **QQ 官方机器人**：将 AppID、AppSecret 等填入 `adapters/qqbot/config.json`
  - **OneBot v11**：将 WebSocket 连接信息填入 `adapters/onebotv11/config.json`

> [!TIP]
> Windows 用户请确保安装 `tzdata`，避免时区数据库缺失导致 `ZoneInfo` 报错：`pip install tzdata`。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/captain-wangrun-cn/N.O.R.A.Core.git
cd N.O.R.A.Core
```

### 2. 环境与依赖

强烈建议使用虚拟环境：

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境 (Windows)
.venv\Scripts\activate

# 激活虚拟环境 (Linux / macOS)
source .venv/bin/activate

# 安装核心依赖
pip install -r requirements.txt
pip install tzdata
```

### 3. （可选但推荐）启动 Docker 存储服务

本项目默认使用：
- **Qdrant**（向量数据库，用于长期语义记忆检索）
- **MongoDB**（存储图片/形象记忆元数据与 OCR 文本）

在项目根目录下启动：

```bash
docker compose up -d
```

检查容器状态：

```bash
docker compose ps
```

> **Docker 连接配置参考**：
> - Qdrant 地址（`memory.qdrant`）：`host: "localhost"`, `port: 6333`
> - MongoDB 地址（`memory.mongo.uri`）：`uri: "mongodb://nora:nora_password@localhost:27017/"`

### 4. 运行配置向导

使用内置 CLI 管理工具：

```bash
python cli.py
```

或直接进入向导：

```bash
python cli.py wizard
```

向导将引导你配置模型及提供商绑定，核心模型推荐分工：
- `smart` 模型：日常聊天对话、前脑主模型（推荐情商高、表达自然、角色扮演优秀的多模态模型）
- `coder` 模型：后脑深度执行、工具与技能调用（推荐逻辑严密、代码与工具调用可靠的强推理模型）
- `fast` 模型：轻量快速判断、是否追问决策、邮件过滤（推荐超低延迟的轻量模型，如 Flash-lite）
- `image` 模型：图像理解与分析（生成标签与提取 OCR 文本，推荐视觉理解能力强的模型）
- `summary` 模型：记忆总结、消息段压缩与长远归档

若需要微调单项配置，可随时运行 `python cli.py configure`。

### 5. 启动运行

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
python view_costs.py --today
```

## 目录结构

```text
N.O.R.A.Core/
├─ main.py                    # 主程序入口（支持多适配器拉起与生命周期管理）
├─ cli.py                     # 配置向导与交互运维管理工具
├─ tui.py / tui.css           # 终端 Textual UI 界面
├─ view_costs.py              # Token 成本统计工具
├─ config.py / config.yml     # 配置加载、持久化与校验逻辑
├─ workspace_config.py        # 工作区隔离与资源目录同步
├─ brain/                     # 脑部核心逻辑（模型、提示词、工具）
│  ├─ providers/              # 模型提供商实现 (Gemini, OpenAI, Anthropic, OpenRouter, Civitai)
│  ├─ templates/              # Jinja2 系统提示词与各场景模板
│  ├─ tools.py                # 后脑内置工具注册与派发
│  ├─ multimodal.py           # 多模态解析与媒体辅助
│  └─ appearance.py           # 虚拟形象与参考图管理
├─ core/                      # 核心调度与编排控制层
│  ├─ controller.py           # NoraController 核心控制器
│  ├─ front_brain.py          # 前脑即时回复与结果审查
│  ├─ back_brain.py           # 后脑执行循环与工具调度
│  ├─ message_handler.py      # 统一消息路由、指令分发与投递回执
│  ├─ polling.py              # 前后脑轮询与打断协作
│  └─ scheduler.py            # 主动消息调度器
├─ adapters/                  # 平台适配器层
│  ├─ telegram/               # Telegram 机器人适配（长轮询、MiniApp RTC 按钮、指令菜单）
│  ├─ qqbot/                  # QQ 官方开放平台机器人适配（WebSocket、消息回执、分片媒体）
│  └─ onebotv11/              # OneBot v11 协议适配（正向/反向 WebSocket）
├─ rtc/                       # 实时通话子系统（Gemini Live WebSocket 桥接与瘦前端）
├─ tts/                       # 语音合成子系统（模块化 Provider，内置 fish_audio）
├─ memory/                    # 记忆与存储体系
│  ├─ message_history.py      # 消息持久化、多级摘要与上下文构建
│  ├─ context_store.py        # 压缩槽位与降级重试机制
│  ├─ rag.py / vector.py      # 向量检索与 RAG 支持
│  └─ image_store.py          # 图片元数据与 MongoDB 关联
├─ skills/                    # 技能扩展目录（内置 web_search, web_fetch 等）
├─ triggers/                  # 外部触发器系统（内置 email 邮件监听等）
├─ lexicon/                   # 扩展词库（常加载与懒加载机制）
├─ tests/                     # 自动化测试用例
└─ docs/                      # 完整文档体系
   ├─ architecture/           # 架构设计与协议详解
   ├─ onboarding/             # 新会话接手与避坑指南
   └─ AI_CHAT_GUIDE.md        # 日常使用指南（实战版）
```

详细结构请参阅：[`docs/CODE_STRUCTURE.md`](docs/CODE_STRUCTURE.md)

## 常见问题

- **`ZoneInfoNotFoundError` (Windows)**
  - 执行 `pip install tzdata` 即可补齐 Windows 缺失的时区数据库。
- **启动提示找不到 `config.yml`**
  - 可先复制 `config.example.yml` 为 `config.yml`，然后运行 `python cli.py wizard` 或 `python cli.py configure` 进行配置。
- **关于 `/start` 指令**
  - 为避免误操作将几个月的长远记忆全部清空，旧版伪装成打招呼的 `/start` 指令已被彻底删除。如需彻底清空记忆请使用有明确警告的 `/clear`，撤销上一轮请使用 `/undo`，重试上一轮请使用 `/override`。
- **多平台如何同时运行？**
  - 在各 adapter 的 `config.json` 中配置好凭据后，直接运行 `python main.py --adapter auto`，系统会自动发现所有就绪的平台并并发监听。
- **如何让新会话接手本项目？**
  - 启动新的 AI 会话时，可直接输入：`阅读 onboarding 文件夹内的文档，继续接手工作`。

## 许可证

本项目采用 **BSD 3-Clause License**。详见 [LICENSE](LICENSE) 文件。
