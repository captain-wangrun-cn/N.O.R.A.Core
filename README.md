# N.O.R.A. Core (Project Echo)

**N.O.R.A.** (Neural Optimized Responsive Assistant) Core 是一个主要用于自娱自乐的 AI 助手框架，旨在提供个人陪伴和一点点效率提升。虽然不敢说完美，但在长期记忆和拟人化交互方面做了一些微小的尝试。

## 🏗 架构设计 (Architecture)

更多细节请查阅 [架构文档索引](docs/architecture/README.md)。

### 💡 一些不成熟的想法 (Key Concepts)

- **[双进程大脑 (Dual-Process Brain)](docs/architecture/dual-process-architecture.md)**: 尝试模仿了 "脑口分离" 的设计。用一个响应快的小模型来处理即时回复和打断（比如用户说"停"、"换一个"），后台再用大模型慢慢处理繁重的逻辑。
- **[抢占与打断 (Preemption)](docs/architecture/preemption.md)**: 试图让机器人支持被随时打断。用户可以在它思考或做事时插嘴，它会尽量像真人一样由小模型接管并停下手中的活。
- **[记忆增强 (Memory & RAG)](docs/architecture/message_history.md)**: 混合了一点 RAG 技术。结合 SiliconFlow Embeddings、Qdrant 向量库和本地 SQLite 存储，希望能让它记得更久一点。
- **技能系统 (Skills)**: 支持热重载的 Python 工具（位于 `tools/` 目录）。可以在运行时动态生成或修改，虽然还在持续完善中。
- **[内置工具系统文档 (Tools)](docs/architecture/tools.md)**: 汇总所有内置工具的参数、行为和安全约束，便于维护与扩展。
- **交互接口**: 目前通过统一的 `BaseAdapter` 接口支持 Telegram Bot API。

## 🚀 快速开始 (Setup)

1. 克隆仓库:

   ```bash
   git clone https://github.com/captain-wangrun-cn/N.O.R.A.Core.git
   cd N.O.R.A.Core
   ```

2. 安装依赖:

   ```bash
   pip install -r requirements.txt
   ```

3. 配置 (使用 CLI 向导):

   ```bash
   python cli.py --configure
   ```

   或者使用交互式菜单:

   ```bash
   python cli.py
   ```

4. 运行:
   ```bash
   # 带 TUI（默认）
   python main.py

   # 无 TUI / 纯命令行
   python main.py --no-tui
   ```

## 🛠 管理工具 (Management CLI)

N.O.R.A. Core 包含一个简易的 CLI 工具 (`cli.py`) 用于配置和维护：

```bash
# 交互式菜单
python cli.py

# 直接命令
python cli.py wizard           # 运行配置向导
python cli.py test-qdrant      # 测试 Qdrant 连接
python cli.py test-rag         # 测试 RAG 系统
python cli.py history          # 聊天记录管理
```

> ⚠️ RAG 数据清理说明：
> 在 CLI 菜单中执行“🧹 清理 RAG 数据”时，`clean_qdrant` / `clean_mongodb` 现已改为**清空所有 collections**（分别针对 Qdrant 与 MongoDB 的 `nora` 库），并包含**强警告 + 二次确认（输入 `DELETE ALL`）**。
> 该操作不可恢复，请谨慎使用。

详见 [CLI 使用指南](docs/CLI_USAGE.md)。

## 💰 成本追踪 (Cost Tracking)

为了防止 Token 用得太快心疼，内置了一个简单的计费追踪：

- **自动定价**: 内置了 Gemini 和 OpenAI 的参考价格
- **自定义定价**: 支持配置自定义模型价格
- **统计查看**: 可以按时间、模型查看大概的花费

```bash
# 查看总花费
python view_costs.py

# 查看今日花费
python view_costs.py --today

# 查看特定提供商的花费
python view_costs.py --provider gemini

# 查看特定模型别名的花费
python view_costs.py --alias smart
```

成本数据存储在 `<workspace>/cost_tracker.db`。

## 📂 目录结构 (Structure)

- `main.py`: 启动入口和 Bot 控制器。
- `brain/`: LLM 路由策略与思考逻辑。
- `core/`: 核心组件 (控制器、成本追踪)。
- `memory/`: 记忆系统 (RAG 与 数据库连接)。
- `skills/`: 动态技能脚本 (Tools)。
- `utils/`: 通用工具类。

## 📝 许可证 (License)

仅供个人学习与自娱自乐使用 (Private / Personal Use Only)。
