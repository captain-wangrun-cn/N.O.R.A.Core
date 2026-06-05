from core.back_brain import BackBrainMixin


def _inject(tool_name, args):
    return BackBrainMixin()._inject_current_tool_context(
        tool_name,
        dict(args),
        runtime_key="telegram:group-1",
        platform="telegram",
        platform_chat_id="group-1",
        storage_id="group-1",
    )


def test_view_media_injects_current_conversation_filters():
    args = _inject("view_media", {"keyword": "截图"})

    assert args["platform"] == "telegram"
    assert args["chat_id"] == "group-1"
    assert args["storage_id"] == "group-1"
    assert "user_id" not in args


def test_view_media_keeps_explicit_filters():
    args = _inject(
        "view_media",
        {
            "keyword": "截图",
            "platform": "discord",
            "chat_id": "other-chat",
            "storage_id": "other-storage",
        },
    )

    assert args["platform"] == "discord"
    assert args["chat_id"] == "other-chat"
    assert args["storage_id"] == "other-storage"


def test_set_alarm_injects_runtime_key():
    args = _inject("set_alarm", {"trigger_time": "23:00", "reason": "测试"})

    assert args["chat_id"] == "telegram:group-1"


def test_progress_note_injects_runtime_key():
    args = _inject("store_progress_note", {"step": "检索", "explanation": "正在查"})

    assert args["chat_id"] == "telegram:group-1"
