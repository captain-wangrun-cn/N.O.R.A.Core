"""「当前时间」的注入位置与时区。

背景（这条曾经是错的）：
    时间原本写在 system prompt 尾部的【系统环境信息】块里。两个问题：

    1. **缓存**：prompt 拼装顺序是 system → history → 当前 user 消息，缓存按前缀
       匹配。时间放在 system 里，每轮都让整个前缀失效；放在最后一条 user 消息里，
       前面那段稳定前缀原样命中，而这条消息本来每轮就是新的，边际成本为零。
    2. **准确**：system 是整个 prompt 最靠前的位置，中间还隔着一长串历史，模型要
       跨过全部对话才能读到它。语义上它描述的应该是"这条消息所在的此刻"。

    另有独立的时区 bug：那一行用裸 `datetime.now()`（机器本地时区），而项目里
    别的时间路径（消息前缀、调度器、日志）都走 `memory.message_history.timezone`。

与之配套的另一半在 `tests/test_current_message_dedup_e2e.py`：历史消息**保留**
各自的时间戳前缀（过去），本文件管"现在"，任何时刻整个 prompt 里只有一处"现在"。
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import config
from core.controller import NoraController


class _Host(NoraController):
    """只挂 _chat_stream_wrapper 需要的最小状态，跳过重量级 __init__。"""

    def __init__(self):
        self.adapter = SimpleNamespace(platform_name="testplat")
        self.non_stream_flags = {}
        self.default_non_stream = False


class _FakeModelClient:
    def __init__(self):
        self.calls = []

    def chat_stream(self, **kwargs):
        self.calls.append(kwargs)

        async def _gen():
            yield {"type": "text", "content": "好"}

        return _gen()


def _collect(agen):
    async def _main():
        return [chunk async for chunk in agen]

    return asyncio.run(_main())


def _parse_stamp(text: str) -> datetime:
    """取出时间戳里的 `YYYY-MM-DD HH:MM:SS`。

    不解析尾部的星期字段：`%A` 的输出随 locale 变（可能是 `Wednesday` 也可能是
    `星期三`），直接 strptime 会因环境不同而失败。
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    assert m, f"时间戳格式不对: {text!r}"
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")


# --- 位置：必须在 user_prompt，不能在 system ---

def test_current_time_goes_into_user_prompt():
    host = _Host()
    client = _FakeModelClient()
    _collect(host._chat_stream_wrapper(client, "chat-1", system_prompt="SYS", user_prompt="你好"))

    sent = client.calls[0]["user_prompt"]
    assert NoraController._CURRENT_TIME_MARKER in sent
    assert "你好" in sent


def test_current_time_is_at_the_head_of_user_prompt():
    """放在最前面：这是模型最后读到的位置的起点，不会被正文隔开。"""
    host = _Host()
    sent = host.append_current_time_to_user_prompt("你好")
    assert sent.lstrip().startswith(NoraController._CURRENT_TIME_MARKER)


def test_current_time_is_not_in_system_env_block():
    """回归锁：时间不能再回到 system 里——那是会作废整个缓存前缀的位置。"""
    host = _Host()
    client = _FakeModelClient()
    _collect(host._chat_stream_wrapper(client, "chat-1", system_prompt="SYS", user_prompt="你好"))

    system_prompt = client.calls[0]["system_prompt"]
    assert NoraController._SYSTEM_ENV_MARKER in system_prompt  # 环境块本身还在
    assert "当前时间" not in system_prompt
    assert not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", system_prompt)
    # 稳定项仍然留在 system（可随缓存前缀命中）
    assert "Chat ID: chat-1" in system_prompt
    assert "testplat" in system_prompt


def test_current_time_injection_is_idempotent():
    """重复包裹不能叠加出两个"现在"。"""
    host = _Host()
    once = host.append_current_time_to_user_prompt("你好")
    twice = host.append_current_time_to_user_prompt(once)
    assert once == twice
    assert twice.count(NoraController._CURRENT_TIME_MARKER) == 1


def test_empty_user_prompt_is_not_stamped():
    """没有 user_prompt 的调用方（如只喂 history 的轮次）不该被硬塞一个时间块。"""
    host = _Host()
    client = _FakeModelClient()
    _collect(host._chat_stream_wrapper(client, "chat-1", system_prompt="SYS", user_prompt=""))
    assert client.calls[0]["user_prompt"] == ""


# --- 时区：必须跟配置走，不能用机器本地时区 ---

def test_current_time_follows_configured_timezone(monkeypatch):
    """判据：产出的墙上时间与 UTC 之差，应等于配置时区的偏移。

    若实现退回裸 `datetime.now()`（机器本地时区），只有在本机恰好就是配置时区时
    才碰巧通过；换一个时区就会失败。
    """
    cases = {"Asia/Shanghai": timedelta(hours=8), "UTC": timedelta(0)}
    for tz_name, offset in cases.items():
        monkeypatch.setattr(
            config, "get_message_history_config", lambda tz=tz_name: {"timezone": tz}
        )
        parsed = _parse_stamp(NoraController._current_time_text())
        delta = datetime.now(timezone.utc).replace(tzinfo=None) - parsed
        assert abs(delta + offset) < timedelta(minutes=2), (
            f"{tz_name}: 时间戳未按配置时区生成（与 UTC 相差 {delta}，应为 {-offset}）"
        )


def test_current_time_falls_back_when_timezone_invalid(monkeypatch):
    """坏时区名不能抛异常——回退系统本地时区，照常产出可读时间。"""
    monkeypatch.setattr(
        config, "get_message_history_config", lambda: {"timezone": "Not/AZone"}
    )
    text = NoraController._current_time_text()
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", text)
