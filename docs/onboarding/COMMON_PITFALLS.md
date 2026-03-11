# ⚠️ 常见陷阱与注意事项

> 历届会话踩过的坑，节省你的时间。

---

## 1. 环境依赖

### ❌ Windows 上 `ZoneInfo` 崩溃
- **现象：** `ZoneInfoNotFoundError: No time zone found with key UTC`
- **原因：** Windows 下 Python 的 `zoneinfo` 需要 `tzdata` 包提供时区数据。
- **修复：** `pip install tzdata`
- **影响范围：** `MessageHistory` 初始化、所有时区相关测试。

### ❌ 缺少 `config.yml`
- **现象：** `FileNotFoundError: config.yml not found`
- **原因：** 配置文件不在版本控制中，需要手动创建。
- **修复：** `cp config.example.yml config.yml` 然后 `python cli.py --configure`

---

## 2. 异步与事件循环

### ❌ 压缩不触发
- **现象：** 消息数量超过 `compress_window` 但 `summaries` 表为空。
- **原因：** `add_message()` 中的 `asyncio.create_task(self._check_and_compress(...))` 需要正在运行的事件循环。在同步调用或无 loop 的测试中，压缩会被静默跳过。
- **排查：** 确认调用链在 `asyncio.run()` 或已有 event loop 的环境内。

### ❌ 异步测试不运行
- **现象：** `test_message_history.py` 标记为 `FAILED: async def functions are not natively supported`
- **原因：** `pytest` 默认不支持 `async def` 测试函数。
- **修复：** `pip install pytest-asyncio` 并在测试上添加 `@pytest.mark.asyncio`。

---

## 3. 消息压缩逻辑

### ⚠️ Level 2 未实现
- 文档描述了三层（Level 1/2/3），但代码只写入 `level=1`（一级摘要）和 `level=3`（归档）。
- `_create_archive_summary` 会**删除所有** `level=1` 记录，导致归档后中期摘要消失。
- 如果需要保留中期层，需要修改归档逻辑。

### ⚠️ 压缩依赖 LLM
- 压缩/归档需要可用的 `summary` 模型（`config.yml` → `llm.models.summary`）。
- 如果模型不可用或 API Key 无效，压缩会静默失败（`except` 捕获后仅打日志）。

### ⚠️ 对话分段 (Session Segmentation)
- 对话段落在 AI 从 `ONLINE` 进入 `SEMI_ONLINE` 时自动创建。
- `messages` 表的 `session_id` 为 `NULL` 表示消息属于当前活跃对话。
- 段落摘要异步生成，短对话可能在摘要完成前 AI 已进入下一轮对话。
- 旧数据库会自动迁移（`ALTER TABLE` 添加 `session_id` 列），无需手动操作。
- `get_context_messages()` 在跨段落消息之间自动插入分隔标记，帮助 LLM 理解时间结构。
- `clear_chat_history` / `clear_all_history` 会同时清理 `conversation_sessions` 表。

---

## 4. 流式输出 / [SPLIT]

### ⚠️ [SPLIT] 只在流式中生效
- `[SPLIT]` 分段逻辑在 `controller.py` 的 `async for raw_chunk in stream` 循环中处理。
- 如果 LLM provider 走 legacy `str` 分支且未输出 `[SPLIT]` 标记，不会自动分段。
- 要让模型输出 `[SPLIT]`，靠 `system.jinja` 中的"消息节奏协议"引导。

### ⚠️ 工具调用时清空缓冲
- 检测到 `tool_call` 时会清空 `response_text_buffer`，避免"思考过程"泄漏给用户。
- 但如果工具调用前已经通过 `[SPLIT]` 发出了部分文本，这些**已发送的无法撤回**。

---

## 5. Telegram 适配

### ⚠️ 消息长度限制
- Telegram 单条消息上限 4096 字符。超长文本由 `_split_long_text()` 自动按段落/行/字符边界切割。
- 与 `[SPLIT]` 是两套独立机制：`[SPLIT]` 是语义分段，`_split_long_text` 是硬限制兜底。

### ⚠️ 群聊响应条件
- 群聊中只有被 @机器人 或回复机器人消息时才响应，私聊始终响应。
- `storage_id` 隔离：私聊用 user_id，群聊用 chat_id。

---

## 6. 成本跟踪

### ⚠️ 无内置价格表
- 已移除所有内置价格，完全依赖 `config.yml` → `cost_tracking.custom_prices`。
- 缺价模型**首次**会打 warning，之后不重复；但成本会记为 $0。
- CLI 向导中可现场输入价格并写入配置。

---

## 7. 开发注意

### ⚠️ 不要读 `config.yml` / `.env`
- 系统提示词中明确禁止 AI 读取配置文件（含 API Key）。
- 技能使用 `config.json` + 环境变量注入机制，不直接读取全局配置。

### ⚠️ `edit_file` 失败后立刻切 `write_file`
- 系统提示词约定：`edit_file` 失败一次就换 `write_file`，禁止反复重试。

### ⚠️ 同一文件 `edit_file` ≥ 3 次会被强制引导
- `controller.py` 中有计数器，对同一文件 `edit_file` 达 3 次时，会注入系统提示强制改用 `write_file`。

---

## 8. 工具调用泄漏 (Tool Call Text Leak)

### ❌ LLM 以文本形式输出工具调用，而非真正使用 function calling
- **现象：** AI 在回复中写出 `execute_tool('edit_file', {...})`、`<execute_skill>...</execute_skill>` 等文本，或带 `new_code="""..."""` 的 Python 代码块，而不是真正调用工具。用户看到了原始的工具调用代码。
- **原因：**
  1. LLM 从 system prompt 中的示例和工具描述中"学到"了调用语法，然后以文本形式模仿输出。
  2. **Gemini history 格式不正确**：工具调用/结果以纯文本格式追加到 history，导致 Gemini 在后续轮次中也以文本形式输出工具调用，而非触发 function calling。
  3. 某些模型（特别是 context 较长或多轮对话后）可能退化为文本模式。
  4. System prompt 中工具调用的示例写法可能误导模型。
- **防护层（已实现，四层防护）：**
  1. **Prompt 层：** `system.jinja` 中有"工具调用方式 — 严格规范"条目，明确禁止文本形式的工具调用。已清理所有可被模仿的调用语法示例。
  2. **History 格式层：** `controller.py` 中将工具调用以 `tool_call` / `tool_response` 结构化格式写入 history，provider 会将其转换为原生格式（Gemini: `function_call`/`function_response` parts；OpenAI: assistant/user messages）。
  3. **泄漏拦截 + 自动执行层：** `controller.py` 中 `_try_parse_leaked_tool_call()` 在检测到泄漏时，尝试解析并真正执行工具调用；解析失败则注入纠正提示让 LLM 重新生成。
  4. **最终输出过滤层：** `_is_tool_call_leak()` 和 `_clean_tool_call_leaks()` 在 [SPLIT] 实时分段和最终发送前清理泄漏文本。
- **如果仍然出现：**
  1. 检查 `system.jinja` 中是否有新增的工具调用文本示例——绝不要在 prompt 中写出 `tool_name("arg1", "arg2")` 形式的代码。
  2. 检查 `_TOOL_LEAK_PATTERNS` 是否覆盖了新的泄漏模式，按需添加。
  3. 确认 LLM provider 正确传递了 `tools` 参数到 API。
  4. 检查 `_try_parse_leaked_tool_call()` 是否能解析新的泄漏格式。

### ⚠️ 双脑架构中"反复要求修改但没执行"
- **现象：** 用户反复要求修改文件，但后台显示没有执行工具。
- **原因：**
  1. 当工具泄漏（P1 问题）发生时，后端 `_generate_response` 认为已"完成"（因为没有真正的 tool_call），文件从未被修改。
  2. 用户连续发消息时，可能命中双脑架构的"忙碌"路径，消息被 fast_llm 处理为 queue/queue/queue，没有触发新的完整生成流程。
- **修复：** 解决 P1（泄漏 → 自动执行）后，此问题自动消失。

### ⚠️ LLM 只回复文本，不主动调用工具（工具自觉性差）
- **现象：** 用户请求明显需要工具操作（如"帮我改一下这个文件"），但 LLM 只给出纯文本回复（如"建议您运行以下命令..."、给出代码块让用户自己跑），不调用任何工具。
- **原因：**
  1. 某些模型（尤其上下文较长时）倾向于给建议而非直接执行。
  2. 之前的 `_generate_response` 中，LLM 第一轮不调用工具就直接 `break` 退出循环，没有重引导机制。
  3. 工具 schema 中参数缺少 description，LLM 不确定如何传参时倾向于不调用。
- **防护层（已实现，三层防护）：**
  1. **Prompt 层：** `system.jinja` 中新增"你是执行者，不是建议者"条款，明确禁止回复代码块让用户自己跑。
  2. **Controller 层：** `_should_nudge_tool_use()` 在第一轮无工具调用时，检查回复是否包含代码块（``` ）——这是"建议用户自己跑"的最直接信号。若检测到，注入纠正提示强制 LLM 重新生成并使用 function calling。仅纠正一次，防止无限循环。
  3. **Schema 层：** `_generate_schema()` 现在从 docstring 的 `:param` 注释中提取参数描述，让 LLM 更清楚如何使用工具。
- **如果仍然出现：**
  1. 确认工具 schema 的 description 字段是否足够清晰。
  2. 检查 `system.jinja` 中"执行者不是建议者"条款是否仍然存在。
