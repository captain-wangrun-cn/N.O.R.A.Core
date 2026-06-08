# Adapter 系统

NORA 的 adapter 是平台通信层：它负责把真实平台事件转换为核心 controller 能理解的标准 context，也负责把核心回复转换成平台消息。

## 设计边界

- Adapter 不决定 Nora 的人格、记忆作用域或主人/访客关系。
- Adapter 只描述平台事实：消息来源、发送目标、平台 message_id、群聊/私聊类型、媒体路径、平台能力。
- 跨平台连续性由 `core/conversation_identity.py`、消息历史和 prompt 注入统一处理。

## 轻量分层

参考 NoneBot2 的 Adapter/Event/Message 思想，但保持 NORA 现有 controller 契约：

- `BaseAdapter`：生命周期、能力、metadata、发送接口。
- `AdapterToolSpec`：adapter 向后脑 ToolManager 暴露平台 API 的标准描述。
- `AdapterEvent`：标准输入事件，可转为 dict context。
- `MessageSegment` / `AdapterMessage`：通用消息段，可序列化为 `[image: ...]` 等现有标记。
- `<platform>/metadata.json`：说明真实平台、协议、限制和隐私提示。
- `<platform>/PROMPT.md`：平台专属格式和限制。

`metadata.json` 里顶层 `description` 描述 adapter 职责；`platform.description` 只描述真实平台本身是什么。`platform.name` 和 `platform.description` 会作为当前平台事实注入给 Nora，平台行为和隐私边界放在 `notes`、`privacy_notes` 或平台 prompt 中。

## Prompt 分层

`brain.prompts.load_adapter_prompt(platform)` 按顺序注入：

1. `adapters/PROMPT.md`：通用 adapter 协议。
2. `adapters/<platform>/metadata.json` 中的 `platform.name` 与 `platform.description`：当前真实平台事实。
3. `adapters/<platform>/PROMPT.md`：平台专属协议。

平台 prompt 不应重写人格，只描述平台格式、长度、命令、媒体和公开场景限制。

## Adapter Tools

Adapter 可以覆盖 `get_adapter_tools()` 返回 `AdapterToolSpec` 列表，让后脑通过统一 `ToolManager` 调用真实平台 API。工具名使用 `<platform>_<action>`；返回值应是结构化文本或 JSON，便于模型二次理解。

风险约定：

- `low`：只读查询，例如群成员总数、管理员列表、单个成员状态。
- `medium`：轻微副作用或可恢复操作。
- `high`：会影响真实用户或公开场景的操作，例如禁言、踢出、封禁、修改 tag/头衔。

高风险工具只靠 prompt 进行口头确认约束：Nora 必须先向主人确认目标、动作和参数；如果 `security.enabled=true`，工具调用还会经过现有安全审查模型。Adapter 层不判断主人/访客关系，这仍由身份模型和 prompt 共同约束。

## Telegram 示例

Telegram adapter 对外仍是 `adapters.telegram.TelegramAdapter`，内部已拆成多个职责模块：

- `main.py`：组合类、生命周期、handler 注册。
- `incoming.py`：输入消息、媒体组、群聊响应判断。
- `sender.py`：文本/媒体发送、typing、编辑、删除。
- `reply.py`：reply 与历史媒体回查。
- `tools.py`：后脑可调用的 Telegram Bot API 工具。
- `commands.py` / `callbacks.py` / `cleanup.py`：平台命令、按钮和危险清理。
- `formatting.py` / `constants.py` / `message.py`：格式化、限制、标准事件辅助。

Telegram Bot API 不支持枚举完整普通成员名单；当前工具只提供成员总数、管理员列表、按已知 `user_id` 查询单个成员。禁言、踢出/封禁、解封、设置管理员头衔、设置普通成员 tag 是否成功取决于机器人在群里的管理员权限，其中 `setChatMemberTag` 需要 Bot API 9.5 和 `can_manage_tags`。

新增平台时优先复制这种结构，而不是把所有逻辑堆进一个 `main.py`。
