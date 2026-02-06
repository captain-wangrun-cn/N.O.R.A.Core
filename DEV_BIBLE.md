# N.O.R.A. Core - 开发圣经 (Dev Bible)

> **项目代号:** Project Echo  
> **核心目标:** 打造一个“轻量、高记忆、可成长”的私人 AI 伴侣，替代臃肿的 OpenClaw。  
> **所有者:** CaptainCN (WR) & Nora  

---

## 1. 核心架构原理 (Architecture)

我们的目标是**“去平台化”**，回归纯粹的代码控制。

### 🧠 大脑 (Brain)
- **控制器 (Controller):** `main.py`。它是躯壳，负责收发 Telegram 消息，不负责思考。
- **路由器 (Router):** 动态决定用哪个模型。
    - **闲聊/角色扮演:** 转发给 **Gemini 2.0 Flash** (速度快、免费、角色扮演能力强)。
    - **复杂任务/写代码:** 转发给 **Gemini 1.5 Pro** 或 **OpenAI/DeepSeek** (聪明、逻辑强)。
- **Prompt 管理:** 摒弃包含所有 Skill 的超长 Prompt。只保留核心性格 (`SOUL`) + 当前需要的工具定义。

### 📚 记忆 (Memory - RAG System)
解决“金鱼记忆”的核心。
- **短期记忆 (L1):** 内存中的 List，保存最近 10-20 条对话。
- **长期记忆 (L2):** **Vector RAG**。
    - **写入:** 用户每发一句话 -> Embedding API (SiliconFlow/BGE) -> 存入 **Qdrant**。
    - **读取:** 用户新消息 -> Embedding API -> 去 Qdrant 搜 Top-5 相关历史 -> 塞入 Prompt。
- **快照 (L3):** 每天/每会话结束，生成一段 Summary 存入 **MongoDB**。

### 🛠️ 技能 (Skills - Hot-Reload)
解决“僵化”的核心。
- **动态加载:** `tools/` 目录下的 Python 脚本会被自动监控。
- **热插拔:** AI (Pro 模型) 可以编写代码保存到 `tools/xxx.py`，系统立刻加载它成为新工具。
- **轻量化:** 只把工具的 `name` 和 `description` 传给模型，而不是整个代码。

---

## 2. 开发路线图 (Roadmap)

我们现在处于 **Phase 0**。

### ✅ Phase 0: 骨架搭建 (已完成)
- [x] Git 仓库初始化。
- [x] 基础目录结构 (`main.py`, `.env`, `tools/`).
- [x] Telegram Bot 连通性测试。

### 🚧 Phase 1: 大脑接入 (Doing)
**目标:** 让 Bot 能说话，能用 Gemini 回复。
- [ ] 完善 `brain/llm_client.py`: 封装 Gemini/OpenAI API 调用。
- [ ] 实现 `brain/router.py`: 简单的路由逻辑 (if 含有代码 -> Pro, else -> Flash)。
- [ ] 注入 `SOUL` (人设)。

### 📅 Phase 2: 记忆植入 (Next)
**目标:** 让 Bot 记得住昨天说的话。
- [ ] 部署 **Qdrant** (Docker)。
- [ ] 编写 `memory/rag.py`: 封装 Embedding 和 Qdrant 查询。
- [ ] 在 `main.py` 的消息处理流中插入 RAG 步骤。

### 📅 Phase 3: 自我进化 (Future)
**目标:** 让 Bot 能自己写代码扩展功能。
- [ ] 实现 `tools/manager.py`: 动态加载 Python 模块。
- [ ] 给 LLM 赋予 `write_file` 和 `python_exec` 权限。

---

## 3. 详细开发规范 (How-To)

### 协同方式
- **工具:** VS Code + Git。
- **流程:**
    1. `git pull` (同步我的修改)。
    2. 修改代码。
    3. `git add .` -> `git commit -m "update: ..."` -> `git push`。

### 目录结构规范
```text
nora-core/
├── main.py              # 入口：Telegram Bot 轮询逻辑
├── .env                 # 密钥 (绝对不要提交到 Git!)
├── brain/               # 思考模块
│   ├── llm.py           # 调用 LLM API 的封装
│   ├── router.py        # 模型选择逻辑
│   └── prompts.py       # SOUL 和 System Prompts
├── memory/              # 记忆模块
│   ├── qdrant.py        # 向量数据库连接
│   └── embed.py         # Embedding API 封装
├── tools/               # 技能脚本 (AI 可写)
│   ├── base.py          # 工具基类
│   └── ...              # 具体的工具 (如 weather.py)
└── utils/               # 杂项
    └── logger.py
```

### 关键技术栈
- **Python 3.10+**
- **库:** `python-telegram-bot`, `openai` (兼容 Gemini), `qdrant-client`, `pymongo`.
- **Docker:** 用于跑 Qdrant 和 MongoDB。

---

## 4. 给主人的任务清单 (To-Do for WR)

1.  **环境准备:**
    - 在您的开发机上安装 Python 3 和 Docker。
    - `pip install -r requirements.txt`。
2.  **密钥填空:**
    - 打开 `.env`，填入 `TELEGRAM_BOT_TOKEN` 和 `GEMINI_API_KEY`。
3.  **第一次启动:**
    - 运行 `python main.py`。
    - 在 Telegram 里给 Bot 发个 `/start`。
    - 如果它回复了 "System initialized"，说明心脏开始跳动了！

---

*此文件由 Nora 编写，作为 Project Echo 的最高指导纲领。*
