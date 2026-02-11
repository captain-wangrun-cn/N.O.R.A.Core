# 双进程架构设计 (Dual-Process Architecture)

## 概述

传统的聊天机器人架构通常是单线程阻塞式的：收到消息 -> 思考/调用工具 -> 生成回复。在这个过程中，机器人处于"在此期间若有新消息则无法处理"的状态，导致用户体验僵硬，无法打断长任务。

N.O.R.A. Core 引入了 **"脑口分离" (Brain-Mouth Separation)** 的设计理念，将"即时交互"与"深度思考"解耦。

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

### 2. 前端交互层 (The Mouth / Fast Path)

- **对应方法**: `handle_new_message`
- **模型**: `fast_llm` (响应快，成本低)
- **逻辑**:
  1.  收到用户消息。
  2.  检查 `WorkerStatus.busy`。
  3.  **若空闲**: 直接唤醒后端（Brain）。
  4.  **若忙碌**:
      - 收集当前后端状态摘要。
      - 调用快速模型进行 **意图判断 (Intent Detection)**。
      - 根据判断结果执行 `STOP` / `CHANGE` / `QUEUE` 操作。
      - 直接给用户返回确认消息（"好的，我停下了" 或 "收到了，稍等我做完手头的事"）。

### 3. 后端思考层 (The Brain / Slow Path)

- **对应方法**: `_generate_response`
- **模型**: `llm` (主模型，逻辑强)
- **逻辑**:
  - 这是一个 `asyncio.Task` 后台任务。
  - 执行 RAG、工具调用循环、最终回复生成。
  - **关键**: 在执行的每一步（RAG前、工具调用前、生成前），都会更新 `WorkerStatus`，让前端时刻"知道"自己在干什么。

## 交互流程图

```mermaid
graph TD
    User[用户消息] --> Mouth[前端交互层]
    Mouth -->|检查状态| Status{后端忙吗?}

    Status -- No --> StartBrain[启动后端任务]
    StartBrain --> Brain[后端思考层\n(RAG / Tools / Generate)]
    Brain -->|更新状态| SharedState[(WorkerStatus)]
    Brain -->|完成| Reply[发送最终回复]

    Status -- Yes --> FastLLM[快速模型判断意图]
    SharedState --> FastLLM

    FastLLM -->|ACTION: queue| Queue[加入待处理队列]
    Queue --> ReplyAck1[回复: 稍等, 忙完这个就看]

    FastLLM -->|ACTION: stop| Stop[取消后端任务]
    Stop --> ReplyAck2[回复: 已停止]

    FastLLM -->|ACTION: change| Change[取消后端 + 启动新任务]
    Change --> StartBrain
```

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

1.  **高响应性**: 用户永远不会觉得机器人"卡死"了。
2.  **可打断性**: 发现机器人理解错了或任务耗时过长，可以随时叫停。
3.  **自然交互**: 机器人对自己正在做的事情有"自我意识"（通过读取 WorkerStatus），回复更自然（如："我正在查天气，不过你要先听笑话吗？"）。
