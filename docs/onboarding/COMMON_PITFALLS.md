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
