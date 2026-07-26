import asyncio
from types import SimpleNamespace

from brain.prompts import load_adapter_prompt, render_template
from core.conversation_identity import build_identity_from_parts, set_owner_resolver
from core.front_brain import FrontBrainMixin
from core.group_listener import AppendAction, GroupMessageEvent, PassiveAction
from core.group_presence_store import GroupPresence
from core.message_handler import MessageHandlerMixin
from core.worker_status import BackendTaskQueue, WorkerStatus


class _DummyHistory:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def get_context_messages(self, *args, **kwargs):
        return list(self.messages)

    def get_forebrain_context_messages(self, *args, **kwargs):
        return list(self.messages)


class _FrontBrainProbe(FrontBrainMixin):
    def __init__(self, responses):
        self.responses = list(responses)
        self.system_prompts = []
        self.histories = []
        self.llm = object()
        self.front_brain_partial = {}
        self.cost_tracking_enabled = False
        self.cost_tracker = None
        self.message_history = _DummyHistory()
        self.rag = SimpleNamespace(enabled=False)
        self.tool_manager = SimpleNamespace(get_tool_intros=lambda: {})
        self.worker_status = {}
        self.task_queues = {}
        self.group_presences = {}

    def group_presence_for(self, runtime_key):
        return self.group_presences.get(runtime_key, GroupPresence.SEMI_ONLINE)

    def _identity(self, context):
        return build_identity_from_parts(
            platform=context.get("platform", "onebotv11"),
            chat_id=context.get("chat_id", "10001"),
            user_id=context.get("user_id", "20002"),
            chat_type=context.get("chat_type", "group"),
            user_name=context.get("user_name", "User"),
        )

    def _identity_for_runtime_key(self, runtime_key):
        platform, chat_id = runtime_key.split(":", 1)
        chat_type = "private" if chat_id in {"owner", "private-chat"} else "group"
        return build_identity_from_parts(
            platform=platform,
            chat_id=chat_id,
            user_id=chat_id,
            chat_type=chat_type,
            user_name=chat_id,
        )

    def get_busy_backend_runtime(self, memory_scope_id):
        for key, status in self.worker_status.items():
            if status.busy:
                return key
        return ""

    def _get_scope_queue_for_runtime(self, chat_id, create=False):
        return self.task_queues.get("shared")

    async def _send_debug(self, chat_id, message):
        return None

    def _strip_thinking_content(self, text):
        return text

    def _chat_stream_wrapper(self, model_client, chat_id, **kwargs):
        self.system_prompts.append(kwargs.get("system_prompt", ""))
        self.histories.append(list(kwargs.get("history") or []))
        response = self.responses.pop(0)

        async def _gen():
            yield {"type": "text", "content": response}

        return _gen()


def test_onebot_prompt_prefers_reply_with_sender_mention():
    prompt = load_adapter_prompt("onebotv11")

    assert "左滑回复" in prompt
    assert "[reply:MESSAGE_ID][at:USER_ID]" in prompt
    assert "而不是只加 reply" in prompt
    assert "仅引用某人的话作为讨论材料" in prompt


def test_front_brain_system_includes_platform_prompt_and_preferences():
    system_prompt = render_template(
        "front_brain.jinja",
        "system",
        soul_prompt="SOUL",
        identity_context="",
        user_preferences_block="【当前 Nora 偏好设置】\n- 分段聊天倾向：高",
        platform_prompt_block="【onebotv11 平台专属协议】\nOneBot v11 / QQ 不解析 Markdown；使用 [SPLIT] 分段。",
        schedule_block="",
        custom_block="",
        tool_intro_block="",
        skill_intro_block="",
        proactive_context="",
    )

    assert "分段聊天倾向：高" in system_prompt
    assert "OneBot v11 / QQ 不解析 Markdown" in system_prompt
    assert "使用 [SPLIT] 分段" in system_prompt
    assert "当前群" in system_prompt
    assert "[ONLINE]" in system_prompt
    assert "多说话者" in system_prompt


def test_front_brain_review_system_includes_platform_prompt_and_preferences():
    system_prompt = render_template(
        "front_brain_review.jinja",
        "system",
        soul_prompt="SOUL",
        identity_context="",
        user_preferences_block="【当前 Nora 偏好设置】\n- 分段聊天倾向：高",
        platform_prompt_block="【onebotv11 平台专属协议】\nOneBot v11 / QQ 不解析 Markdown；使用 [SPLIT] 分段。",
        schedule_block="",
        custom_block="",
        tool_intro_block="",
    )

    assert "分段聊天倾向：高" in system_prompt
    assert "OneBot v11 / QQ 不解析 Markdown" in system_prompt
    assert "使用 [SPLIT] 分段" in system_prompt
    assert "当前群" in system_prompt
    assert "[ONLINE]" in system_prompt
    assert "多说话者" in system_prompt


def test_group_listener_templates_define_strict_actions_and_batches():
    passive_system = render_template("group_listener.jinja", "passive_system")
    passive_user = render_template(
        "group_listener.jinja",
        "passive_user",
        events_text="[1] Alice: first\n[2] Bob: second",
    )
    append_system = render_template("group_listener.jinja", "append_system")
    append_user = render_template(
        "group_listener.jinja",
        "append_user",
        base_events_text="old batch",
        new_events_text="new batch",
    )

    assert all(action in passive_system for action in ("KEEP_LISTENING", "REPLY", "SEMI_ONLINE"))
    assert "不可信" in passive_system and "directed_to_nora=true" in passive_system
    assert "即使没有 directed_to_nora=true" in passive_system
    assert "及时、具体、相关且非重复" in passive_system
    assert "不要仅因“有机会说话”就插话" in passive_system
    assert "无法确定时选择 KEEP_LISTENING" in passive_system
    assert "总消息数" in passive_user and "Alice: first" in passive_user and "Bob: second" in passive_user
    assert "APPEND" in append_system and "KEEP_PENDING" in append_system
    assert "Windows" in append_system and "Linux" in append_system
    assert "不可信" in append_system
    assert "old batch" in append_user and "new batch" in append_user


def test_group_listener_fast_action_parser_requires_exact_action():
    assert MessageHandlerMixin._parse_fast_action(" reply \n", PassiveAction) == PassiveAction.REPLY
    assert MessageHandlerMixin._parse_fast_action("APPEND", AppendAction) == AppendAction.APPEND

    for invalid in ("不要 REPLY，应 KEEP_LISTENING", "REPLY because directed", "REPLY\nKEEP_LISTENING", ""):
        try:
            MessageHandlerMixin._parse_fast_action(invalid, PassiveAction)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid fast output accepted: {invalid!r}")


def test_group_listener_classifier_jsonl_preserves_routing_metadata():
    context = {
        "runtime_key": "onebotv11:10001",
        "memory_scope_id": "relationship:owner:default",
        "place_scope_id": "place:onebotv11:10001",
        "platform": "onebotv11",
        "chat_id": "10001",
        "user_id": "20002",
        "user_name": "群友A",
        "text": "补充条件",
        "platform_message_id": "12",
        "platform_message_ids": ["11", "12"],
        "explicit_bot_mention": True,
        "reply_to_bot": True,
        "reply_to_message_id": "10",
        "reply_to_user_id": "nora",
        "reply_to_user_name": "Nora",
        "reply_to_text": "之前的回答",
    }
    event = GroupMessageEvent.from_context(context, 7)

    payload = __import__("json").loads(
        MessageHandlerMixin._format_group_events_for_classifier(
            [event], new_event_sequences={7}
        )
    )

    assert payload["window_part"] == "new"
    assert payload["sender"] == {"id": "20002", "name": "群友A"}
    assert payload["message_ids"] == ["11", "12"]
    assert payload["directed_to_nora"] is True
    assert payload["explicit_bot_mention"] is True
    assert payload["reply_to_bot"] is True
    assert payload["reply"] == {
        "message_id": "10",
        "user_id": "nora",
        "user_name": "Nora",
        "text": "之前的回答",
    }
    promoted = MessageHandlerMixin._format_group_events([event])
    assert promoted == "[7] 群友A id=12（回复 Nora#10）: 补充条件"
    assert not promoted.startswith("{")


def test_front_brain_retries_legacy_backend_signal_without_task_instruction():
    probe = _FrontBrainProbe(
        [
            "我去看一下。[NEED_BACKEND]",
            "我去看一下。[TASK_INSTRUCTION]查询当前 QQ 群成员信息并汇总。[/TASK_INSTRUCTION] [NEED_BACKEND]",
        ]
    )

    result = asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "onebotv11",
                "chat_id": "10001",
                "user_id": "20002",
                "chat_type": "group",
                "user_name": "User",
                "text": "帮我看一下这个群的信息",
            }
        )
    )

    assert result["needs_backend"] is True
    assert result["task_instruction"] == "查询当前 QQ 群成员信息并汇总。"
    assert result["user_reply"] == "我去看一下。"
    assert len(probe.system_prompts) == 2
    assert "必须同时输出 [TASK_INSTRUCTION]" in probe.system_prompts[1]


def test_front_brain_prompt_includes_current_onebot_group_scene():
    probe = _FrontBrainProbe(["在群里收到啦。"])

    result = asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "onebotv11",
                "chat_id": "10001",
                "user_id": "20002",
                "chat_type": "group",
                "user_name": "群友A",
                "chat_title": "测试群",
                "platform_message_id": 345,
                "text": "老大，怎么啦？是不是等急啦",
            }
        )
    )

    assert result["user_reply"] == "在群里收到啦。"
    system_prompt = probe.system_prompts[0]
    assert "【当前会话场景 (Scene / Reply Target)】" in system_prompt
    assert "当前窗口: QQ 群聊" in system_prompt
    assert "回复目标: onebotv11:10001" in system_prompt
    assert "当前说话人: 群友A" in system_prompt
    assert "当前说话人平台用户 ID: 20002" in system_prompt
    assert "[at:20002]" in system_prompt
    assert "自然插话并明确对象" in system_prompt
    assert "群聊名称: 测试群" in system_prompt
    assert "当前入站消息 ID: 345" in system_prompt
    assert "[reply:MESSAGE_ID]" in system_prompt
    assert "多人或多分支对话中需要明确目标" in system_prompt
    assert "看到 ID 不代表必须回复或自动引用" in system_prompt
    assert "不要每条都加" in system_prompt
    assert "模拟 QQ 左滑回复时的“回复 + @ 发送人”" in system_prompt
    assert "通常优先同时使用 `[reply:MESSAGE_ID]` 和 `[at:USER_ID]`" in system_prompt
    assert "只是引用其话语作为讨论材料" in system_prompt
    assert "禁止从昵称、正文或历史猜测" in system_prompt


def test_front_brain_prompt_includes_group_presence_trigger_and_online_examples():
    probe = _FrontBrainProbe(["当然呀，我在呢。[ONLINE]"])

    result = asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "onebotv11",
                "chat_id": "10001",
                "user_id": "20002",
                "chat_type": "group",
                "user_name": "群友A",
                "text": "陪我聊天",
                "explicit_bot_mention": True,
                "reply_to_bot": False,
            }
        )
    )

    assert result["force_online"] is True
    assert result["user_reply"] == "当然呀，我在呢。"
    system_prompt = probe.system_prompts[0]
    assert "【群监听运行状态（可信系统元数据）】" in system_prompt
    assert "当前群监听模式: SEMI_ONLINE" in system_prompt
    assert "本轮触发方式: 用户明确 @ 你" in system_prompt
    assert "陪我聊天" in system_prompt
    assert "必须输出 `[ONLINE]`" in system_prompt
    assert "一次性问答" in system_prompt


def test_front_brain_private_prompt_omits_group_presence_runtime_block():
    probe = _FrontBrainProbe(["我在呀。"])

    asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "telegram",
                "chat_id": "private-chat",
                "user_id": "owner",
                "chat_type": "private",
                "user_name": "Owner",
                "text": "陪我聊天",
                "explicit_bot_mention": True,
            }
        )
    )

    assert "【群监听运行状态（可信系统元数据）】" not in probe.system_prompts[0]


def test_front_brain_prompt_includes_ordered_aggregate_and_distinct_reply_ids():
    probe = _FrontBrainProbe(["收到。"])

    asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "telegram",
                "chat_id": "private-chat",
                "user_id": "owner",
                "chat_type": "private",
                "user_name": "Owner",
                "platform_message_id": "103",
                "platform_message_ids": ["101", 102, "101"],
                "reply_to_message_id": 88,
                "reply_target_message_id": "99",
                "text": "看看这些",
            }
        )
    )

    system_prompt = probe.system_prompts[0]
    assert "当前聚合入站消息 IDs（按输入顺序）: 101, 102, 103" in system_prompt
    assert "用户当前消息引用的历史消息 ID: 88" in system_prompt
    assert "应用已选择的出站引用目标 ID: 99" in system_prompt
    assert "禁止猜测、转换或跨平台/跨场景复用" in system_prompt


def test_front_brain_prompt_without_inbound_ids_does_not_invent_message_id():
    probe = _FrontBrainProbe(["主动问候。"])

    asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "telegram",
                "chat_id": "private-chat",
                "user_id": "owner",
                "chat_type": "private",
                "user_name": "Owner",
                "text": "主动消息上下文",
            }
        )
    )

    system_prompt = probe.system_prompts[0]
    assert "- 当前入站消息 ID:" not in system_prompt
    assert "- 当前聚合入站消息 IDs（按输入顺序）:" not in system_prompt
    assert "- 用户当前消息引用的历史消息 ID:" not in system_prompt
    assert "- 应用已选择的出站引用目标 ID:" not in system_prompt


def test_front_brain_history_preserves_system_context_and_not_last20_truncated():
    probe = _FrontBrainProbe(["收到啦。"])
    probe.message_history = _DummyHistory(
        [{"role": "system", "content": "[压缩段] 历史摘要", "timestamp": 0}]
        + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"历史消息{i}", "timestamp": i + 1}
            for i in range(25)
        ]
    )

    result = asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "onebotv11",
                "chat_id": "10001",
                "user_id": "20002",
                "chat_type": "group",
                "user_name": "User",
                "text": "新的问题",
            }
        )
    )

    assert result["user_reply"] == "收到啦。"
    history = probe.histories[0]
    assert history[0] == {"role": "system", "content": "[压缩段] 历史摘要"}
    assert any(item["content"] == "历史消息0" for item in history)
    assert len(history) == 26


def test_front_brain_history_removes_current_user_duplicate():
    probe = _FrontBrainProbe(["收到啦。"])
    probe.message_history = _DummyHistory(
        [
            {"role": "system", "content": "[压缩段] 历史摘要", "timestamp": 0},
            {"role": "user", "content": "User: 新的问题", "timestamp": 1},
        ]
    )

    asyncio.run(
        probe._generate_front_chat_response(
            {
                "platform": "onebotv11",
                "chat_id": "10001",
                "user_id": "20002",
                "chat_type": "group",
                "user_name": "User",
                "text": "新的问题",
            }
        )
    )

    assert probe.histories[0] == [{"role": "system", "content": "[压缩段] 历史摘要"}]


def test_backend_status_redacts_other_window_details_in_group():
    probe = _FrontBrainProbe([])
    status = WorkerStatus()
    status.start(
        "下载私聊里那两个视频",
        task_instruction="下载私聊用户发来的两个视频，完成后发回私聊。",
    )
    probe.worker_status["onebotv11:20002"] = status

    block = probe._build_backend_status_block("onebotv11:10001")

    assert "另一个窗口执行任务" in block
    assert "来源地点: onebotv11:20002" in block
    assert "下载私聊里那两个视频" not in block
    assert "下载私聊用户发来的两个视频" not in block


def test_backend_status_allows_owner_private_cross_window_details():
    def resolver(platform, user_id, chat_type):
        return user_id == "owner"

    set_owner_resolver(resolver)
    try:
        probe = _FrontBrainProbe([])
        status = WorkerStatus()
        status.start(
            "下载群里那个视频",
            task_instruction="下载群里用户提到的视频，完成后发回当前私聊。",
        )
        probe.worker_status["onebotv11:10001"] = status

        block = probe._build_backend_status_block("onebotv11:owner")

        assert "下载群里那个视频" in block
        assert "下载群里用户提到的视频" in block
    finally:
        set_owner_resolver(None)


def test_backend_status_redacts_other_window_queue_items_in_group():
    probe = _FrontBrainProbe([])

    async def run():
        queue = BackendTaskQueue()
        await queue.enqueue(
            {
                "context": {
                    "platform": "onebotv11",
                    "chat_id": "20002",
                    "user_id": "20002",
                    "chat_type": "private",
                    "text": "下载私聊视频",
                    "task_instruction": "下载私聊里的两个视频。",
                },
                "text": "下载私聊视频",
            }
        )
        probe.task_queues["shared"] = queue

    asyncio.run(run())
    block = probe._build_backend_status_block("onebotv11:10001")

    assert "另有 1 个其它窗口的排队任务，内容已隐藏。" in block
    assert "下载私聊视频" not in block
    assert "下载私聊里的两个视频" not in block
