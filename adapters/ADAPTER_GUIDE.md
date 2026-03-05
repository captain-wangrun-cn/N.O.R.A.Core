# Adapter 开发指南

本指南面向在 `adapters/` 下新增平台适配器的开发者。

## 1. 目标

`BaseAdapter` 现在只保留“最小消息收发能力 + 可扩展生命周期钩子”：

- 最小必须实现：
  - `run(message_handler)`
  - `send_message(chat_id, text, **kwargs)`
  - `start_typing(chat_id)`
  - `stop_typing(chat_id)`
- 可选覆盖：
  - `platform_name`
  - `platform_features`
  - `edit_message(...)`
  - `delete_message(...)`
  - `on_startup()` / `on_ready()` / `on_shutdown()`
  - `before_message(context)` / `after_message(context, result)`

## 2. 推荐目录结构

建议每个平台用一个子目录：

- `adapters/<platform>/__init__.py`
- `adapters/<platform>/main.py`
- `adapters/<platform>/PROMPT.md`（可选，平台提示词）

## 3. 最小实现模板

```python
from typing import Any, Dict
from adapters.base import BaseAdapter, PlatformFeatures


class XxxAdapter(BaseAdapter):
    @property
    def platform_name(self) -> str:
        return "xxx"

    @property
    def platform_features(self) -> PlatformFeatures:
        return PlatformFeatures(
            supports_typing_indicator=True,
            supports_rich_media=False,
            max_message_length=2000,
        )

    def run(self, message_handler):
        self._message_handler = message_handler
        # 启动平台 SDK / 事件循环
        # 在适当时机调用 startup() / on_ready()

    async def send_message(self, chat_id: str, text: str, **kwargs) -> str:
        # 实际发送，返回 message_id
        return "message-id"

    async def start_typing(self, chat_id: str):
        return None

    async def stop_typing(self, chat_id: str):
        return None
```

## 4. 生命周期建议

- 连接建立后调用：`await self.startup()`
- 平台信息准备就绪后调用：`await self.on_ready()`
- 进程退出或 SDK 停止时调用：`await self.shutdown()`

说明：

- `startup()` / `shutdown()` 会维护 `is_running` 状态。
- 如需自定义启动/关闭逻辑，请覆盖 `on_startup()` / `on_shutdown()`，而不是直接改状态位。

## 5. 消息钩子建议

在把事件转发给控制器前后使用钩子：

1. `processed = await self.before_message(context)`
2. `result = await self._message_handler(processed)`
3. `await self.after_message(processed, result)`

适用场景：

- 统一清洗输入（mention、reply 格式化）
- 注入平台元数据
- 埋点统计与审计日志

## 6. 与 Controller 的契约

传递给 `message_handler` 的 `context` 建议至少包含：

- `chat_id`: str
- `user_id`: str
- `text`: str
- `chat_type`: str（如 `private` / `group`）
- `user_name`: str

## 7. 开发约定（来自 onboarding）

- 使用 `logging`，避免 `print`
- 使用类型标注，尤其是公开方法
- 导入顺序：标准库 → 第三方 → 项目内部
- 改动适配器后，补充对应测试

## 8. 自检清单

- [ ] `run()` 已保存 `message_handler`
- [ ] 已在正确时机触发 `startup/on_ready/shutdown`
- [ ] `send_message()` 返回稳定的消息 ID 字符串
- [ ] `before_message/after_message` 不会吞异常（必要时记录日志）
- [ ] `platform_features` 与实际能力一致
