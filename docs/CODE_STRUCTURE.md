# 代码结构概览

> 仓库：`N.O.R.A.Core`（`main` 分支）

## 顶层结构

```text
N.O.R.A.Core/
├─ main.py / cli.py / tui.py / view_costs.py
├─ config.py / config.example.yml / workspace_config.py
├─ lexicon/
├─ brain/
├─ core/
├─ adapters/
├─ memory/
├─ skills/
├─ triggers/
├─ tests/
├─ docs/
├─ locales/
└─ LICENSE / README.md / requirements.txt ...
```

## 顶层文件说明

- `main.py`：程序入口。
- `cli.py`：配置向导与运维命令入口。
- `tui.py` / `tui.css`：终端 UI。
- `view_costs.py`：成本查询工具。
- `config.py`：配置读取与访问器。
- `workspace_config.py`：工作区管理与启动同步（如技能/架构文档同步）。
- `config.example.yml`：配置模板。

## 核心目录说明

### `core/`（控制与编排）

- `controller.py`：`NoraController` 主控制器（组合各 Mixin）。
- `message_handler.py`：消息入口路由、命令分发。
- `front_brain.py`：前脑即时回复 + 后脑结果审查。
- `back_brain.py`：后脑执行循环（RAG、工具/技能、生成）。
- `interrupt_handler.py`：忙碌时意图检测（stop/change/queue）。
- `polling.py`：前后脑轮询协同。
- `scheduler.py` / `scheduler_mixin.py`：主动消息调度与状态管理。
- `routing.py`：信号解析与辅助路由逻辑。
- `worker_status.py`：后脑状态与队列结构。
- `cost_tracker.py`：token 使用与成本记录。

### `brain/`（模型与提示词）

- `llm.py`：模型客户端工厂（按 alias/provider 获取）。
- `providers/`：提供商实现（`gemini.py`、`openai.py`）。
- `prompts.py`：系统提示词与身份上下文注入。
- `templates/`：Jinja 模板（system/persona/schedule/front_brain 等）。
- `tools.py`：内置工具注册、schema 生成与执行。
- `multimodal.py`：图片输入解析与多模态辅助。
- `logging_config.py`：日志配置。

### `adapters/`（平台适配）

- `base.py`：平台适配器基类 + `AdapterEvent` / `AdapterMessage` / `MessageSegment` 轻量标准层。
- `aggregator.py`：连续消息聚合。
- `PROMPT.md`：通用平台适配协议（跨平台连续性、媒体标记、输出分层）。
- `ADAPTER_GUIDE.md`：新增/维护 adapter 的开发指南。
- `telegram/`：Telegram 适配实现，按 `main/incoming/sender/reply/commands/callbacks` 等职责拆分，含 `metadata.json` 与平台提示。

### `memory/`（记忆与数据）

- `message_history.py`：消息持久化、压缩、分段与上下文重建。
- `context_store.py`：消息镜像与段落压缩存储。
- `rag.py` / `embed.py` / `vector.py`：检索增强与向量能力。
- `image_store.py`：图片记忆（MongoDB + Qdrant 双写）。

### `lexicon/`（轻量词库）

- `manager.py`：词库加载与检索管理器（常加载 + 懒加载 manifest）。
- `always/`：常加载词库（注入 system prompt）。
- `lazy/`：懒加载词库（命中后注入 user prompt）。
- `PROMPT.md`：词库全局用途说明（全链路注入 system prompt）。

### `skills/`（技能系统）

- `loader.py`：技能扫描与加载。
- `skill_creator/`：技能脚手架创建相关能力。
- `web_search/`、`web_fetch/`：内置示例技能。

### `triggers/`（外部触发系统）

- `base.py`：Trigger 基类。
- `manager.py`：触发器管理器。
- `factory.py`：按配置构建触发器。
- `email/`：Email Trigger 实现。
- `config.example.yml`：触发器配置模板。

### `docs/`（文档）

- `architecture/`：架构文档全集。
- `onboarding/`：接手指南与约定。
- `CLI_USAGE.md` / `HANDOVER.md`：使用与交接文档。

### `tests/`（测试）

- 覆盖 CLI、成本、记忆、RAG、工具、路由、时区、多模态、触发器等核心能力。

## 配置与运行数据

- `config.yml`：本地运行配置（由 `config.example.yml` 复制生成）。
- 默认 workspace：`~/.nora/workspace`（可在 `config.yml -> workspace.root_path` 覆盖）。
- 常见数据文件（位于 workspace 下）：
  - `data/memory/message_history.db`
  - `data/memory/message_log.db`
  - `data/memory/context_compression.db`

## 运行要点

- 主入口：`python main.py`
- 配置向导：`python cli.py` 或 `python cli.py --configure`
- 测试：`pytest`
- 架构总览：`docs/architecture/README.md`
