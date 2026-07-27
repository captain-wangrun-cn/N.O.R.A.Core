import asyncio

from adapters.aggregator import MessageAggregator
from core.conversation_identity import (
    DEFAULT_OWNER_ID,
    DEFAULT_RELATIONSHIP_ID,
    build_conversation_identity,
    build_identity_from_target,
    conversation_target_dict,
    set_owner_resolver,
)
from core.message_handler import group_message_content


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


def test_shared_memory_scope_across_platforms():
    """三平台私聊归入同一 memory_scope_id，但 place/runtime 各不相同。"""
    set_owner_resolver(None)
    try:
        tg = build_conversation_identity(
            {"platform": "telegram", "chat_id": "c1", "user_id": "u1", "chat_type": "private"}
        )
        web = build_conversation_identity(
            {"platform": "web", "chat_id": "c2", "user_id": "u1", "chat_type": "private"}
        )
        discord = build_conversation_identity(
            {"platform": "discord", "chat_id": "c3", "user_id": "u1", "chat_type": "private"}
        )

        # 共享上下文主键一致
        assert tg.memory_scope_id == web.memory_scope_id == discord.memory_scope_id
        assert tg.memory_scope_id == DEFAULT_RELATIONSHIP_ID
        assert tg.owner_id == DEFAULT_OWNER_ID

        # 来源地点 & runtime_key 各异
        assert tg.place_scope_id == "telegram:c1"
        assert web.place_scope_id == "web:c2"
        assert discord.place_scope_id == "discord:c3"
        assert len({tg.runtime_key, web.runtime_key, discord.runtime_key}) == 3
    finally:
        set_owner_resolver(None)


def test_actor_display_name_captured_and_persisted():
    set_owner_resolver(None)
    try:
        identity = build_conversation_identity(
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u9",
                "user_name": "张三",
                "chat_type": "group",
            }
        )
        assert identity.actor_display_name == "张三"

        restored = build_identity_from_target(conversation_target_dict(identity))
        assert restored.actor_display_name == "张三"
        assert restored.memory_scope_id == DEFAULT_RELATIONSHIP_ID
        assert restored.place_scope_id == "telegram:group-1"
    finally:
        set_owner_resolver(None)


def test_owner_resolver_injection_sets_is_owner():
    """注入的 resolver 决定 is_owner；恢复 target 时优先用持久化值避免副作用。"""
    calls = []

    def resolver(platform, user_id, chat_type):
        calls.append((platform, user_id, chat_type))
        return user_id == "owner-uid"

    set_owner_resolver(resolver)
    try:
        owner = build_conversation_identity(
            {"platform": "telegram", "chat_id": "c1", "user_id": "owner-uid", "chat_type": "private"}
        )
        visitor = build_conversation_identity(
            {"platform": "telegram", "chat_id": "g1", "user_id": "other", "chat_type": "group"}
        )
        assert owner.is_owner is True
        assert visitor.is_owner is False
        assert len(calls) == 2

        # 持久化里带 is_owner=True，恢复时不应再调用 resolver
        calls.clear()
        restored = build_identity_from_target(conversation_target_dict(owner))
        assert restored.is_owner is True
        assert calls == []
    finally:
        set_owner_resolver(None)


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


def test_aggregator_separates_group_contributors_by_user():
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

        assert len(completed) == 2
        by_user = {ctx["user_id"]: ctx for ctx in completed}
        assert by_user["u1"]["text"] == "第一句"
        assert by_user["u1"]["platform_message_ids"] == ["101"]
        assert not by_user["u1"].get("reply_target_message_id")
        assert by_user["u2"]["text"] == "第二句"
        assert by_user["u2"]["platform_message_ids"] == ["102"]
        assert not by_user["u2"].get("reply_target_message_id")
        assert by_user["u1"]["contributors"] == [{"user_id": "u1", "user_name": "Alice"}]
        assert by_user["u2"]["contributors"] == [{"user_id": "u2", "user_name": "Bob"}]

    asyncio.run(run())


def test_aggregator_still_combines_same_group_user_messages():
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
                "user_id": "u1",
                "user_name": "Alice",
                "chat_type": "group",
                "platform_message_id": "102",
            },
        )

        await asyncio.sleep(0.03)

        assert len(completed) == 1
        context = completed[0]
        assert context["text"] == "第一句\n第二句"
        assert context["platform_message_ids"] == ["101", "102"]
        assert not context.get("reply_target_message_id")
        assert [c["user_id"] for c in context["contributors"]] == ["u1", "u1"]

    asyncio.run(run())


def test_aggregator_preserves_explicit_reply_target():
    async def run():
        completed = []

        async def on_complete(context):
            completed.append(context)

        agg = MessageAggregator(timeout=0.01, on_complete=on_complete)
        await agg.add_message(
            "group-1",
            "显式回复",
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u1",
                "chat_type": "group",
                "platform_message_id": "101",
                "reply_target_message_id": "999",
            },
        )

        await asyncio.sleep(0.03)

        assert len(completed) == 1
        assert completed[0]["reply_target_message_id"] == "999"

    asyncio.run(run())


def test_aggregator_preserves_native_reference_metadata_per_part():
    async def run():
        completed = []

        async def on_complete(context):
            completed.append(context)

        agg = MessageAggregator(timeout=0.01, on_complete=on_complete)
        await agg.add_message(
            "group-1",
            "[reply:201]第一句[at:301]",
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u1",
                "chat_type": "group",
                "platform_message_id": "101",
                "reply_to_message_id": "201",
                "reply_to_user_id": "u2",
                "reply_to_user_name": "Bob",
                "mentioned_user_ids": ["301"],
                "mentioned_users": [{"user_id": "301", "display_name": "Dave"}],
            },
        )
        await agg.add_message(
            "group-1",
            "[reply:202]第二句[at:302]",
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u1",
                "chat_type": "group",
                "platform_message_id": "102",
                "reply_to_message_id": "202",
                "reply_to_user_id": "u3",
                "reply_to_user_name": "Carol",
                "mentioned_user_ids": ["302", "301"],
                "mentioned_users": [
                    {"user_id": "302", "display_name": "Eve"},
                    {"user_id": "301", "display_name": "Later Name"},
                ],
            },
        )

        await asyncio.sleep(0.03)

        context = completed[0]
        assert context["text"] == "[reply:201]第一句[at:301]\n[reply:202]第二句[at:302]"
        assert context["reply_to_message_id"] == "202"
        assert context["reply_to_user_id"] == "u3"
        assert context["reply_to_user_name"] == "Carol"
        assert context["mentioned_user_ids"] == ["301", "302"]
        assert context["mentioned_users"] == [
            {"user_id": "301", "display_name": "Dave"},
            {"user_id": "302", "display_name": "Eve"},
        ]
        assert context["native_reference_parts"] == [
            {
                "platform_message_id": "101",
                "platform_message_ids": ["101"],
                "reply_to_message_id": "201",
                "reply_to_user_id": "u2",
                "reply_to_user_name": "Bob",
                "mentioned_user_ids": ["301"],
                "mentioned_users": [{"user_id": "301", "display_name": "Dave"}],
            },
            {
                "platform_message_id": "102",
                "platform_message_ids": ["102"],
                "reply_to_message_id": "202",
                "reply_to_user_id": "u3",
                "reply_to_user_name": "Carol",
                "mentioned_user_ids": ["302", "301"],
                "mentioned_users": [
                    {"user_id": "302", "display_name": "Eve"},
                    {"user_id": "301", "display_name": "Later Name"},
                ],
            },
        ]
        assert not context.get("reply_target_message_id")

    asyncio.run(run())


def test_group_message_content_prefixes_single_contributor_aggregate():
    context = {
        "contributors": [
            {"user_id": "u1", "user_name": "Alice"},
            {"user_id": "u1", "user_name": "Alice"},
        ]
    }

    assert group_message_content(context, "Alice", "第一句\n第二句", "group") == "Alice: 第一句\n第二句"


def test_aggregator_preserves_context_platform_message_ids_from_media_group():
    async def run():
        completed = []

        async def on_complete(context):
            completed.append(context)

        agg = MessageAggregator(timeout=0.01, on_complete=on_complete)
        await agg.add_message(
            "group-1",
            "[image: a]\n[image: b]",
            {
                "platform": "telegram",
                "chat_id": "group-1",
                "user_id": "u1",
                "user_name": "Alice",
                "chat_type": "group",
                "platform_message_id": "101",
                "platform_message_ids": ["101", "102"],
                "chat_title": "测试群",
                "group_member_count": 42,
                "group_online_count": None,
                "group_online_count_status": "unavailable:telegram_bot_api",
            },
        )

        await asyncio.sleep(0.03)

        assert completed[0]["platform_message_ids"] == ["101", "102"]
        assert completed[0]["chat_title"] == "测试群"
        assert completed[0]["group_member_count"] == 42
        assert completed[0]["group_online_count"] is None

    asyncio.run(run())


def test_group_message_content_does_not_double_prefix_multi_sender_aggregate():
    context = {
        "contributors": [
            {"user_id": "u1", "user_name": "Alice"},
            {"user_id": "u2", "user_name": "Bob"},
        ]
    }
    text = "Alice: 第一条\nBob: 第二条"

    assert group_message_content(context, "Bob", text, "group") == text
    assert group_message_content({}, "Alice", "hello", "group") == "Alice: hello"
    assert group_message_content({}, "Alice", "hello", "private") == "hello"
