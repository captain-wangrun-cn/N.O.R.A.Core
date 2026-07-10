import asyncio
import re
import pytest

try:
    from core import message_handler as mh
except Exception as exc:  # pragma: no cover - 环境依赖不足时跳过
    pytest.skip(f"skip message_handler ack tests: {exc}", allow_module_level=True)


class _DummyHandler(mh.MessageHandlerMixin):
    def __init__(self, response_text: str = "DECISION:enqueue"):
        self.fast_llm = object()
        self._response_text = response_text

    def _chat_stream_wrapper(self, model_client, chat_id: str, **kwargs):
        async def _gen():
            yield {"type": "text", "content": self._response_text}

        return _gen()


def test_auto_confirm_backend_enqueue_yes(monkeypatch):
    monkeypatch.setattr(mh, "get_soul_prompt", lambda: "")
    monkeypatch.setattr(mh, "load_identity_context", lambda include_schedule=False: "")
    monkeypatch.setattr(mh, "render_template", lambda *args, **kwargs: "prompt")

    handler = _DummyHandler("DECISION:enqueue")

    result = asyncio.run(
        handler._auto_confirm_backend_enqueue(
            chat_id="chat-1",
            pending_request_text="帮我搜一下今天的科技新闻",
            front_reply="我去查一下",
            task_instruction="检索并总结三条",
            queue_summary="（暂无排队任务）",
            progress_summary="正在处理上一个请求",
        )
    )

    assert result is True


def test_auto_confirm_backend_enqueue_skip(monkeypatch):
    monkeypatch.setattr(mh, "get_soul_prompt", lambda: "")
    monkeypatch.setattr(mh, "load_identity_context", lambda include_schedule=False: "")
    monkeypatch.setattr(mh, "render_template", lambda *args, **kwargs: "prompt")

    handler = _DummyHandler("DECISION:skip")

    result = asyncio.run(
        handler._auto_confirm_backend_enqueue(
            chat_id="chat-1",
            pending_request_text="嗯嗯好的",
            front_reply="抱抱你",
            task_instruction="",
            queue_summary="（暂无排队任务）",
            progress_summary="正在处理上一个请求",
        )
    )

    assert result is False


def test_auto_confirm_backend_enqueue_fallback_to_enqueue_on_error(monkeypatch):
    monkeypatch.setattr(mh, "get_soul_prompt", lambda: "")
    monkeypatch.setattr(mh, "load_identity_context", lambda include_schedule=False: "")
    monkeypatch.setattr(mh, "render_template", lambda *args, **kwargs: "prompt")

    class _ErrHandler(_DummyHandler):
        def _chat_stream_wrapper(self, model_client, chat_id: str, **kwargs):
            raise RuntimeError("llm down")

    handler = _ErrHandler()

    result = asyncio.run(
        handler._auto_confirm_backend_enqueue(
            chat_id="chat-1",
            pending_request_text="帮我查一下",
            front_reply="",
            task_instruction="",
            queue_summary="（暂无排队任务）",
            progress_summary="",
        )
    )

    assert result is True


def test_delete_last_assistant_message_supports_nth(tmp_path):
    from memory.message_history import MessageHistory

    db_path = tmp_path / "history.db"
    mirror_db_path = tmp_path / "mirror.db"
    context_db_path = tmp_path / "context.db"
    history = MessageHistory(
        db_path=str(db_path),
        mirror_db_path=str(mirror_db_path),
        context_db_path=str(context_db_path),
    )

    history.add_message("telegram", "chat-1", "assistant", "第一条", "assistant", metadata={"source": "front"})
    history.add_message("telegram", "chat-1", "assistant", "第二条", "assistant", metadata={"source": "front"})
    history.add_message("telegram", "chat-1", "assistant", "第三条", "assistant", metadata={"source": "front"})

    deleted = history.delete_last_assistant_message("telegram", "chat-1", nth=2)
    assert deleted is not None

    latest = history.delete_last_assistant_message("telegram", "chat-1")
    assert latest is not None
    assert latest["message_id"] != deleted["message_id"]


class _SplitSendingHandler(mh.MessageHandlerMixin):
    _SPLIT_MARKER_PATTERN = re.compile(r"\[SPLIT(?::([0-9.]+))?\]", re.IGNORECASE)

    def __init__(self):
        self.sent = []

    async def _send_platform_message(self, chat_id: str, text: str, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return [str(len(self.sent))]


def test_split_message_consumes_fallback_reply_on_first_sent_part_only():
    handler = _SplitSendingHandler()

    ids = asyncio.run(
        handler._send_split_message(
            "onebotv11:group:100",
            "first[SPLIT]second",
            reply_to_message_id="777",
            chat_type="group",
        )
    )

    assert ids == ["1", "2"]
    assert handler.sent[0][2]["reply_to_message_id"] == "777"
    assert handler.sent[1][2]["reply_to_message_id"] == ""


def test_split_message_keeps_explicit_controls_local_to_each_part():
    handler = _SplitSendingHandler()

    asyncio.run(
        handler._send_split_message(
            "onebotv11:group:100",
            "[reply:111]first[SPLIT][at:222]second",
            chat_type="group",
        )
    )

    assert handler.sent[0][1] == "[reply:111]first"
    assert handler.sent[1][1] == "[at:222]second"


# ---- reply_target_message_id 私聊不回复引用测试 ----


def test_reply_target_message_id_returns_empty_for_private_chat():
    """私聊场景下 reply_target_message_id 应返回空字符串，避免消息错乱。"""
    from core.message_handler import reply_target_message_id

    context = {
        "chat_type": "private",
        "platform_message_id": "123",
        "reply_target_message_id": "456",
    }
    assert reply_target_message_id(context) == ""


def test_reply_target_message_id_returns_explicit_id_for_group_chat():
    """群聊仅在显式指定回复目标时返回消息 ID。"""
    from core.message_handler import reply_target_message_id

    context = {
        "chat_type": "group",
        "platform_message_id": "789",
        "reply_target_message_id": "101",
    }
    assert reply_target_message_id(context) == "101"


def test_reply_target_message_id_does_not_use_incoming_ids_for_group_chat():
    """普通群聊入站消息 ID 不应被自动转换为回复目标。"""
    from core.message_handler import reply_target_message_id

    context = {
        "chat_type": "group",
        "platform_message_id": "789",
        "reply_to_message_id": "456",
    }
    assert reply_target_message_id(context) == ""


def test_reply_target_message_id_defaults_to_private_when_missing():
    """chat_type 缺失时默认视为私聊，不应返回 ID。"""
    from core.message_handler import reply_target_message_id

    context = {"platform_message_id": "222"}
    assert reply_target_message_id(context) == ""
