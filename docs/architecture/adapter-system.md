# Adapter 系统

NORA 的 adapter 是平台通信层：它负责把真实平台事件转换为核心 controller 能理解的标准 context，也负责把核心回复转换成平台消息。

## 设计边界

- Adapter 不决定 Nora 的人格、记忆作用域或主人/访客关系。
- Adapter 只描述平台事实：消息来源、发送目标、平台 message_id、群聊/私聊类型、媒体路径、平台能力。
- 跨平台连续性由 `core/conversation_identity.py`、消息历史和 prompt 注入统一处理。

## 轻量分层

参考 NoneBot2 的 Adapter/Event/Message 思想，但保持 NORA 现有 controller 契约：

- `BaseAdapter`：生命周期、能力、metadata、发送接口。
- `AdapterEvent`：标准输入事件，可转为 dict context。
- `MessageSegment` / `AdapterMessage`：通用消息段，可序列化为 `[image: ...]` 等现有标记。
- `<platform>/metadata.json`：说明真实平台、协议、限制和隐私提示。
- `<platform>/PROMPT.md`：平台专属格式和限制。

## Prompt 分层

`brain.prompts.load_adapter_prompt(platform)` 按顺序注入：

1. `adapters/PROMPT.md`：通用 adapter 协议。
2. `adapters/<platform>/PROMPT.md`：平台专属协议。

平台 prompt 不应重写人格，只描述平台格式、长度、命令、媒体和公开场景限制。

## Telegram 示例

Telegram adapter 对外仍是 `adapters.telegram.TelegramAdapter`，内部已拆成多个职责模块：

- `main.py`：组合类、生命周期、handler 注册。
- `incoming.py`：输入消息、媒体组、群聊响应判断。
- `sender.py`：文本/媒体发送、typing、编辑、删除。
- `reply.py`：reply 与历史媒体回查。
- `commands.py` / `callbacks.py` / `cleanup.py`：平台命令、按钮和危险清理。
- `formatting.py` / `constants.py` / `message.py`：格式化、限制、标准事件辅助。

新增平台时优先复制这种结构，而不是把所有逻辑堆进一个 `main.py`。
