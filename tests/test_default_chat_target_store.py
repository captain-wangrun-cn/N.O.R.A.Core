from types import SimpleNamespace

from core.conversation_identity import build_conversation_identity, conversation_target_dict
from core import default_chat_id_store


def test_default_chat_target_persists_storage_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        default_chat_id_store,
        "get_workspace_manager",
        lambda: SimpleNamespace(data_dir=str(tmp_path)),
    )

    identity = build_conversation_identity(
        {
            "platform": "telegram",
            "chat_id": "private-chat",
            "user_id": "real-user",
            "chat_type": "private",
        }
    )

    assert default_chat_id_store.save_default_chat_target(conversation_target_dict(identity))

    target = default_chat_id_store.load_default_chat_target()
    assert target["runtime_key"] == "telegram:private-chat"
    assert target["platform_chat_id"] == "private-chat"
    assert target["storage_id"] == "real-user"
    assert default_chat_id_store.load_default_chat_id() == "telegram:private-chat"
