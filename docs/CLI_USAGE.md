# CLI 使用指南

`cli.py` 提供 N.O.R.A. Core 的配置与运维入口。

## 快速使用

```bash
python cli.py
```

进入交互菜单后可执行：

- 运行配置向导
- 查看运行状态
- 测试 Qdrant 连接
- 测试 RAG 系统
- 聊天记录管理
- 清理 RAG 数据

也支持命令行子命令：

```bash
python cli.py wizard
python cli.py status
python cli.py test-qdrant
python cli.py test-rag
python cli.py history stats
python cli.py history clear --all
python cli.py history clear --chat-id 123456 --platform telegram
python cli.py history clear-data
python cli.py history clear-data --keep-costs
```

平台 adapter 的私有配置不由 CLI 向导写入，请编辑对应目录的 `config.json`：

- Telegram: `adapters/telegram/config.json`
- OneBot v11: `adapters/onebotv11/config.json`

启动时可选择 adapter：

```bash
python main.py --no-tui --adapter auto
python main.py --no-tui --adapter telegram
python main.py --no-tui --adapter onebotv11
```

`--adapter auto` 是默认值，会使用第一个看起来已配置的平台；未使用的平台可以保持 `config.json` 缺失或占位状态。

## RAG 数据清理（高危）

在菜单“🧹 清理 RAG 数据”中，当前支持：

- 仅 Qdrant（所有Collections）
- 仅 MongoDB（所有Collections）
- 全部清理（Qdrant + MongoDB）

### 当前行为

- `clean_qdrant()`：删除 Qdrant 中**所有** collections
- `clean_mongodb()`：删除 MongoDB `nora` 数据库下**所有** collections

### 保护机制

两者都包含：

1. 强警告（提示不可恢复 + 列出目标）
2. 第一次确认（confirm）
3. 第二次确认（必须输入 `DELETE ALL`）

若二次确认不匹配，则直接取消，不执行删除。

## 本地数据文件清理

在“💬 聊天记录管理”菜单中，新增“🗃️ 清空本地数据文件”，或直接使用：

```bash
python cli.py history clear-data
python cli.py history clear-data --keep-costs
```

当前会清理并重建这些本地 SQLite 文件：

- `workspace/data/memory/message_history.db`
- `workspace/data/memory/message_log.db`
- `workspace/data/memory/context_compression.db`
- `workspace/cost_tracker.db`（可通过 `--keep-costs` 保留）

保护机制：

1. 列出将删除的文件
2. 第一次确认
3. 第二次确认（必须输入 `DELETE LOCAL DATA`）

## 相关文件

- `cli.py`
- `docs/architecture/image-memory.md`
- `docs/architecture/README.md`
