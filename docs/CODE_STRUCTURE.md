# 代码结构概览

> 仓库：N.O.R.A.Core（main 分支）

## 顶层文件
- `main.py` / `cli.py` / `tui.py` / `view_costs.py`：入口与命令行/终端 UI。
- `config.py` / `config.example.yml` / `workspace_config.py`：配置加载与默认模板。
- `requirements.txt` / `docker-compose.yml` / `LICENSE` / `README.md` / `DEV_BIBLE.md` / `STATE_SNAPSHOT.md`：依赖、部署与说明。
- `tui.css`：终端 UI 样式。
- `test_link_sanitize.py`：顶层独立测试。

## 核心目录
- `core/`
  - `controller.py`：主业务控制器，管理会话、LLM、工具、历史等（当前采用“前脑优先路由”：先前脑回复，再按需转后脑）。
  - `routing.py`：路由辅助函数（媒体输入检测、前脑路由信号解析等）。
  - `cost_tracker.py`：成本记录与价格查询。
  - `__pycache__/`

- `brain/`
  - `interface.py`：LLM 接口定义。
  - `llm.py`：LLM 客户端获取与调用。
  - `logging_config.py`：日志配置。
  - `multimodal.py`：多模态输入处理（图片标签提取、image_id 生成、二进制读取）。
  - `prompts.py`：提示词模板。
  - `tools.py`：工具注册与调用（含 `view_image` 图片检索工具）。
  - `providers/`：LLM 提供方适配（`gemini.py`、`openai.py`）。
  - `templates/`：Jinja 模板（系统与 persona，含 `image_tags.jinja` 图片标签提示）。

- `adapters/`
  - `base.py`：适配器基类（最小接口 + 生命周期/消息钩子 + 平台能力声明）。
  - `aggregator.py`：聚合器。
  - `ADAPTER_GUIDE.md`：新平台适配器开发指南。
  - `telegram/`：Telegram 适配实现（`main.py`、`PROMPT.md`、`__init__.py`），媒体下载路径为 `workspace/data/telegram/`。

- `memory/`
  - `message_history.py`：消息持久化、压缩、归档、上下文重建。
  - `embed.py` / `vector.py` / `rag.py` / `message_history.py` / `__pycache__/`：向量化、RAG 与消息历史。
  - `image_store.py`：图片记忆存储（MongoDB + Qdrant 双写），图片元数据管理与多模式检索。
  - `message_history.py`（主要逻辑在此）。

- `skills/`
  - `loader.py`：技能加载。
  - `web_search/`、`web_fetch/`、`skill_creator/`：技能实现与说明（`SKILL.md`、`main.py` 等）。

- `locales/`
  - `en_US.yml` / `zh_CN.yml`：多语言文案。

- `docs/`
  - `HANDOVER.md`、`architecture/`（架构文档：message history/compression、timestamp、tool-loop 等）。

- `tests/`
  - 各功能单测：`test_cli_price_display.py`、`test_context_pricing.py`、`test_cost_tracker.py`、`test_embedding.py`、`test_message_history.py`、`test_rag.py`、`test_schema_fix.py`、`test_timestamp.py`、`test_timezone.py`、`test_vector.py`。

## 配置与数据
- `config.yml`（需自行提供，示例见 `config.example.yml`）。
- `workspace/data/memory/message_history.db`：SQLite 聊天记录与摘要存储（路径可配置）。
- `workspace/data/telegram/`：Telegram 接收的图片/文档/贴纸落盘目录。

## 运行要点
- 入口：`main.py`（主流程）、`cli.py`（命令行工具）、`tui.py`（终端 UI）。
- LLM 配置在 `config.yml` 的 `llm.*`；消息历史配置在 `memory.message_history.*`。
- Telegram 适配器入口 `adapters/telegram/main.py`。
- `view_image` 支持 `return_image=true`，会返回可解析的图片内容标签并触发下一轮 image 模型分析。
- 路由策略：消息先进入前脑（fast 模型）做即时回复；若前脑回复中含 `[NEED_BACKEND]`，再启动后脑工具循环。
