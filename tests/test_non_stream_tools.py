"""非流式 chat() 必须把工具调用带回来（后脑工具循环的命门）。

背景（这是真实踩过的坑）：
    后脑开了非流式（`interaction.non_stream` 或 `/set_stream on`）后，
    `_chat_stream_wrapper` 会走 `model_client.chat()`。而 `chat()` 的返回类型是 str，
    历史上只取 `message.content`、完全无视 `message.tool_calls`。模型只调工具时
    content 恰好是 None/""，于是：

        finish_reason=tool_calls  →  日志"返回内容为空"  →  last_error="empty_content"
                                  →  返回 ""            →  后脑工具循环拿到空回直接结束

    用户侧的观感是"后脑什么都不做"，而模型其实每次都正确地发起了工具调用。
    流式路径（chat_stream）一直是好的，所以只有开了非流式的会话会犯病。

修复：provider 把工具调用存进 `last_tool_calls`，`_chat_stream_wrapper` 的非流式
分支翻译成与 chat_stream 同款的 chunk 序列。
"""
import asyncio
import inspect
import json
from types import SimpleNamespace

import config
from brain.providers.openai import OpenAIProvider
from core import message_handler as message_handler_module
from core.controller import NoraController
from core import non_stream_store as nss_module
from core.non_stream_store import (
    get_non_stream_flag,
    load_non_stream_flags,
    set_non_stream_flag,
)


# ---------------------------------------------------------------------------
# OpenAIProvider.chat()：摘出 tool_calls
# ---------------------------------------------------------------------------

def _make_provider(response, *, use_responses_api: bool = False):
    """构造一个绕过 __init__ 的 provider，只需要 chat() 用到的属性。"""

    class _FakeCompletions:
        async def create(self, **kwargs):
            return response

    class _FakeResponses:
        async def create(self, **kwargs):
            return response

    provider = object.__new__(OpenAIProvider)
    provider.use_responses_api = use_responses_api
    provider.model = "test-model"
    provider.model_alias = "coder"
    provider.max_output_tokens = None
    provider.last_usage = None
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions()),
        responses=_FakeResponses(),
    )
    provider._with_retry = lambda fn, max_retries=3, base_delay=1.0: fn()
    return provider


def _tool_call_response(tool_name: str, args: dict, content=None):
    """构造一个"只调工具、没有正文"的 chat.completions 返回体。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=content,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_abc",
                            type="function",
                            function=SimpleNamespace(
                                name=tool_name, arguments=json.dumps(args)
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )


def test_chat_returns_tool_call_instead_of_calling_it_empty():
    """核心回归：finish_reason=tool_calls + content=None 不能再被当成"空回"。"""
    provider = _make_provider(
        _tool_call_response("exec_command", {"command": "yt-dlp https://x"})
    )

    text = asyncio.run(provider.chat("sys", "下载视频", [], tools=[{"name": "exec_command"}]))

    assert text == ""  # 正文确实为空
    assert provider.last_error is None, "有工具调用就不是空回，不能置 empty_content"
    assert provider.last_tool_calls == [
        {
            "name": "exec_command",
            "args": {"command": "yt-dlp https://x"},
            "id": "call_abc",
        }
    ]


def test_chat_still_flags_genuinely_empty_response():
    """没有工具调用、也没有正文，才是真的空回——这个信号不能被顺手改掉。"""
    provider = _make_provider(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="", tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=0),
        )
    )

    text = asyncio.run(provider.chat("sys", "hi", []))

    assert text == ""
    assert provider.last_error == "empty_content"
    assert provider.last_tool_calls == []


def test_chat_keeps_text_alongside_tool_call():
    """模型"边说边调"时正文要保留，工具调用也要带上。"""
    provider = _make_provider(
        _tool_call_response("get_available_skills", {}, content="我先查一下有什么技能")
    )

    text = asyncio.run(provider.chat("sys", "你能干啥", []))

    assert text == "我先查一下有什么技能"
    assert provider.last_error is None
    assert [c["name"] for c in provider.last_tool_calls] == ["get_available_skills"]


def test_chat_unparseable_tool_args_are_preserved_not_dropped():
    """arguments 不是合法 JSON 时保留原文，不能把这次调用整个丢掉。"""
    provider = _make_provider(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call_x",
                                function=SimpleNamespace(
                                    name="exec_command", arguments="{not json"
                                ),
                            )
                        ],
                    ),
                )
            ],
            usage=None,
        )
    )

    asyncio.run(provider.chat("sys", "x", []))

    assert len(provider.last_tool_calls) == 1
    assert provider.last_tool_calls[0]["args"] == {"raw": "{not json"}


def test_chat_responses_api_extracts_function_calls():
    """Responses API 的非流式分支同样要摘出 function_call（GPT-5.4+ 带工具走这条）。"""
    provider = _make_provider(
        SimpleNamespace(
            output_text="",
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="exec_command",
                    arguments='{"command": "ls"}',
                    call_id="call_resp_1",
                )
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=3),
        ),
        use_responses_api=True,
    )

    text = asyncio.run(provider.chat("sys", "ls 一下", [], tools=[{"name": "exec_command"}]))

    assert text == ""
    assert provider.last_error is None
    assert provider.last_tool_calls == [
        {"name": "exec_command", "args": {"command": "ls"}, "id": "call_resp_1"}
    ]


# ---------------------------------------------------------------------------
# _chat_stream_wrapper：非流式分支要产出 tool_call chunk
# ---------------------------------------------------------------------------

class _Host(NoraController):
    """只挂 _chat_stream_wrapper 需要的最小状态，跳过重量级 __init__。"""

    def __init__(self, non_stream: bool = True):
        self.adapter = SimpleNamespace(platform_name="testplat")
        self.non_stream_flags = {"chat-1": non_stream}
        self.default_non_stream = non_stream


class _FakeNonStreamClient:
    """模拟 provider 的非流式 chat()：正文为空、只带一个工具调用。"""

    def __init__(self):
        self.last_tool_calls = [
            {"name": "exec_command", "args": {"command": "ls"}, "id": "call_1"}
        ]
        self.last_usage = {"input_tokens": 11, "output_tokens": 7}
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(self, **kwargs):
        self.chat_calls += 1
        return ""

    def chat_stream(self, **kwargs):
        self.stream_calls += 1

        async def _gen():
            yield {"type": "text", "content": "streamed"}

        return _gen()


def _collect(agen):
    async def _main():
        return [chunk async for chunk in agen]

    return asyncio.run(_main())


def test_non_stream_wrapper_yields_tool_call_chunk():
    """非流式下后脑必须能收到 tool_call，否则整轮工具循环直接空转结束。"""
    host = _Host(non_stream=True)
    client = _FakeNonStreamClient()

    chunks = _collect(
        host._chat_stream_wrapper(client, "chat-1", system_prompt="SYS", user_prompt="下载视频")
    )

    assert client.chat_calls == 1
    assert client.stream_calls == 0
    tool_chunks = [c for c in chunks if c["type"] == "tool_call"]
    assert tool_chunks == [
        {"type": "tool_call", "name": "exec_command", "args": {"command": "ls"}}
    ]
    usage_chunks = [c for c in chunks if c["type"] == "usage"]
    assert usage_chunks == [
        {"type": "usage", "input_tokens": 11, "output_tokens": 7}
    ]


def test_non_stream_wrapper_yields_text_when_present():
    """有正文时照旧产出 text chunk（顺序与 chat_stream 一致：text → tool_call）。"""

    class _Client(_FakeNonStreamClient):
        async def chat(self, **kwargs):
            return "我查到了"

    host = _Host(non_stream=True)
    chunks = _collect(
        host._chat_stream_wrapper(_Client(), "chat-1", system_prompt="SYS", user_prompt="x")
    )

    assert [c["type"] for c in chunks] == ["text", "tool_call", "usage"]
    assert chunks[0]["content"] == "我查到了"


def test_non_stream_flag_off_still_goes_through_chat_stream():
    """关掉非流式的会话不能被这次改动顺手改道。"""
    host = _Host(non_stream=False)
    client = _FakeNonStreamClient()

    chunks = _collect(
        host._chat_stream_wrapper(client, "chat-1", system_prompt="SYS", user_prompt="x")
    )

    assert client.stream_calls == 1
    assert client.chat_calls == 0
    assert chunks == [{"type": "text", "content": "streamed"}]


# ---------------------------------------------------------------------------
# /set_stream 持久化
# ---------------------------------------------------------------------------

def _use_tmp_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(
        nss_module,
        "get_workspace_manager",
        lambda: type("Workspace", (), {"data_dir": str(tmp_path)})(),
    )


def test_non_stream_flag_roundtrip(monkeypatch, tmp_path):
    """写入后重新读取（模拟重启）仍能拿到设置。"""
    _use_tmp_workspace(monkeypatch, tmp_path)

    assert set_non_stream_flag("telegram:123", True) is True

    # 模拟进程重启：模块级函数重新从盘上读
    assert get_non_stream_flag("telegram:123") is True
    assert load_non_stream_flags() == {"telegram:123": True}


def test_non_stream_unset_is_none_not_false(monkeypatch, tmp_path):
    """没设置过必须是 None，不能被当成"明确设成流式"。"""
    _use_tmp_workspace(monkeypatch, tmp_path)

    set_non_stream_flag("telegram:123", True)

    assert get_non_stream_flag("telegram:123") is True
    assert get_non_stream_flag("telegram:999") is None


def test_non_stream_flag_can_be_turned_back_off(monkeypatch, tmp_path):
    """显式设成 off 要落盘，重启后不能又变回默认值。"""
    _use_tmp_workspace(monkeypatch, tmp_path)

    set_non_stream_flag("telegram:123", True)
    set_non_stream_flag("telegram:123", False)

    assert get_non_stream_flag("telegram:123") is False
    assert load_non_stream_flags() == {"telegram:123": False}


def test_non_stream_flag_is_per_runtime_key(monkeypatch, tmp_path):
    """输出模式按端点隔离，一个会话的设置不串到别的会话。"""
    _use_tmp_workspace(monkeypatch, tmp_path)

    set_non_stream_flag("telegram:123", True)
    set_non_stream_flag("onebotv11:456", False)

    assert load_non_stream_flags() == {"telegram:123": True, "onebotv11:456": False}


def test_cmd_set_stream_persists_flag():
    """源码级接线断言：/set_stream 命令必须真的调持久化，而不只改内存 dict。"""
    src = inspect.getsource(message_handler_module.MessageHandlerMixin._cmd_set_stream)
    assert "set_non_stream_flag(" in src


def test_controller_loads_persisted_flags():
    """源码级接线断言：controller 初始化时要读回持久化的设置。"""
    src = inspect.getsource(NoraController.__init__)
    assert "load_non_stream_flags()" in src


# ---------------------------------------------------------------------------
# /nonstream 已删除
# ---------------------------------------------------------------------------

def test_nonstream_command_is_gone():
    """/nonstream 是 /set_stream 的别名，已合并删除；残留正则会重新收留这个命令。"""
    forbidden = ("/nonstream",)
    targets = [
        inspect.getsource(message_handler_module),
        inspect.getsource(NoraController),
    ]
    for source in targets:
        for token in forbidden:
            assert token not in source, f"仍残留 {token} 的处理逻辑"


def test_message_handler_module_has_no_nonstream_regex():
    """确认命令分发里没有 nonstream 正则（上面那条是按模块整体扫的兜底）。"""
    assert "nonstream\\b" not in inspect.getsource(message_handler_module)
