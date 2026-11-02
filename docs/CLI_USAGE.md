# CLI 使用指南

`cli.py` 提供 N.O.R.A. Core 的全功能配置向导与日常运维工具。

---

## 快速使用

### 1. 交互式菜单

直接运行即可进入交互式终端菜单：

```bash
python cli.py
```

在交互式主菜单中可进行：
- **🔧 配置管理（查看总览/修改）**：查看当前配置总览，并支持单项微调（无需重跑完整向导）
- **🧙 运行交互式配置向导**：全新部署或重构配置
- **📊 查看运行状态**：查看系统、平台适配器与模型接入状态
- **🧪 测试 Qdrant 连接**：验证向量数据库连接
- **🧠 测试 RAG 系统**：验证嵌入与语义检索链路
- **💬 聊天记录管理**：查看历史统计、清理会话、清空本地 SQLite 数据
- **🧹 清理 RAG 数据**：高危清理 Qdrant 与 MongoDB 记忆集合

---

### 2. 命令行子命令

`cli.py` 支持直接通过子命令快速执行运维任务，无需进入交互菜单：

```bash
# 运行完整配置向导
python cli.py wizard

# 查看配置总览与单项微调
python cli.py configure

# 显示系统运行状态报告
python cli.py status

# 测试向量库与 RAG 检索
python cli.py test-qdrant
python cli.py test-rag

# 聊天记录管理
python cli.py history stats                                            # 查看对话消息统计
python cli.py history clear --all                                     # 清理所有聊天历史（保留已标记项）
python cli.py history clear --chat-id 123456 --platform telegram      # 清理指定平台特定会话
python cli.py history clear-data                                      # 清空本地 SQLite 数据文件并重建
python cli.py history clear-data --keep-costs                         # 清空本地数据但保留 token 成本库
```

---

## 平台适配器配置与多平台启动

各平台适配器的私有接入凭据不由核心 CLI 向导管理，请在对应适配器目录的 `config.json` 中配置：

- **Telegram**：`adapters/telegram/config.json`
- **QQ 官方机器人**：`adapters/qqbot/config.json`
- **OneBot v11**：`adapters/onebotv11/config.json`

### 启动平台适配器

`main.py` 的 `--adapter` 参数支持多种启动方式：

```bash
# 1. 自动模式（默认）：并发拉起所有已正确配置的平台适配器
python main.py --adapter auto
# 或无 TUI 模式
python main.py --no-tui --adapter auto

# 2. 启动全部适配器
python main.py --adapter all

# 3. 指定单一适配器启动
python main.py --adapter telegram
python main.py --adapter qqbot
python main.py --adapter onebotv11

# 4. 多平台并发启动（逗号分隔）
python main.py --adapter telegram,qqbot
```

> [!NOTE]
> 默认的 `--adapter auto` 会自动检测所有已填写有效凭据的适配器并并发启动。未使用的平台适配器只需保持其 `config.json` 为空或占位状态即可。

---

## 本地数据文件清理（维护与重置）

在“💬 聊天记录管理”中选择“🗃️ 清空本地数据文件”，或直接执行：

```bash
python cli.py history clear-data
python cli.py history clear-data --keep-costs
```

该操作会清空并自动重建以下本地 SQLite 数据库文件：
- `workspace/data/memory/message_history.db`（历史消息库）
- `workspace/data/memory/message_log.db`（日志镜像库）
- `workspace/data/memory/context_compression.db`（上下文压缩槽位与段落摘要库）
- `workspace/cost_tracker.db`（成本统计库，添加 `--keep-costs` 可予以保留）

**安全保护机制**：
1. 明确列出即将被删除并重建的文件绝对路径；
2. 第一次确认（Y/n）；
3. 第二次确认（必须准确输入 `DELETE LOCAL DATA` 字符验证码方可执行）。

---

## RAG 远程数据清理（高危操作）

在菜单“🧹 清理 RAG 数据”中，支持：
- 仅清理 Qdrant（所有 Collections）
- 仅清理 MongoDB（所有 Collections）
- 全部清理（Qdrant + MongoDB）

**安全保护机制**：
包含双重确认，必须手动输入 `DELETE ALL`，若输入不匹配则立即取消。

---

## 相关文件索引

- `cli.py`：管理工具源码
- `main.py`：系统主入口与适配器解析
- `docs/architecture/image-memory.md`：多模态图像存储设计
- `docs/architecture/message_history.md`：多级记忆存储与压缩机制
- `docs/architecture/qqbot.md`：QQ 官方机器人适配器规范
