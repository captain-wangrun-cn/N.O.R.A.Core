import asyncio

from core.conversation_identity import build_identity_from_parts
from core.controller import NoraController
from core.scheduler import (
    AIPresence,
    get_ai_presence,
    get_ai_state_summary,
    record_user_activity,
    set_ai_presence,
)
from core.scheduler_mixin import SchedulerMixin
from core.private_presence_store import PrivatePresence, PrivatePresenceStore
from core.worker_status import BackendTaskQueue, WorkerStatus


class _DummyHistory:
    def __init__(self):
        self.closed = []
        self.current_messages = []

    def close_session(self, **kwargs):
        self.closed.append(kwargs)
        return len(self.closed)

    def get_current_segment_messages(self, *args, **kwargs):
        return list(self.current_messages)


class _DummyGroupListener:
    def __init__(self):
        self._modes = {}

    def mode_for(self, runtime_key):
        from core.group_presence_store import GroupPresence
        return self._modes.get(runtime_key, GroupPresence.SEMI_ONLINE)


class _ScopeController(SchedulerMixin):
    FOLLOWUP_END_IDLE_SECONDS = 900
    FOLLOWUP_INITIAL_DELAY = 120
    FOLLOWUP_NEED_FOLLOW_DELAY = 15

    _scope_key_for_identity = NoraController._scope_key_for_identity
    _scope_key_for_runtime_key = NoraController._scope_key_for_runtime_key
    _get_scope_queue_for_runtime = NoraController._get_scope_queue_for_runtime
    get_or_create_scope_queue = NoraController.get_or_create_scope_queue
    _runtime_keys_for_scope = NoraController._runtime_keys_for_scope
    _has_pending_task = NoraController._has_pending_task
    _has_pending_front_task = NoraController._has_pending_front_task
    get_busy_backend_runtime = NoraController.get_busy_backend_runtime
    get_active_runtime_keys = NoraController.get_active_runtime_keys
    is_memory_scope_idle = NoraController.is_memory_scope_idle
    _sync_global_backend_flags = NoraController._sync_global_backend_flags

    def __init__(self):
        self.conversation_identities = {}
        self.task_queues = {}
        self.worker_status = {}
        self.generation_tasks = {}
        self.front_brain_tasks = {}
        self._followup_timers = {}
        self._followup_delay_override = {}
        self._followup_suspended_until_idle = {}
        self.message_history = _DummyHistory()
        self.private_presence = PrivatePresenceStore()
        self.group_listener = _DummyGroupListener()

    def add_identity(self, platform, chat_id, user_id, chat_type="private"):
        identity = build_identity_from_parts(
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
        )
        self.conversation_identities[identity.runtime_key] = identity
        return identity

    def _identity_for_runtime_key(self, runtime_key):
        return self.conversation_identities[runtime_key]


def test_scope_queue_is_shared_across_platforms():
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")
    web = controller.add_identity("web", "web-chat", "owner")

    q1 = controller._get_scope_queue_for_runtime(tg.runtime_key, create=True)
    q2 = controller._get_scope_queue_for_runtime(web.runtime_key, create=True)

    assert q1 is q2
    assert list(controller.task_queues.keys()) == [tg.memory_scope_id]


def test_busy_backend_runtime_is_scope_wide():
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")
    web = controller.add_identity("web", "web-chat", "owner")
    status = WorkerStatus()
    status.start("telegram task")
    controller.worker_status[tg.runtime_key] = status

    assert controller.get_busy_backend_runtime(web.memory_scope_id) == tg.runtime_key


def test_transition_waits_until_whole_scope_is_idle():
    """第一个 runtime 切 semi_online 时 scope 仍 active → private presence 保持 ONLINE；
    第二个也切时 → private presence 变 SEMI_ONLINE。"""
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")
    web = controller.add_identity("web", "web-chat", "owner")

    # 设置私聊为 ONLINE（模拟正在聊天）
    controller.private_presence.set(tg.memory_scope_id, PrivatePresence.ONLINE, reason="test_setup")
    record_user_activity(web.runtime_key)

    # 第一个 runtime 切 semi_online — scope 仍有 web 活跃
    controller._transition_to_semi_online(tg.runtime_key)
    assert controller.message_history.closed == []
    assert controller.private_presence.get(tg.memory_scope_id) == PrivatePresence.ONLINE

    # 第二个 runtime 切 semi_online — scope 全部空闲
    controller._transition_to_semi_online(web.runtime_key)
    assert len(controller.message_history.closed) == 1
    assert controller.message_history.closed[0]["memory_scope_id"] == tg.memory_scope_id
    assert controller.private_presence.get(tg.memory_scope_id) == PrivatePresence.SEMI_ONLINE


def test_active_runtime_includes_scope_queue():
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")
    web = controller.add_identity("web", "web-chat", "owner")

    async def run():
        queue = controller._get_scope_queue_for_runtime(tg.runtime_key, create=True)
        await queue.enqueue({"text": "queued from tg", "context": {"chat_id": tg.platform_chat_id}})

    asyncio.run(run())

    active = controller.get_active_runtime_keys(web.memory_scope_id)
    assert tg.runtime_key in active
    assert web.runtime_key in active


def test_backend_start_writes_private_presence():
    """私聊后脑启动后，PrivatePresenceStore 变为 ONLINE；
    is_backend_busy / is_generating 为 True。"""
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")

    controller._update_scheduler_state(tg.runtime_key, busy=True)

    assert controller.private_presence.get(tg.memory_scope_id) == PrivatePresence.ONLINE
    state = get_ai_state_summary()
    assert state["is_backend_busy"] is True
    assert state["is_generating"] is True

    controller._transition_to_semi_online(tg.runtime_key)
    cleanup_state = get_ai_state_summary()
    assert cleanup_state["is_backend_busy"] is False
    assert cleanup_state["is_generating"] is False
    assert controller.private_presence.get(tg.memory_scope_id) == PrivatePresence.SEMI_ONLINE


def test_group_followup_timer_is_rejected_and_legacy_timer_cleared():
    async def run():
        controller = _ScopeController()
        group = controller.add_identity("onebotv11", "10001", "owner", chat_type="group")
        legacy = asyncio.create_task(asyncio.sleep(60))
        controller._followup_timers[group.runtime_key] = legacy
        controller._followup_delay_override[group.runtime_key] = 15

        controller._start_followup_timer(group.runtime_key, initial_delay=1)
        await asyncio.sleep(0)

        assert group.runtime_key not in controller._followup_timers
        assert group.runtime_key not in controller._followup_delay_override
        assert legacy.cancelled()

    asyncio.run(run())


def test_private_followup_timer_still_starts():
    async def run():
        controller = _ScopeController()
        private = controller.add_identity("telegram", "private-chat", "owner")
        started = asyncio.Event()

        async def fake_followup_loop(runtime_key):
            assert runtime_key == private.runtime_key
            started.set()

        controller._followup_loop = fake_followup_loop
        controller._start_followup_timer(private.runtime_key, initial_delay=1)
        await asyncio.wait_for(started.wait(), timeout=0.2)
        assert private.runtime_key in controller._followup_timers
        assert controller._followup_delay_override[private.runtime_key] == 1
        await controller._followup_timers[private.runtime_key]

    asyncio.run(run())


def test_followup_context_is_limited_to_current_place():
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")
    ob = controller.add_identity("onebotv11", "qq-chat", "owner")
    controller.message_history.current_messages = [
        {
            "role": "user",
            "content": "Telegram 这里刚聊到一半",
            "platform": "telegram",
            "chat_id": tg.storage_id,
            "place_scope_id": tg.place_scope_id,
        },
        {
            "role": "user",
            "content": "QQ 这里是另一个活跃话题",
            "platform": "onebotv11",
            "chat_id": ob.storage_id,
            "place_scope_id": ob.place_scope_id,
        },
    ]

    tg_msgs = controller._current_place_segment_messages(tg)
    ob_msgs = controller._current_place_segment_messages(ob)

    assert [m["content"] for m in tg_msgs] == ["Telegram 这里刚聊到一半"]
    assert [m["content"] for m in ob_msgs] == ["QQ 这里是另一个活跃话题"]


def test_followup_context_is_limited_between_onebot_groups():
    controller = _ScopeController()
    group_a = controller.add_identity("onebotv11", "10001", "owner", chat_type="group")
    group_b = controller.add_identity("onebotv11", "10002", "owner", chat_type="group")
    assert group_a.memory_scope_id == group_b.memory_scope_id
    assert group_a.place_scope_id == "onebotv11:10001"
    assert group_b.place_scope_id == "onebotv11:10002"

    controller.message_history.current_messages = [
        {
            "role": "user",
            "content": "群10001正在聊作业",
            "platform": "onebotv11",
            "chat_id": group_a.storage_id,
        },
        {
            "role": "user",
            "content": "群10002正在聊游戏",
            "platform": "onebotv11",
            "chat_id": group_b.storage_id,
        },
    ]

    group_a_msgs = controller._current_place_segment_messages(group_a)
    group_b_msgs = controller._current_place_segment_messages(group_b)

    assert [m["content"] for m in group_a_msgs] == ["群10001正在聊作业"]
    assert [m["content"] for m in group_b_msgs] == ["群10002正在聊游戏"]
