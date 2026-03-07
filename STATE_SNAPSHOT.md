# N.O.R.A. Core - 状态快照

**文档版本:** 5.0
**更新日期:** 2026-03-07
**当前状态:** 主干稳定；前脑优先路由已落地；媒体输入协议已统一。

---

## 1. 当前架构状态

### 1.1 路由：前脑优先（已启用）
- 所有用户消息先进入前脑（fast 模型）。
- 前脑即时回复用户，并判断是否需要后脑执行任务。
- 需要后脑时，前脑回复中附加 `[NEED_BACKEND]` 信号。
- Controller 解析并移除该信号后发送给用户，再自动启动后脑工具循环。

### 1.2 后脑任务循环（保持）
- 后脑仍负责 RAG、工具调用、技能执行、结果总结。
- `WorkerStatus` + `pending_messages` 的忙碌/打断/排队机制保持可用。

### 1.3 媒体协议（已统一）
- 统一使用 English-only 标识：`[image:]`、`[video:]`、`[audio:]`、`[file:]`、`[doc:]`。
- 系统模板与 Telegram 适配已对齐。

---

## 2. 近期已完成（2026-03-07）

- [x] 新增图片模型别名 `image`，并在输入含图片时自动切换模型（可选，缺省回退）。
- [x] 主系统提示加入跨平台媒体输入协议。
- [x] 清理中文媒体标签，统一到英文标签。
- [x] 重写 `adapters/telegram/PROMPT.md`，去除历史损坏内容。
- [x] 路由改为“前脑优先”，移除独立路由前置调用。
- [x] 新增前脑路由信号解析函数与测试。

---

## 3. 关键文件映射

- `core/controller.py`
    - `_start_routed_task`：统一先走前脑。
    - `_generate_front_chat_response`：前脑即时回复 + 路由判断 + 后脑交接。
    - `_generate_response`：后脑工具循环（保留）。
- `core/routing.py`
    - `has_image_input`：图片输入检测。
    - `parse_front_brain_response`：解析 `[NEED_BACKEND]` 信号。
- `brain/templates/system.jinja`
    - 跨平台媒体输入协议（英文标识）。
- `adapters/telegram/main.py`
    - 结构化媒体标签输出（英文）。

---

## 4. 验证状态

- 路由与协议相关测试通过：
    - `tests/test_routing.py`
    - `tests/test_tool_plan.py`
- 全量测试在当前环境下存在两类非本次改动问题：
    1) 缺少 `config.yml`
    2) 缺少 `tzdata`（时区测试失败）

---

## 5. 后续建议

1. 给 `[NEED_BACKEND]` 增加更严格协议（仅允许末尾，避免误触）。
2. 为“前脑判定 → 后脑交接”补充端到端回归测试。
3. 如需跑全量测试，先补齐本地测试依赖：`config.yml` 与 `tzdata`。

---

## 6. 维护信息

- 记录人：GitHub Copilot
- 日期：2026-03-07
