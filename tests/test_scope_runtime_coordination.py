import asyncio

from core.conversation_identity import build_identity_from_parts
from core.controller import NoraController
from core.scheduler import (
    AIPresence,
    get_ai_presence,
    record_user_activity,
    set_ai_presence,
)
from core.scheduler_mixin import SchedulerMixin
from core.worker_status import BackendTaskQueue, WorkerStatus


class _DummyHistory:
    def __init__(self):
        self.closed = []

    def close_session(self, **kwargs):
        self.closed.append(kwargs)
        return len(self.closed)


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

    def add_identity(self, platform, chat_id, user_id):
        identity = build_identity_from_parts(
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            chat_type="private",
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
    controller = _ScopeController()
    tg = controller.add_identity("telegram", "tg-chat", "owner")
    web = controller.add_identity("web", "web-chat", "owner")

    set_ai_presence(AIPresence.ONLINE)
    record_user_activity(web.runtime_key)

    controller._transition_to_semi_online(tg.runtime_key)
    assert controller.message_history.closed == []
    assert get_ai_presence() == AIPresence.ONLINE

    controller._transition_to_semi_online(web.runtime_key)
    assert len(controller.message_history.closed) == 1
    assert controller.message_history.closed[0]["memory_scope_id"] == tg.memory_scope_id
    assert get_ai_presence() == AIPresence.SEMI_ONLINE


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
