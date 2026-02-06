# N.O.R.A. Core - Technical Design Document (TDD)

> **Project Name:** N.O.R.A. Core  
> **Version:** 1.0.0-draft  
> **Maintainers:** CaptainCN (WR), Nora  
> **Repository:** https://github.com/captain-wangrun-cn/N.O.R.A.Core  

---

## 1. Executive Summary

N.O.R.A. Core (Neural Optimized Responsive Assistant) is a bespoke, lightweight AI agent framework designed to replace the generic OpenClaw system. It prioritizes **cost-efficiency**, **long-term memory retention (RAG)**, and **autonomous capability expansion (Hot-Reloading Skills)**.

The system is built on a **Python-first** architecture, leveraging **Docker** for infrastructure consistency and **Git** for version control.

---

## 2. System Architecture

### 2.1 High-Level Component Diagram

```mermaid
graph TD
    User((User)) -->|Telegram API| Controller[Controller (main.py)]
    
    subgraph "N.O.R.A. Runtime"
        Controller -->|Context| Router{LLM Router}
        
        subgraph "Cognitive Layer"
            Router -->|Chat/Roleplay| FlashModel[Gemini 2.0 Flash]
            Router -->|Logic/Code| ProModel[Gemini 1.5 Pro]
        end
        
        subgraph "Memory Layer (RAG)"
            Controller <-->|Query/Upsert| VectorStore[(Qdrant)]
            VectorStore <-->|Vectorize| EmbedAPI[SiliconFlow BGE-M3]
            Controller -->|Archive| DocStore[(MongoDB)]
        end
        
        subgraph "Capability Layer"
            ProModel -->|Tool Calls| SkillMgr[Skill Manager]
            SkillMgr -->|Load/Exec| PyScripts[Python Scripts (tools/*.py)]
        end
    end
```

### 2.2 Core Components Specification

#### 2.2.1 Controller (`main.py`)
- **Responsibility:** Event loop handling, message routing, and error management.
- **Library:** `python-telegram-bot` (Async Mode).
- **Logic:**
  1. Receive `Update` from Telegram.
  2. Retrieve user context from `Memory Layer` (L1 + L2).
  3. Construct `System Prompt` (Identity + Skills).
  4. Invoke `Cognitive Layer`.
  5. Execute `Capability Layer` (if Tool Calls present).
  6. Return response to user.

#### 2.2.2 Cognitive Layer (`brain/`)
- **LLM Client:** Unified wrapper for OpenAI-compatible APIs (Gemini/DeepSeek/SiliconFlow).
- **Router Logic:**
  - **Heuristic:** If message contains code blocks or keywords (`write`, `script`, `analyze`) -> **Pro Model**.
  - **Default:** -> **Flash Model**.
- **Prompt Engineering:**
  - Context window management (Sliding Window: last 10 messages).
  - RAG Context injection (Top-k relevant memories).

#### 2.2.3 Memory Layer (`memory/`)
- **Vector Database:** **Qdrant** (Local Docker instance).
- **Embedding Model:** **BGE-M3** via SiliconFlow API (or local `sentence-transformers` fallback).
- **Data Schema (Qdrant Point):**
  ```json
  {
    "id": "uuid",
    "vector": [0.12, ...],
    "payload": {
      "text": "User prefers Voxel games.",
      "timestamp": "2026-02-06T06:00:00Z",
      "tags": ["preference", "gaming"],
      "type": "chat_history"
    }
  }
  ```

#### 2.2.4 Capability Layer (Skill System) (`tools/`)
- **Design Pattern:** Dynamic Module Loading (`importlib`).
- **Discovery:** Watchdog process monitors `tools/*.py`.
- **Definition:** Each tool module must export a `TOOL_DEF` dict and a `run(**kwargs)` function.
- **Security:** Subprocess isolation for code execution (optional in V1, trusted environment).

---

## 3. Implementation Roadmap

### Phase 1: Foundation (Current)
*Objective: Establish connectivity and basic chat capability.*
- [x] **Repo Init:** Git structure and environment config.
- [ ] **LLM Integration:** Implement `brain/llm.py` using `openai` SDK (targeting Gemini endpoint).
- [ ] **Telegram Hook:** Connect `main.py` to basic echo logic.
- [ ] **Identity:** Implement `SOUL` injection.

### Phase 2: Memory Integration
*Objective: Enable persistence across sessions.*
- [ ] **Infrastructure:** `docker-compose.yml` for Qdrant & MongoDB.
- [ ] **RAG Pipeline:** Implement `memory.add(text)` and `memory.query(text)`.
- [ ] **Context Injection:** Modify Controller to fetch memory before LLM call.

### Phase 3: Autonomous Expansion
*Objective: Enable self-coding capabilities.*
- [ ] **Tool Manager:** Implement hot-reloading logic in `tools/manager.py`.
- [ ] **Meta-Tools:** Create `write_tool.py` allowing the AI to generate new Python scripts in `tools/`.

---

## 4. Development Standards

### 4.1 Code Style
- **Language:** Python 3.10+
- **Formatting:** PEP 8 (Use `black` formatter).
- **Typing:** Strict Type Hints (`typing.List`, `typing.Optional`) required for all function signatures.
- **Async:** Use `asyncio` for all I/O bound operations (DB, API, Network).

### 4.2 Configuration Management
- **Secrets:** All API keys must be loaded from `.env` via `python-dotenv`.
- **Constants:** Non-secret config (timeouts, model names) in `config.py`.

### 4.3 Directory Structure
```text
nora-core/
├── main.py                 # Application Entry Point
├── config.py               # Global Configuration
├── docker-compose.yml      # Infrastructure Definitions
├── requirements.txt        # Python Dependencies
├── .env                    # Secrets (Not in Git)
├── brain/
│   ├── __init__.py
│   ├── llm.py              # LLM Client Adapter
│   ├── prompts.py          # System Prompt Templates
│   └── router.py           # Model Routing Logic
├── memory/
│   ├── __init__.py
│   ├── vector.py           # Qdrant Wrapper
│   └── mongo.py            # MongoDB Wrapper
└── tools/
    ├── __init__.py
    ├── manager.py          # Tool Loader/Executor
    └── base_tools.py       # Built-in Tools (e.g., Web Search)
```

---

*Document Status: Draft / Active*
*Last Updated: 2026-02-06*
