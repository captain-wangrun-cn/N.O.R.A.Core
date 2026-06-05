import asyncio

from adapters.aggregator import MessageAggregator
from core.conversation_identity import (
    build_conversation_identity,
    build_identity_from_target,
    conversation_target_dict,
)


def test_conversation_identity_private_vs_group_storage():
    private = build_conversation_identity(
        {"platform": "telegram", "chat_id": "chat-1", "user_id": "user-1", "chat_type": "private"}
    )
    assert private.platform == "telegram"
    assert private.platform_chat_id == "chat-1"
    assert private.storage_id == "user-1"
    assert private.runtime_key == "telegram:chat-1"

    group = build_conversation_identity(
        {"platform": "telegram", "chat_id": "group-1", "user_id": "user-1", "chat_type": "group"}
    )
    assert group.storage_id == "group-1"
    assert group.runtime_key == "telegram:group-1"


def test_conversation_target_round_trips_private_storage_id():
    identity = build_conversation_identity(
        {"platform": "telegram", "chat_id": "chat-1", "user_id": "user-1", "chat_type": "private"}
    )
    target = conversation_target_dict(identity)
    restored = build_identity_from_target(target)

    assert restored.runtime_key == "telegram:chat-1"
    assert restored.platform_chat_id == "chat-1"
    assert restored.actor_user_id == "user-1"
    assert restored.storage_id == "user-1"


def test_aggregator_separates_same_chat_id_across_platforms():
    async def run():
        completed = []

        async def on_complete(context):
            completed.append(context)

        agg = MessageAggregator(timeout=0.01, on_complete=on_complete)
        await agg.add_message("42", "from telegram", {"platform": "telegram", "chat_id": "42", "user_id": "u1", "chat_type": "private"})
        await agg.add_message("42", "from discord", {"platform": "discord", "chat_id": "42", "user_id": "u1", "chat_type": "private"})

        await asyncio.sleep(0.03)

        texts = sorted(ctx["text"] for ctx in completed)
        assert texts == ["from discord", "from telegram"]

    asyncio.run(run())


def test_aggregator_preserves_group_contributors():
    async def run():
        completed = []

        async def on_complete(context):
            completed.append(context)

        agg = MessageAggregator(timeout=0.01, on_complete=on_complete)
        await agg.add_message(
            "group-1",
            "第一句",
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u1",
                "user_name": "Alice",
                "chat_type": "group",
                "platform_message_id": "101",
            },
        )
        await agg.add_message(
            "group-1",
            "第二句",
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u2",
                "user_name": "Bob",
                "chat_type": "group",
                "platform_message_id": "102",
            },
        )

        await asyncio.sleep(0.03)

        assert len(completed) == 1
        context = completed[0]
        assert "Alice: 第一句" in context["text"]
        assert "Bob: 第二句" in context["text"]
        assert context["platform_message_ids"] == ["101", "102"]
        assert [c["user_id"] for c in context["contributors"]] == ["u1", "u2"]

    asyncio.run(run())
