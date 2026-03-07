# 双进程架构设计 (Dual-Process Architecture)

## 概述

传统的聊天机器人架构通常是单线程阻塞式的：收到消息 -> 思考/调用工具 -> 生成回复。在这个过程中，机器人处于"在此期间若有新消息则无法处理"的状态，导致用户体验僵硬，无法打断长任务。

N.O.R.A. Core 引入了 **"脑口分离" (Brain-Mouth Separation)** 的设计理念，将"即时交互"与"深度思考"解耦。

## 路由策略：前脑优先 (Front-Brain-First)

**所有消息首先进入前脑**，由前脑自行判断是否需要后脑介入：

1. 用户发送消息 → 前脑（fast_llm）立即处理
2. 前脑生成自然回复，同时判断是否需要工具/技能操作
3. **不需要后脑** → 回复用户，结束
4. **需要后脑** → 在回复末尾附加 `[NEED_BACKEND]` 标记 → 回复用户后自动启动后脑任务

**优势**（相比旧的独立路由模型）：
- **省一次 LLM 调用** — 不再需要独立的路由分类器
- **用户零等待** — 前脑回复先到，后脑在后台执行
- **更准确** — 前脑拥有完整上下文和对话历史，判断比独立路由器更可靠

## 核心组件

### 1. 状态追踪器 (`WorkerStatus`)

这是连接前端（交互层）与后端（思考层）的共享内存。

- **职责**: 实时记录后端正在进行的任务状态。
- **数据结构**:
  - `busy` (bool): 是否正在处理任务
  - `phase` (str): 当前阶段 (e.g., "RAG检索", "工具执行")
  - `detail` (str): 详细描述 (e.g., "正在搜索关键词: python async")
  - `tool_history` (list): 已执行步骤的摘要
  - `original_query` (str): 最初触发该任务的用户指令

### 2. 前脑交互层 (Front Brain / Fast Path)

- **对应方法**: `_generate_front_chat_response`
- **模型**: `fast_llm` (响应快，成本低)
- **双重职责**:
  1. **即时回复** — 用自然语言直接回应用户
  2. **路由判断** — 通过 `[NEED_BACKEND]` 标记决定是否需要后脑
- **逻辑**:
  1. 收到用户消息，生成自然对话回复
  2. 解析回复中是否包含 `[NEED_BACKEND]` 信号
  3. 移除信号标记后发送清洁回复给用户
  4. 若需要后脑 → 启动后脑任务（`_generate_response`）
  5. 若不需要 → 直接结束

### 3. 后脑思考层 (Back Brain / Slow Path)

- **对应方法**: `_generate_response`
- **模型**: `llm` (主模型，逻辑强)
- **逻辑**:
  - 这是一个 `asyncio.Task` 后台任务。
  - 执行 RAG、工具调用循环、最终回复生成。
  - **关键**: 在执行的每一步（RAG前、工具调用前、生成前），都会更新 `WorkerStatus`，让前端时刻"知道"自己在干什么。

### 4. 忙碌时的意图检测

- **对应方法**: `_detect_interrupt_intent_and_reply`
- **触发条件**: 后脑忙碌时用户发来新消息
- **行为**: 用 fast_llm 判断 STOP / CHANGE / QUEUE 意图

## 交互流程图

```mermaid
graph TD
    User[用户消息] --> FrontBrain[前脑交互层]
    FrontBrain -->|生成回复 + 路由判断| Reply1[发送前脑回复给用户]
    
    Reply1 -->|检查信号| Signal{包含 NEED_BACKEND?}
    
    Signal -- No --> Done[结束 ✅]
    
    Signal -- Yes --> BackBrain[启动后脑任务]
    BackBrain --> Brain[后脑思考层\n(RAG / Tools / Generate)]
    Brain -->|更新状态| SharedState[(WorkerStatus)]
    Brain -->|完成| Reply2[发送后脑结果给用户]

    User2[用户新消息\n(后脑忙碌时)] --> CheckBusy{后端忙吗?}
    CheckBusy -- Yes --> FastLLM[快速模型判断意图]
    SharedState --> FastLLM
    
    FastLLM -->|ACTION: queue| Queue[加入待处理队列]
    FastLLM -->|ACTION: stop| Stop[取消后端任务]
    FastLLM -->|ACTION: change| Change[取消后端 + 启动新任务]
```

## 路由信号协议

前脑通过在回复末尾附加 `[NEED_BACKEND]` 标记来请求后脑介入。

**示例：**
- `"好的，让我来帮你查一下~ [NEED_BACKEND]"` → 需要后脑
- `"今天天气不错呢！"` → 不需要后脑
- `"马上处理 ✨ [NEED_BACKEND]"` → 需要后脑

**解析逻辑** (`parse_front_brain_response`):
1. 检测回复中是否包含 `[NEED_BACKEND]`
2. 移除标记，清理多余空行
3. 返回 `{needs_backend: bool, user_reply: str}`

## 意图判断逻辑 (Intent Detection)

当后端忙碌时，前端不会机械地排队用户的消息，而是通过 `fast_llm` 理解语义：

- **STOP (停止)**:
  - 用户语料: "停", "别做了", "取消", "够了"
  - 行为: 立即调用 `task.cancel()`，重置状态。
- **CHANGE (切换)**:
  - 用户语料: "换个话题", "先别查那个了，帮我查...", "错了，是这个..."
  - 行为: 停止当前任务 -> 立即启动新任务处理该消息。
- **QUEUE (排队/追加)**:
  - 用户语料: "还有个事...", "进度怎么样了?", (补充细节)
  - 行为: 回复用户已收到 -> 将消息存入 `pending_messages` -> 后端任务完成后自动处理。

## 优势

1.  **高响应性**: 用户永远不会觉得机器人"卡死"了。前脑先回复，后脑在后台执行。
2.  **零浪费路由**: 前脑回复即路由，不需要额外的路由 LLM 调用。
3.  **可打断性**: 发现机器人理解错了或任务耗时过长，可以随时叫停。
4.  **自然交互**: 机器人对自己正在做的事情有"自我意识"（通过读取 WorkerStatus），回复更自然。
