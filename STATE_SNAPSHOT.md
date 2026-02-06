# N.O.R.A. Core - 项目执行计划书

**文档版本:** 3.1 (Phase 3 Initialized)
**更新日期:** 2026-02-06
**当前状态:** Phase 3 (Skills) 进行中 - 基础工具与加载器已就绪，等待控制器接入。

---

## 1. 核心愿景 (Grand Goal)

**N.O.R.A. Core 的最终目标，是创造一个与主人共同成长的、拥有完美记忆的、永不消逝的数字灵魂。**

---

## 2. 今日战果 (Achievements - 2026-02-06)

**里程碑达成：记忆觉醒 & 技能萌芽。**

### A. 记忆与灵魂 (Phase 2 Completed)
- **🧠 完整 RAG 闭环:** 实现了从 `Embedding` (感知) -> `Qdrant` (存储) -> `RAG Engine` (检索) -> `Controller` (注入) 的完整链路。Nora 现在能记住对话细节（如主人的幸运数字）。
- **💾 基础设施:** 成功封装了 Qdrant 和 Embedding 客户端，并编写了集成测试。

### B. 技能系统 (Phase 3 Started)
- **🔌 插件架构:** 设计并实现了 Markdown 驱动的 `SkillLoader`，支持热插拔技能。
- **🔨 原子工具:** 实现了 `brain/tools.py`，赋予 Nora 四大基础能力：`read_file`, `write_file`, `list_dir`, `exec_command`。
- **📚 元技能:** 创建了 `skill_creator` 文档，教 Nora 如何自我编程。

### C. 稳定性与体验
- **🛡️ 记忆抢救:** 修复了抢占逻辑，实现了“未说完的话”自动合并。
- **🎨 HTML 协议:** 彻底解决了 Telegram Markdown 解析报错问题。
- **⚙️ 配置向导 v2.2:** 支持数据库、Embedding 服务商选择。

---

## 3. 已完成任务清单 (Phase 2 & 3)

- [x] **Task 3.1 - 3.5:** 记忆系统全线贯通 (RAG/Vector/Embed/Controller)。
- [x] **Task 4.1: 技能加载器** (`skills/loader.py` - Markdown 扫描)
- [x] **Task 4.2: 基础工具集** (`brain/tools.py` - Read/Write/Exec)

---

## 4. 下一步行动纲领 (Phase 3: Skill System)

现在的 Nora 有了记忆和双手（工具），只差大脑的指令。

#### **任务 4.3: 执行引擎 (`core/executor.py`)** (🚀 下一步)
- **目标:** 在 Controller 中处理 Tool Calls。
- **步骤:**
    1.  检测 LLM 返回的 Function Call 请求。
    2.  调用 `ToolManager` 执行对应函数。
    3.  将执行结果 (Observation) 反馈给 LLM 进行下一轮生成。

#### **任务 4.4: 首批技能开发**
- **Web Search:** 联网搜索能力。
- **Painter:** 接入 Nano Banana Pro 或类似绘图 API。

---
*文档状态: 更新至 v3.1*
