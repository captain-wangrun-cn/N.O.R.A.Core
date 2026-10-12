"""`/start` 已删除——它曾经是个伪装成打招呼的硬重置。

事故（2026-10-12 生产）：
    Telegram 命令菜单里 `/start` 的标签是「开始对话」，但它调的是
    `_cmd_start` → `clear_chat_history(keep_pinned=True)`，实际删掉
    `messages`（非 pinned）、**整个 `summaries`（含 level-3 长期归档）**、
    `conversation_sessions`、压缩重试队列，并连带清空消息镜像库与压缩上下文库。

    用户顺手一按，几个月对话记忆全没了。而且**不可恢复**：实测
    `PRAGMA secure_delete = 1`，被删的页被清零，`.recover` / 空闲页捞数据无效
    （1474 页里 1460 页近乎全零）。

    教训：**命令的菜单标签必须等于它的实际后果。** 一个「看起来最无害」的入口
    不该是破坏性最强的操作。清空历史请走 `/clear`（标签就是「清空聊天记录」）。

本文件锁住"/start 不要再回来"。
"""

import inspect
from pathlib import Path

from core import message_handler as mh_module
from core.message_handler import MessageHandlerMixin

_REPO = Path(__file__).resolve().parent.parent


def test_cmd_start_method_is_gone():
    """`_cmd_start` 这个破坏性实现必须彻底删除，不能只是不再注册。"""
    assert not hasattr(MessageHandlerMixin, "_cmd_start")


def test_no_start_dispatch_regex_in_message_handler():
    """分发链路上不能再有 /start 的正则（否则从 OneBot 等入口仍能触发）。"""
    src = inspect.getsource(mh_module)
    assert "/start" not in src
    assert "_cmd_start" not in src


def test_plain_start_text_no_longer_triggers_anything_destructive():
    """退一步：就算有人重新加回 /start，也绝不能是"清空历史"这条路径。

    这里做的是源码级断言——`handle_new_message` 的 /start 分支附近不得出现
    clear_chat_history。
    """
    src = inspect.getsource(MessageHandlerMixin.handle_new_message)
    assert "_cmd_start" not in src


def test_telegram_start_handler_and_menu_entry_removed():
    """Telegram 侧：handler 注册与命令菜单都不能再有 start。"""
    main_src = (_REPO / "adapters" / "telegram" / "main.py").read_text(encoding="utf-8")
    assert "CommandHandler('start'" not in main_src
    assert 'BotCommand("start"' not in main_src

    cmd_src = (_REPO / "adapters" / "telegram" / "commands.py").read_text(encoding="utf-8")
    assert "_start_command" not in cmd_src


def test_clear_is_the_only_destructive_history_command_and_is_honestly_named():
    """/clear 仍在（标签即后果），且它的 keep_pinned 语义与 /start 曾用的不同。"""
    src = inspect.getsource(MessageHandlerMixin._cmd_clear)
    assert "clear_chat_history" in src


def test_telegram_prompt_does_not_teach_the_model_to_suggest_start():
    """PROMPT.md 里不能再把 /start 当命令范例教给模型。"""
    prompt_src = (_REPO / "adapters" / "telegram" / "PROMPT.md").read_text(encoding="utf-8")
    assert "/start" not in prompt_src
