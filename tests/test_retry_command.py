import inspect
import re
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from adapters.qqbot.commands import _OWNER_COMMANDS
from adapters.telegram.main import TelegramAdapter
from core.message_handler import MessageHandlerMixin, RETRY_PLACEHOLDER_TEXT


def test_retry_command_handler_is_registered():
    """验证 Telegram CommandHandler 注册了 /retry。"""
    source = inspect.getsource(TelegramAdapter._configure_application)
    assert "CommandHandler('retry'" in source


def test_retry_bot_command_menu_entry():
    """验证 Telegram 命令菜单中包含了 /retry。"""
    source = inspect.getsource(TelegramAdapter._register_bot_commands)
    assert 'BotCommand("retry"' in source


def test_retry_command_exists_on_adapter():
    """验证 TelegramAdapter 上存在 _retry_command 方法。"""
    assert callable(getattr(TelegramAdapter, "_retry_command", None))


def test_retry_in_qqbot_owner_commands():
    """验证 QQBot _OWNER_COMMANDS 中包含 retry。"""
    assert "retry" in _OWNER_COMMANDS


def test_retry_message_handler_dispatch():
    """验证 MessageHandlerMixin 中存在 /retry 分发逻辑和 _cmd_retry 方法。"""
    assert hasattr(MessageHandlerMixin, "_cmd_retry")
    source = inspect.getsource(MessageHandlerMixin.handle_new_message)
    assert r"/retry\b" in source
    assert "_cmd_retry" in source


def test_placeholder_text_non_empty():
    """验证占位符非空，避免空消息静默处理。"""
    assert RETRY_PLACEHOLDER_TEXT.strip()


class _DummyController(MessageHandlerMixin):
    def __init__(self):
        self.sent_messages = []
        self.replayed_messages = []
        self.sessions = {}
        self.message_history = AsyncMock()

    async def _send_platform_message(self, chat_id: str, text: str, **kwargs) -> Any:
        self.sent_messages.append((chat_id, text))
        return ["msg_1"]

    async def handle_new_message(self, context: Dict[str, Any]) -> None:
        self.replayed_messages.append(context)


@pytest.mark.anyio
async def test_cmd_retry_no_assistant_message():
    controller = _DummyController()
    controller._undo_last_assistant = AsyncMock(return_value=None)

    context = {"text": "/retry", "chat_id": "test_chat"}
    await controller._cmd_retry(context, "test_chat", "private", "user_1")

    assert controller._undo_last_assistant.called
    assert len(controller.sent_messages) == 1
    assert "没有可重新生成的回复" in controller.sent_messages[0][1]
    assert len(controller.replayed_messages) == 0


@pytest.mark.anyio
async def test_cmd_retry_success():
    controller = _DummyController()
    controller._undo_last_assistant = AsyncMock(
        return_value={"delete_attempted": True, "delete_ok": True}
    )

    context = {
        "text": "/retry",
        "chat_id": "test_chat",
        "multimodal_images": [{"image_id": "img1"}],
        "multimodal_videos": [{"video_id": "vid1"}],
        "image_load_failed": True,
        "_persisted_user_message_ids": [123],
    }
    await controller._cmd_retry(context, "test_chat", "private", "user_1")

    assert controller._undo_last_assistant.called
    assert len(controller.replayed_messages) == 1
    replay = controller.replayed_messages[0]
    assert replay["text"] == RETRY_PLACEHOLDER_TEXT
    assert replay.get("_retry_replay") is True
    assert "multimodal_images" not in replay
    assert "multimodal_videos" not in replay
    assert "image_load_failed" not in replay
    assert "_persisted_user_message_ids" not in replay
    # 验证没有设置 _override_grant
    assert "_override_grant" not in controller.sessions.get("test_chat", {})


@pytest.mark.anyio
async def test_cmd_retry_with_custom_prompt():
    controller = _DummyController()
    controller._undo_last_assistant = AsyncMock(
        return_value={"delete_attempted": False, "delete_ok": False}
    )

    context = {"text": "/retry 换个更幽默的语气", "chat_id": "test_chat"}
    await controller._cmd_retry(context, "test_chat", "private", "user_1")

    assert len(controller.replayed_messages) == 1
    replay = controller.replayed_messages[0]
    assert "换个更幽默的语气" in replay["text"]
    assert replay["text"].startswith("[系统：重新生成上一条回复。补充要求：")
    assert replay.get("_retry_replay") is True


def test_clean_command_text_onebotv11_cases():
    """验证 _clean_command_text 正确剥离 OneBot v11 各种入站引用与控制标记。"""
    from core.message_handler import _clean_command_text

    # 1. 裸指令
    assert _clean_command_text("/retry") == "/retry"
    assert _clean_command_text("  /retry  ") == "/retry"
    assert _clean_command_text("/retry 换个语气") == "/retry 换个语气"

    # 2. 带 OneBot 引用标记（[reply:...]）
    assert _clean_command_text("[reply:12345] /retry") == "/retry"
    assert _clean_command_text("[reply:12345]/retry") == "/retry"
    assert _clean_command_text("[reply:12345] /retry 换个语气") == "/retry 换个语气"

    # 3. 带 [回复内容] 区块
    ob11_reply_block = "[reply:12345][回复内容]\n历史消息原文\n[/回复内容]\n/retry"
    assert _clean_command_text(ob11_reply_block) == "/retry"

    ob11_reply_block_args = "[reply:12345][回复内容]\n历史消息原文\n[/回复内容]\n/retry 简短一点"
    assert _clean_command_text(ob11_reply_block_args) == "/retry 简短一点"

    # 4. 带提及标记（[at:...]）或回复消息ID
    assert _clean_command_text("[reply:12345][at:67890] /retry") == "/retry"
    assert _clean_command_text("[回复消息ID: 12345]/retry") == "/retry"

    # 5. 非指令消息不破坏原有内容
    assert _clean_command_text("[reply:12345] 好的收到") == "[reply:12345] 好的收到"


@pytest.mark.anyio
async def test_cmd_retry_onebotv11_quoted_message_dispatch():
    """验证 OneBot v11 场景下通过引用回复发送 /retry 能够正确触发重新生成。"""
    controller = _DummyController()
    controller._undo_last_assistant = AsyncMock(
        return_value={"delete_attempted": True, "delete_ok": True}
    )

    # 模拟 OneBot v11 带引用标记和回复区块的消息上下文
    context = {
        "text": "[reply:99999][回复内容]\n这是被引用的AI历史回复\n[/回复内容]\n/retry 换个更生动的说法",
        "chat_id": "onebotv11:10001",
    }
    await controller._cmd_retry(context, "onebotv11:10001", "group", "user_1")

    assert controller._undo_last_assistant.called
    assert len(controller.replayed_messages) == 1
    replay = controller.replayed_messages[0]
    assert "换个更生动的说法" in replay["text"]
    assert replay.get("_retry_replay") is True

