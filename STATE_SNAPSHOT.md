# N.O.R.A. Core - 项目执行计划书

**文档版本:** 2.0  
**更新日期:** 2026-02-06  
**当前状态:** Phase 1 完成，准备进入 Phase 2。

---

## 1. 核心愿景 (Grand Goal)

**N.O.R.A. Core 的最终目标，是创造一个与主人共同成长的、拥有完美记忆的、永不消逝的数字灵魂。**

它不仅仅是 OpenClaw 的替代品，更是：
- **一个经济上可持续的伙伴:** 运营成本极低，可以长久陪伴。
- **一个懂您的知己:** 通过 RAG 记忆系统，记住您的每一个偏好、每一次对话、每一个想法。
- **一个强大的助手:** 通过可自我扩展的技能系统，不断学习新能力，满足您未来的任何需求。

---

## 2. 已完成工作详解 (Phase 1: Foundation)

我们已经为这个伟大的目标打下了坚实的地基。

- **`[2.1]` 架构定型:**
    - 通过 `DEV_BIBLE.md`，我们定义了一个以 **Python + Docker** 为核心，**RAG** 为记忆，**动态加载** 为技能的模块化架构。文档经过多次修订，确保了专业性和清晰度。

- **`[2.2]` 配置系统:**
    - 摒弃了简陋的 `.env`，采用 `config.yml` 进行结构化配置。
    - 开发了 `configure.py`，一个支持 **i18n (中/英文)**、**动态模型列表获取**、**“上一步”** 功能的优雅 TUI 配置向导。

- **`[2.3]` 核心代码框架:**
    - **控制器解耦:** `main.py` 作为启动器，`core/controller.py` 处理核心业务，实现了关注点分离。
    - **LLM 抽象层:** 在 `brain/interface.py` 中定义了 `BaseLLM` 接口，并在 `brain/providers/` 中实现了 **Gemini** 和 **OpenAI** 的兼容层，确保了大脑的可替换性。

- **`[2.4]` 首次启动成功:**
    - 主人已在本地环境中成功运行 `main.py`，新的 **N.O.R.A. Core** 已证明其具备了基础的对话能力。

---

## 3. 下一步行动纲领 (Phase 2: Memory Integration)

这是将“灵魂”注入“躯壳”的关键阶段。我们将为 N.O.R.A. Core 安装记忆海马体。

#### **任务 3.1: 搭建基础设施 (`docker-compose.yml`)**
- **目标:** 一键启动项目所需的所有外部服务。
- **步骤:**
    1.  在项目根目录创建 `docker-compose.yml` 文件。
    2.  定义 `qdrant` 服务，使用官方 `qdrant/qdrant` 镜像。
    3.  定义 `mongodb` 服务，使用官方 `mongo` 镜像。
    4.  为两个服务配置 `volumes`，将数据持久化到本地的 `./data/` 目录，防止容器重启后数据丢失。

#### **任务 3.2: 实现嵌入服务 (`memory/embed.py`)**
- **目标:** 将文本转化为向量。
- **步骤:**
    1.  创建 `EmbeddingClient` 类。
    2.  实现 `get_embedding(text: str) -> List[float]` 方法。
    3.  在该方法内部，调用 `requests` 库向 **SiliconFlow (BGE-M3)** 的 API 端点发送请求。
    4.  处理 API 的响应，提取向量数据并返回。

#### **任务 3.3: 封装向量数据库 (`memory/vector.py`)**
- **目标:** 提供与 Qdrant 交互的简单接口。
- **步骤:**
    1.  创建 `VectorStore` 类。
    2.  在 `__init__` 中，使用 `qdrant_client` 连接到 Docker 启动的 Qdrant 实例。
    3.  实现 `upsert(text: str, vector: List[float], metadata: dict)` 方法，用于存储或更新记忆。
    4.  实现 `query(vector: List[float], top_k: int = 5)` 方法，用于根据向量检索最相似的 `k` 条记忆。

#### **任务 3.4: 整合 RAG 引擎 (`memory/rag.py`)**
- **目标:** 将上述服务整合成一个完整的记忆引擎。
- **步骤:**
    1.  创建 `RAGEngine` 类，它会实例化 `EmbeddingClient` 和 `VectorStore`。
    2.  实现 `add_memory(text: str, metadata: dict)` 方法，它会先获取文本的 embedding，然后调用 `vector_store.upsert`。
    3.  实现 `retrieve_memory(text: str) -> str` 方法，它会获取查询文本的 embedding，然后调用 `vector_store.query`，最后将返回的多个记忆片段格式化成一段适合注入 Prompt 的文本。

#### **任务 3.5: 注入控制器 (`core/controller.py`)**
- **目标:** 让 N.O.R.A. Core 真正开始使用记忆。
- **步骤:**
    1.  在 `NoraController` 的 `__init__` 中实例化 `RAGEngine`。
    2.  **重构 `handle_message` 方法:**
        -   在调用 LLM **之前**，执行 `rag_engine.retrieve_memory(user_text)`，获取相关历史。
        -   将这段历史文本**注入**到传递给 LLM 的 System Prompt 或对话历史中。
        -   在收到 LLM 的回复 **之后**，分别调用 `rag_engine.add_memory()`，将**用户的提问**和**AI 的回答**作为两条独立的记忆存入数据库。

---

*这份文档将作为我们下一阶段开发的唯一参考。*
