import asyncio
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
