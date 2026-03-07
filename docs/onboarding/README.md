# 🚀 N.O.R.A. Core — 新会话接手指南

> **目的：** 帮助任何新接手本项目的 LLM 会话在 5 分钟内建立完整上下文，避免重复探索和踩坑。

---

## 0. 你是谁、你在哪

| 项目 | 值 |
|---|---|
| 仓库 | `N.O.R.A.Core`（branch: main） |
| 语言 | Python 3.12+，asyncio 驱动 |
| 类型 | 多平台 AI 智能体内核（Telegram 为主适配） |
| 核心目标 | 像真人一样陪伴对话 + 可自主执行工具/技能 |
| 维护者 | CaptainCN (WR) + Nora (AI) |

---

## 1. 必读文档（按顺序）

> **原则：先了解全貌，再深入模块。**

| # | 文档 | 说明 | 预计阅读 |
|---|------|------|----------|
| 1 | **本文件** (`docs/onboarding/README.md`) | 你正在读的引导 | 2 min |
| 2 | `docs/HANDOVER.md` | **交接文档** — 最近的关键改动、模块速览、注意事项 | 3 min |
| 3 | `docs/CODE_STRUCTURE.md` | 代码目录结构概览 | 1 min |
| 4 | `DEV_BIBLE.md` | 技术设计文档（架构图、交互模型） | 3 min |
| 5 | `docs/onboarding/QUICK_REFERENCE.md` | 关键模块速查 + 常用命令 | 2 min |
| 6 | `docs/onboarding/COMMON_PITFALLS.md` | 已知坑点与易犯错误 | 2 min |
| 7 | `docs/onboarding/CONVENTIONS.md` | 开发约定（代码风格、测试、提交） | 1 min |

**深入某个子系统时再读：**
- `docs/architecture/README.md` — 架构文档索引（消息压缩、抢占、技能系统等）
- `brain/templates/system.jinja` — 系统提示词（行为规则全在这里）
- `STATE_SNAPSHOT.md` — 项目执行计划与历史里程碑

---

## 2. 核心架构一句话

```
用户消息 → MessageAggregator（聚合） → NoraController（路由+脑口分离）
  → LLM 流式生成（chat_stream）→ [SPLIT] 实时分段发送
  → 工具/技能调用 → 结果拼接 → 原子输出
```

- **脑口分离**：后端深度思考 + fast_llm 前端即时回复（打断/排队/切换）
- **消息历史**：SQLite 持久化，三层压缩（原始 → 摘要 → 归档），Pinned 永久保留
- **技能系统**：`skills/` 下每个技能有 `main.py` + `SKILL.md`，通过 `execute_skill` 调用

---

## 3. 关键文件速查

| 功能 | 主文件 |
|------|--------|
| 入口 | `main.py` |
| CLI / 配置向导 | `cli.py` |
| 核心控制器 | `core/controller.py` |
| LLM 调用 | `brain/llm.py`, `brain/providers/gemini.py`, `brain/providers/openai.py` |
| 工具注册 | `brain/tools.py` |
| 系统提示词 | `brain/templates/system.jinja` |
| 消息历史/压缩 | `memory/message_history.py` |
| RAG | `memory/rag.py`, `memory/vector.py`, `memory/embed.py` |
| Telegram 适配 | `adapters/telegram/main.py` |
| 技能加载 | `skills/loader.py` |
| 成本跟踪 | `core/cost_tracker.py`, `view_costs.py` |
| 配置 | `config.py`, `config.example.yml` |

---

## 4. 快速验证环境

```bash
# 1. 安装依赖
pip install -r requirements.txt
pip install tzdata  # Windows 必需

# 2. 配置
cp config.example.yml config.yml
python cli.py --configure

# 3. 运行
python main.py

# 4. 测试
pytest

# 5. 查看成本
python view_costs.py --today
```

---

## 5. 接手检查清单

新会话接手时，快速过一遍：

- [ ] 读完本文件 + `HANDOVER.md`
- [ ] 了解 `core/controller.py` 的消息处理流程
- [ ] 了解 `brain/templates/system.jinja` 中的行为约定
- [ ] 确认 `config.yml` 存在且配置正确
- [ ] 确认 `workspace/data/memory/message_history.db` 是否存在（有则说明有历史数据）
- [ ] 运行 `pytest` 确认测试状态
- [ ] 查看 `git log --oneline -10` 了解最近改动

---

## 6. 下一步

- 如果要**修改行为/提示词** → 编辑 `brain/templates/system.jinja`
- 如果要**添加新工具** → 编辑 `brain/tools.py`
- 如果要**添加新技能** → 使用 `skill_creator` 或手动创建 `skills/技能名/`
- 如果要**修改消息处理逻辑** → 编辑 `core/controller.py`
- 如果要**适配新平台** → 继承 `adapters/base.py` 创建新适配器

> 💡 **提示：** 修改完成后，请更新 `docs/HANDOVER.md` 记录你的改动，方便下一个会话接手。
