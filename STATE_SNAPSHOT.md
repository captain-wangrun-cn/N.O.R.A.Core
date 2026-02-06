# N.O.R.A. Core - 项目执行计划书

**文档版本:** 3.0 (Phase 2 Complete)
**更新日期:** 2026-02-06
**当前状态:** Phase 2 (Memory) 已完成，Phase 3 (Skills) 准备启动。

---

## 1. 核心愿景 (Grand Goal)

**N.O.R.A. Core 的最终目标，是创造一个与主人共同成长的、拥有完美记忆的、永不消逝的数字灵魂。**

---

## 2. 今日战果 (Achievements - 2026-02-06)

**里程碑达成：Nora 现在拥有了长久记忆与连贯思维。**

### A. 记忆与灵魂 (Phase 2 Completed)
- **🧠 完整 RAG 闭环:** 实现了从 `Embedding` (感知) -> `Qdrant` (存储) -> `RAG Engine` (检索) -> `Controller` (注入) 的完整链路。Nora 现在能记住对话细节（如主人的幸运数字）。
- **💾 基础设施:** 成功封装了 Qdrant 和 Embedding 客户端，并编写了覆盖全链路的集成测试 (`tests/test_rag.py`)。

### B. 稳定性与体验 (Stability & Polish)
- **🛡️ 记忆抢救:** 修复了抢占逻辑，实现了“未说完的话”自动合并，保证高频对话不丢失上下文。
- **🎨 HTML 协议:** 彻底解决了 Telegram Markdown 解析报错问题，支持稳定的富文本。
- **🧹 代码洁癖:** 
    - **日志降级:** 将生产环境日志从“话痨”降级为关键信息流。
    - **警告屏蔽:** 使用 Context Manager 彻底根除了 Google SDK 的 `FutureWarning`。

### C. 交互升级
- **⚙️ 配置向导 v2.2:** 支持数据库、Embedding 服务商选择与智能预填。

---

## 3. 已完成任务清单 (Phase 2 Checklist)

- [x] **Task 3.1: 基础设施** (Docker Compose + Config)
- [x] **Task 3.2: 嵌入服务** (`memory/embed.py` - OpenAI/SiliconFlow 兼容)
- [x] **Task 3.3: 向量数据库** (`memory/vector.py` - Qdrant 封装)
- [x] **Task 3.4: RAG 引擎** (`memory/rag.py` - 上下文组装)
- [x] **Task 3.5: 控制器注入** (`core/controller.py` - 赋予记忆)

---

## 4. 下一步行动纲领 (Phase 3: Skill System)

现在的 Nora 有了记忆，下一步是给她**双手**。我们将构建一个插件化的技能系统。

#### **任务 4.1: 技能加载器 (`skills/loader.py`)**
- **目标:** 动态扫描 `skills/` 目录，加载 Python 脚本或配置文件。
- **设计:** 类似于 OpenClaw 的 `SKILL.md` 结构，但适配 N.O.R.A. Core 的 Python 运行时。

#### **任务 4.2: 工具注册表 (`brain/tools.py`)**
- **目标:** 将加载的技能转换为 LLM 可调用的 `Function Declaration` (Tool Use)。
- **适配:** 同时支持 Gemini 的 Function Calling 和 OpenAI 的 Tools 格式。

#### **任务 4.3: 执行引擎 (`core/executor.py`)**
- **目标:** 当 LLM 决定调用工具时，安全地执行对应的 Python 函数，并将结果返回给 LLM。

#### **任务 4.4: 首批技能开发**
- **Web Search:** 联网搜索能力。
- **Painter:** 接入 Nano Banana Pro 或类似绘图 API。

---
*文档状态: 更新至 v3.0 - 这是一个重要的里程碑。*
