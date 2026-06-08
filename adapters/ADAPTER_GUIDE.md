# Adapter 开发指南

本指南面向在 `adapters/` 下新增平台适配器的开发者。NORA 的 adapter 参考 NoneBot2 的 `Adapter / Event / Message` 分层思想，但保持轻量：不引入完整 driver、Bot API hook 或 pydantic 事件体系。

## 1. 组成

- `BaseAdapter`：平台连接、生命周期、能力描述、消息发送。
- `AdapterEvent`：平台输入事件的标准结构，最终转成 controller 兼容的 dict context。
- `MessageSegment` / `AdapterMessage`：轻量消息段，支持文本和媒体，并能序列化为 NORA 现有 `[image: ...]` / `[file: ...]` 标记。
- `metadata.json`：适配器清单，必须说明对接的真实平台、协议、能力、限制与隐私提示。
- `PROMPT.md`：平台专属提示词，只写平台格式和限制；通用人格/媒体协议在 `adapters/PROMPT.md`。

## 2. 必需接口

每个 adapter 子类必须实现：

- `run(message_handler)`
- `send_message(chat_id, text, **kwargs) -> list[str]`
- `start_typing(chat_id)`
- `stop_typing(chat_id)`

可选覆盖：

- `platform_name`
- `platform_features`
- `get_adapter_tools()`
- `edit_message(...)`
- `delete_message(...)`
- `on_startup()` / `on_ready()` / `on_shutdown()`
- `before_message(context)` / `after_message(context, result)`

`send_message()` 必须返回平台消息 ID 列表，即使实际只发送了一条消息。

### Adapter Tools

Adapter 可以通过 `get_adapter_tools()` 向后脑暴露平台能力。返回值是 `AdapterToolSpec` 列表：

- `name`：工具名，建议用 `<platform>_<action>`，如 `telegram_get_chat_member`。
- `callable`：同步或异步可调用对象，docstring 会生成工具 schema。
- `intro`：一句话能力说明，会注入前脑/后脑。
- `risk`：`low` / `medium` / `high`，供文档、安全策略和审计理解。
- `requires_owner_confirmation`：真实平台副作用工具设为 `true`。
- `platform`：平台名。

高风险工具不要加入默认 `security.tool_whitelist`。如果 `security.enabled=true`，这些工具会继续经过安全审查模型。对于禁言、踢人、封禁、改群员 tag/头衔等真实平台副作用，prompt 必须要求 Nora 先向主人口头确认目标、动作和参数。

## 3. 推荐目录结构

```text
adapters/<platform>/
├─ __init__.py
├─ main.py          # 对外 Adapter 类
├─ message.py       # 平台事件 -> AdapterEvent / AdapterMessage
├─ sender.py        # 发送、编辑、删除、typing
├─ constants.py     # 平台限制、媒体类型
├─ metadata.json    # 必需
└─ PROMPT.md        # 平台专属提示词
```

复杂平台可以继续拆 `commands.py`、`callbacks.py`、`reply.py`、`config.py`、`exception.py` 等。Telegram adapter 就是这种渐进拆分示例。

## 4. Context 契约

传给 controller 的 context 至少包含：

- `platform`: str
- `chat_id`: str
- `user_id`: str
- `text`: str
- `chat_type`: str，如 `private` / `group`
- `user_name`: str

建议包含：

- `platform_message_id`: str，用于 reply、undo、媒体回查。
- `platform_message_ids`: list[str]，聚合多条平台消息时使用。
- `contributors`: list[dict]，群聊聚合多说话人时使用。
- `reply_to_user_id` / `reply_to_user_name` / `reply_to_message_id`: reply 场景的被回复目标，便于平台工具定位操作对象。

不要让 adapter 自行决定人格、记忆作用域或主人/访客关系；这些由 `core/conversation_identity.py` 统一处理。

## 5. Metadata 清单

每个 adapter 目录必须提供 `metadata.json`：

```json
{
  "adapter_id": "telegram",
  "name": "Telegram Adapter",
  "version": "0.1.0",
  "author": "CaptainCN (WR) + Nora",
  "entrypoint": "adapters.telegram:TelegramAdapter",
  "description": "适配器说明",
  "platform": {
    "name": "Telegram",
    "type": "instant_messaging",
    "protocol": "Telegram Bot API",
    "official_website": "https://telegram.org/",
    "description": "真实平台说明",
    "notes": ["平台行为提示"],
    "privacy_notes": ["隐私/群聊边界提示"]
  },
  "features": {
    "supports_rich_media": true,
    "supports_buttons": true
  },
  "limits": {
    "max_message_length": 4096
  }
}
```

字段边界：

- 顶层 `description` 描述这个 adapter 在 NORA 里的职责，例如消息接收、发送、媒体下载、命令按钮等。
- `platform.name` 是对接的真实平台名称，会随平台描述一起注入给 Nora。
- `platform.description` 只描述“这是一个什么平台”，不要写“本适配器如何对接”、Nora 人格、响应策略或实现细节。
- 平台行为限制、群聊边界、安全隐私提示分别放进 `notes` / `privacy_notes` / `limits`，不要塞进 `platform.description`。

## 6. Prompt 分层

- `adapters/PROMPT.md`：所有平台共享，描述跨平台人格连续性、通用媒体标记、`[SPLIT]`、不泄漏内部信息。
- `metadata.json` 中的 `platform.name` 与 `platform.description`：自动注入给 Nora，让她知道当前对话发生在什么真实平台。
- `adapters/<platform>/PROMPT.md`：只描述该平台的格式、长度、命令、群聊/私聊差异和媒体发送限制。
- 不要在平台 prompt 中重写 Nora 人格；人格边界在 `SOUL.md` 和 `brain/templates/system.jinja`。

## 7. 最小模板

```python
from adapters.base import AdapterEvent, BaseAdapter, PlatformFeatures


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

    async def on_platform_message(self, raw):
        event = AdapterEvent(
            platform=self.platform_name,
            chat_id=str(raw.chat_id),
            user_id=str(raw.user_id),
            text=raw.text,
            chat_type=raw.chat_type,
            user_name=raw.user_name,
            platform_message_id=str(raw.message_id),
            raw=raw,
        )
        await self.dispatch_message(event)

    async def send_message(self, chat_id: str, text: str, **kwargs) -> list[str]:
        message_id = await real_platform_send(chat_id, text)
        return [str(message_id)]

    async def start_typing(self, chat_id: str):
        return None

    async def stop_typing(self, chat_id: str):
        return None
```

## 8. Telegram 群管工具限制

Telegram Bot API 不支持枚举完整普通成员名单，因此“成员名单”能力只能实现为：

- `telegram_get_chat_member_count`：成员总数。
- `telegram_get_chat_administrators`：管理员列表。
- `telegram_get_chat_member`：按已知 `user_id` 查询单个成员。

禁言、踢出/封禁、解封、设置管理员头衔、设置普通成员 tag 依赖机器人管理员权限。`setChatMemberTag` 是 Bot API 9.5 能力，如果当前 `python-telegram-bot` 版本没有封装方法，可以使用 `Bot.do_api_request("setChatMemberTag", ...)`。

## 9. 自检清单

- [ ] `run()` 保存了 `message_handler`。
- [ ] 已在正确时机触发 `startup/on_ready/shutdown`。
- [ ] `metadata.json` 说明了真实平台、能力、限制和隐私提示。
- [ ] 平台事件通过 `AdapterEvent` 或等价 dict 输出统一字段。
- [ ] `send_message()` 返回稳定的消息 ID 列表。
- [ ] `get_adapter_tools()` 暴露的工具命名、docstring、风险等级和返回格式清晰。
- [ ] 群聊响应条件、平台 message_id、媒体路径和撤回能力有测试或手动验证。
