# N.O.R.A. Core - 项目执行计划书

**文档版本:** 4.0 (Interactive Paradigm Shift)
**更新日期:** 2026-02-06
**当前状态:** Phase 3 (Skills) 核心功能完成，交互范式已升级。正在进行收尾工作。

---

## 1. 核心愿景 (Grand Goal)

**N.O.R.A. Core 的最终目标，是创造一个与主人共同成长的、拥有完美记忆的、永不消逝的数字灵魂。**

---

## 2. 今日战果 (Achievements - 2026-02-06)

**里程碑达成：交互范式革命 & 高级编程能力。**

### A. 交互范式 (Phase 3.5 Completed)
- **🧠 智能分段:** 实现了 `send_intermediate_message` 工具和相应的 Prompt 协议，使 Nora 能够像人类一样、有节奏地分段发送长消息。
- **🛡️ 稳定性修复:**
    - 解决了 Telegram HTML `<ul>` 标签不支持的崩溃问题。
    - 解决了“消息过长”的崩溃问题，加入了工具输出截断器。
- **🎯 认知矫正:**
    - 通过重命名工具和强化 Prompt，解决了 `send_message` 导致的“工具 vs 技能”认知混淆。
    - 注入了“用户指令至上”协议，解决了 Nora 因“过于主动”而忽略用户核心任务的问题。

### B. 高级编程能力 (Phase 3.2 Completed)
- **🔪 代码手术刀:** 实现了 `edit_file` 工具，赋予 Nora 精准修改代码的能力。
- **📞 专家委托:** 实现了 `delegate_to_coder` 工具和动态模型路由，使 Nora 能将复杂代码任务外包给专门的 Code LLM。
- **⚙️ 配置向导:** 将 `coder` 模型的配置加入 `configure.py`，完善了项目工程化。

### C. 技能系统 (Phase 3.1 Completed)
- **🧩 拥抱标准:** 升级 `SkillLoader` 以全面支持 `Agent Skills` 开放标准的 YAML Frontmatter，并强化了技能模板 (`skill_creator`)。
- **📖 先读后做:** 在 Prompt 中注入了“技能学习协议”，确保 Nora 在使用技能前会先阅读 `SKILL.md` 文档。

---

## 3. 已完成任务清单 (Phase 3)

- [x] **Task 4.1 - 4.3:** 技能系统（加载、工具、执行循环）全线贯通。
- [x] **Task 4.3.1 (衍生):** 实现 `edit_file` & `delegate_to_coder` 工具。
- [x] **Task 4.3.2 (衍生):** 实现 `send_intermediate_message` 智能分段交互。
- [x] **Task 4.3.3 (衍生):** 修复一系列由交互升级引发的认知和稳定性 Bug。

---

## 4. 下一步行动纲领 (Next Actions)

- [x] **Task 5.1: 修复 `/start` 命令** (🐞 **已解决**)
    - **成果:** 通过改为 `startswith("/start")` 解决了消息聚合导致的重置失效问题。

#### **任务 5.2: 交互范式二度进化 - 流式分段** (🛡️ **已完成**)
- **挑战:** 解决工具化分段发送导致的乱序、重复及无限循环问题。
- **方案:** 引入 `[SPLIT]` 流式分割符，实现真正的实时同步发送。
- **状态:** 已通过 `controller.py` 的深度重构实现。

#### **任务 4.4: 首批技能开发 - Web Search** (🚀 **即将开始**)
- **目标:** 在全新的、稳固的架构上，创建第一个高级技能。
- **步骤:**
    1.  使用 `skill_creator` 创建 `skills/web_search/SKILL.md` (遵循新模板)。
    2.  使用 `delegate_to_coder` 编写 `skills/web_search/search.py` 脚本。
    3.  进行测试和验收。
