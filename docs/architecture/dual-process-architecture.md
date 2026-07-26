# 双进程架构设计 (Dual-Process Architecture)

## 概述

传统的聊天机器人架构通常是单线程阻塞式的：收到消息 -> 思考/调用工具 -> 生成回复。在这个过程中，机器人处于"在此期间若有新消息则无法处理"的状态，导致用户体验僵硬，无法打断长任务。

N.O.R.A. Core 引入了 **"脑口分离" (Brain-Mouth Separation)** 的设计理念，将"即时交互"与"深度思考"解耦。

> 2026-03 更新：后脑在轮询模式下不再直接对用户发送分段输出，而是将全部结果汇总给前脑，由前脑统一转述，避免重复回复；后脑也会将工具结果与关键路径写入 `WorkerStatus`，前脑审查时一并可见。

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
 - **轮询场景**：后脑忙碌期间，前脑只负责意图判定/队列；后脑完成后，前脑审查后脑的“工作报告”并转述给用户（后脑不直接对用户说）。

### 3. 后脑思考层 (Back Brain / Slow Path)

- **对应方法**: `_generate_response`
- **模型**: `llm` (主模型，逻辑强)
- **逻辑**:
  - 这是一个 `asyncio.Task` 后台任务。
  - 执行 RAG、工具调用循环、最终回复生成。
  - **关键**: 在执行的每一步（RAG前、工具调用前、生成前），都会更新 `WorkerStatus`，让前端时刻"知道"自己在干什么。
  - **轮询模式下的输出汇总**：后脑流式分段（[SPLIT]）不会直接发送用户，而是累积到 `final_response_buffer` + `temp_history`，供前脑审查时一次性查看。工具结果/关键结果也写入 `WorkerStatus.tool_history_readable` 与 `key_results`，前脑审查时会附带给用户。

### 4. 忙碌时的意图检测

- **对应方法**: `_detect_interrupt_intent_and_reply`
- **触发条件**: 后脑忙碌时用户发来新消息
- **行为**: 用 fast_llm 判断 STOP / CHANGE / QUEUE 意图

### 5. 调试可观测性（`/debug`）

- **入口**: Telegram 指令 `/debug`（按会话 toggle 开/关）
- **作用**: 将内部关键阶段实时推送给用户，便于排查“为什么慢/在做什么”
- **推送内容**（开启时）:
  - RAG 检索命中情况
  - 推理轮次（Turn）
  - 工具/技能调用参数摘要
  - 工具执行结果摘要
  - token usage
  - 生成完成状态

该能力不改变前后脑主流程，仅增强可观测性与调试体验。

## 前后脑轮询/协同循环（新增）

> 目标：让前脑能**持续感知**后脑进度，并在后脑忙碌时对用户新消息进行**轮询式处理**（STOP / CHANGE / QUEUE），保持对话流畅可打断。

### 关键点

1) **异步后台任务**：后脑始终以 `asyncio.Task` 形式运行，不阻塞前脑；状态实时写入 `WorkerStatus`。
2) **忙碌轮询**：当后脑忙碌时，前脑收到新消息会触发 `_detect_interrupt_intent_and_reply`（fast_llm），按 STOP / CHANGE / QUEUE 路径处理。
3) **队列协同**：QUEUE 结果会将消息放入 per-chat `BackendTaskQueue`；当前任务完成的 `finally` 中会轮询队列并启动下一任务。
  - 2026-03-25 更新：如果是”前脑主动判定需要后脑”的新请求，而此时后脑仍忙碌，则不会直接入队；会先进入”待确认入队”状态，由前脑展示当前进度与队列详情，用户确认后才真正入队。
4) **在线状态三层模型**（2026-07-26 重构）：
   - **全局 `AIPresence`**（`core/scheduler.py`）：系统级忙碌标志（`is_generating` / `is_backend_busy`），不表示任何私聊在线状态。
   - **私聊 `PrivatePresenceStore`**（`core/private_presence_store.py`）：按 `memory_scope_id` 存储私聊的 `ONLINE` / `SEMI_ONLINE` 状态，持久化到 JSON，重启后过期自动重置。
   - **群聊 `GroupPresenceStore`**（`core/group_presence_store.py`）：按群 `runtime_key` 独立存储。
   - `_update_scheduler_state` / `_mark_scheduler_idle` 同步到私聊 `PrivatePresenceStore`，驱动对话延续计时器（FOLLOWUP 循环）。
5) **对话分段闭环**：FOLLOWUP 循环判定 END 或追话超限后，调用 `_transition_to_semi_online` 触发 `close_session`，封闭本轮对话段落。
6) **结果交接**：后脑结束后构建”工作报告”（包含分段输出、工具步骤、关键结果），前脑调用 `_front_brain_review` 审查并生成最终用户向回复；后脑在轮询模式下不直接触达用户。

### 时序（轮询协同）

```
用户消息 → 前脑即时回复 → (若 NEED_BACKEND) 启动后脑 Task
  ↓ (后脑忙碌期间)
用户再发消息 → fast_llm 轮询意图 (STOP/CHANGE/QUEUE)
  ↓
QUEUE → 入队（或先二次确认后入队）→ 当前后脑任务完成 → 轮询队列启动下一任务
STOP/CHANGE → 取消当前任务 (CHANGE 会立即以新消息重启后脑)
  ↓
后脑任务完成 → _mark_scheduler_idle → FOLLOWUP 计时开始
  ↓
FOLLOWUP 轮询判定 END/追话 → _transition_to_semi_online → close_session
```

### 与旧模型的区别

- 不是“前脑等后脑”的阻塞式：后脑始终异步运行，前脑可随时插话。
- 轮询的是“新消息意图”与“队列”，而非频繁拉取模型输出，成本更低。
- 结合对话分段（session）与在线状态，使“结束/追话/继续”可闭环管理。

## 交互流程图

```mermaid
graph TD
    User[用户消息] --> FrontBrain[前脑交互层]
    FrontBrain -->|生成回复 + 路由判断| Reply1[发送前脑回复给用户]
    
    Reply1 -->|检查信号| Signal{包含 NEED_BACKEND?}
    
    Signal -- No --> Done[结束 ✅]
    
  Signal -- Yes --> BackBrain[启动后脑任务]
  BackBrain --> Brain[后脑思考层<br/>(RAG/Tools/Generate)]
  Brain -->|更新状态| SharedState[(WorkerStatus)]
  Brain -->|完成工作报告| Report[后脑工作报告<br/>(含工具步骤/关键结果)]
  Report --> Review[前脑审查转述]
  Review --> Reply2[发送最终用户回复]

  User2[用户新消息<br/>(后脑忙碌时)] --> CheckBusy{后端忙吗?}
  CheckBusy -- Yes --> FastLLM[快速模型判断意图]
  SharedState --> FastLLM
    
  FastLLM -->|ACTION: queue| Queue[加入待处理队列]
  FastLLM -->|ACTION: stop| Stop[取消后端任务]
  FastLLM -->|ACTION: change| Change[取消后端 + 启动新任务]
  Queue -->|当前任务完成后出队| BackBrain
```

## 路由信号协议

前脑通过在回复末尾附加 `[NEED_BACKEND]` 标记来请求后脑介入。

### 扩展信号（2026-04）

除 `[NEED_BACKEND]` 外，当前系统还支持以下高频信号：

- `[NO_REPLY]`：静默执行本轮，不向用户发送可见回复。
  - 用途：仅下发任务指示、静默记录内部信息、避免重复打扰。
  - 行为：解析层会将 `should_reply=false`，轮询审查与后脑发送层都会抑制用户可见输出。
- `[NEED_FOLLOW]`：要求对话延续系统加速跟进。
  - 用途：当你判断“应该较快追一句”时，触发更短的首次 follow-up 延迟。
  - 行为：会将首次跟进延迟覆盖为 `FOLLOWUP_NEED_FOLLOW_DELAY`（默认 15 秒）。
- `[SPLIT:秒数]`：分段发送并指定段间延迟（例如 `[SPLIT:1.5]`）。
  - 用途：模拟更自然的打字停顿，而不是固定节奏。
  - 行为：`[SPLIT]` 使用默认极短停顿；`[SPLIT:数字]` 则按给定秒数 `sleep` 后发送下一段。

> 建议：标记放在回复末尾，避免混入正文语义；若本轮仅后台动作且不需用户感知，优先使用 `[NO_REPLY]`。

**示例：**
- `"好的，让我来帮你查一下~ [NEED_BACKEND]"` → 需要后脑
- `"今天天气不错呢！"` → 不需要后脑
- `"马上处理 ✨ [NEED_BACKEND]"` → 需要后脑
- `"我先在后台整理一下，不打扰你 [NO_REPLY]"` → 静默执行
- `"我记下了，稍后我会主动跟你确认 [NEED_FOLLOW]"` → 加速 follow-up
- `"收到啦[SPLIT:1.2]我先整理重点给你"` → 分段且延时 1.2 秒

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
  - 行为: 回复用户已收到 -> 将消息存入队列 -> 后端任务完成后自动处理。
  - 若这是前脑新判定出的后脑请求，且后脑当前忙碌，则先由前脑展示当前队列详情并询问是否确认入队；确认后才真正加入队列。

## 优势

1.  **高响应性**: 用户永远不会觉得机器人"卡死"了。前脑先回复，后脑在后台执行。
2.  **零浪费路由**: 前脑回复即路由，不需要额外的路由 LLM 调用。
3.  **可打断性**: 发现机器人理解错了或任务耗时过长，可以随时叫停。
4.  **自然交互**: 机器人对自己正在做的事情有"自我意识"（通过读取 WorkerStatus），回复更自然。
