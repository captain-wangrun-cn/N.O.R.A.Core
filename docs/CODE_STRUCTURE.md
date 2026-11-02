# 代码结构概览

> 仓库：`N.O.R.A.Core`（`main` 分支）

## 顶层结构

```text
N.O.R.A.Core/
├─ main.py / cli.py / tui.py / view_costs.py
├─ config.py / config.example.yml / workspace_config.py
├─ lexicon/               # 词库子系统
├─ brain/                 # 模型与认知
├─ core/                  # 控制与调度
├─ adapters/              # 平台适配器
├─ rtc/                   # 实时音视频通话
├─ tts/                   # 语音合成
├─ memory/                # 多级记忆与上下文
├─ skills/                # 动态技能体系
├─ triggers/              # 外部事件触发器
├─ tests/                 # 测试集
├─ docs/                  # 系统设计与接手文档
├─ locales/               # 多语言资源
└─ LICENSE / README.md / requirements.txt ...
```

## 顶层文件说明

- `main.py`：程序主入口，支持并发拉起多个平台适配器并管理应用生命周期。
- `cli.py`：交互式向导、单项配置微调与运维子命令工具。
- `tui.py` / `tui.css`：基于 Textual 的终端 UI 界面。
- `view_costs.py`：Token 消耗与成本查询工具。
- `config.py`：配置读取、持久化、校验与访问器。
- `workspace_config.py`：工作区路径管理与资源目录同步（如技能、身份文档等）。
- `config.example.yml`：核心配置模板。

## 核心目录说明

### `core/`（控制与编排）

- `controller.py`：`NoraController` 主控制器，组合所有功能 Mixin。
- `message_handler.py`：消息入口路由、指令分发与多平台投递回执处理。
- `group_listener.py`：按 `platform:chat_id` 管理群聊运行态，协调消息窗口与打断逻辑。
- `group_presence_store.py`：持久化群聊在线状态（`ONLINE` / `SEMI_ONLINE`）。
- `front_brain.py`：前脑即时回复与后脑工作结果审查转述。
- `back_brain.py`：后脑执行循环（RAG 检索、工具/技能循环、多模态标签提取）。
- `interrupt_handler.py`：忙碌状态下的意图检测（`stop` / `change` / `queue`）。
- `polling.py`：前后脑轮询协同机制与延迟状态消费。
- `scheduler.py` / `scheduler_mixin.py`：主动消息调度、每日日程生成与动态闹钟。
- `routing.py`：前脑标记解析（`[NEED_BACKEND]`、`[DRAW:]`、`[VOICE]`、`[SEMI_ONLINE]` 等）。
- `appearance_gen.py`：形象生图与异步追发流水线。
- `worker_status.py`：后脑工作状态与任务队列模型。
- `cost_tracker.py`：Token 用量与成本记录。

### `brain/`（模型与认知）

- `llm.py`：模型客户端工厂（统一按 alias 或 provider 实例化与路由）。
- `providers/`：多模型提供商原生接入：
  - `gemini.py`：Google GenAI SDK 与官方 REST 双通道，支持 URL 编码转义命名空间模型名。
  - `openai.py`：OpenAI 原生与通用兼容网关接口，适配流式与非流式工具调用。
  - `anthropic.py`：Anthropic Claude 客户端支持，兼顾思考态模型。
  - `openrouter.py`：OpenRouter 聚合接口接入。
  - `civitai.py`：Civitai 生图与 LoRA 调度。
- `prompts.py`：系统提示词组装、时区时间注入与身份上下文嵌入。
- `templates/`：Jinja2 模板库（`system`、`persona`、`front_brain`、`media_analysis`、`draw_desc`、`image_tags` 等）。
- `tools.py`：内置工具注册（读写、搜索、闹钟、技能等）与 Schema 生成。
- `multimodal.py`：图片与视频解析辅助（表情包首帧抽取、缩略图生成）。
- `appearance.py`：形象系统共享原语（读取 `APPEARANCE.md` 与参考图锚点）。

### `adapters/`（平台适配层）

- `base.py`：适配器基类，定义 `AdapterEvent`、`AdapterMessage`、`MessageSegment` 标准层。
- `aggregator.py`：用户连续快速输入消息聚合器。
- `delivery.py`：不可变投递快照与部分发送回执机制。
- `telegram/`：Telegram 适配实现（长轮询、Mini App RTC 呼叫、指令注册与撤销）。
- `qqbot/`：QQ 官方开放平台机器人适配（WebSocket 连接、主动消息、分片媒体上传、两分钟内自身撤回）。
- `onebotv11/`：OneBot v11 协议适配（正向/反向 WebSocket，支持 Base64 媒体跨机传输）。
- `ADAPTER_GUIDE.md`：适配器扩展开发指南。

### `memory/`（多级记忆与数据）

- `message_history.py`：原始消息持久化、多级摘要生成、构建模型可见历史上下文的唯一入口（`build_history_messages`）。
- `context_store.py`：上下文槽位压缩、产物质量守卫与分块二分降级重试（`chunked fallback`），读路径自动剥离存量标签。
- `rag.py` / `embed.py` / `vector.py`：RAG 检索增强与 Qdrant 向量操作。
- `image_store.py`：图片记忆（MongoDB 元数据 + Qdrant 向量检索）。
- `appearance_store.py`：形象生成图记录与历史检索。

### `rtc/`（实时通话子系统）

- `server.py`：基于 aiohttp 的 WebSocket 桥接服务器，编排浏览器客户端与 Google Live API。
- `live_client.py`：Google Live API 双向 WebSocket 客户端封装（音频流与转写处理）。
- `auth.py`：双轨认证（Telegram initData HMAC 校验 + 静态 Token）。
- `session.py` / `invitation.py`：通话会话状态机、时长控制与邀请管理。
- `history_writer.py`：通话结束自动生成总结并沉淀为记忆。
- `web/`：无状态前端（原生 JS + AudioWorklet 纯净实现）。

### `tts/`（语音合成子系统）

- `base.py`：`BaseTTSProvider` 统一基类。
- `registry.py`：Provider 动态发现与加载（内置 `fish_audio`，支持中英日音素与情绪标记）。
- `TTS_GUIDE.md`：自定义 TTS Provider 编写指南。

### `triggers/`（外部事件驱动）

- `base.py` / `manager.py` / `factory.py`：Trigger 基类与生命周期管理。
- `email/`：IMAP 邮件监听与模型过滤。

### `lexicon/`（词库系统）

- `manager.py`：词库加载与命中检索管理器。
- `always/` 与 `lazy/`：常加载词库（进 system prompt）与懒加载词库（命中进 user prompt）。

## 配置与数据路径

- 主配置文件：`config.yml`（由 `config.example.yml` 复制生成）。
- 默认工作区根目录：`~/.nora/workspace`（可在 `config.yml -> workspace.root_path` 覆盖）。
- 核心本地数据（位于工作区下）：
  - `data/memory/message_history.db`（历史消息数据库）
  - `data/memory/context_compression.db`（段落摘要与压缩槽位数据库）
  - `data/memory/message_log.db`（日志镜像库）
  - `cost_tracker.db`（成本统计库）
