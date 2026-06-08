import asyncio

from core.message_handler import MessageHandlerMixin
from core.conversation_identity import build_identity_from_parts
from core.controller import NoraController


class _DummyAdapter:
    platform_name = "telegram"

    async def send_message(self, chat_id, text, **kwargs):
        return ["msg-1"]


class _DummyHistory:
    def __init__(self):
        self.calls = []

    def add_message(self, **kwargs):
        self.calls.append(kwargs)


class _DummyController(MessageHandlerMixin):
    def __init__(self):
        self.adapter = _DummyAdapter()
        self.message_history = _DummyHistory()
        self.generation_tasks = {}
        self.sessions = {}
        self.task_queues = {}
        self.worker_status = {}
        self._followup_timers = {}
        self._followup_delay_override = {}
        self._followup_suspended_until_idle = {}
        self.front_brain_tasks = {}
        self.conversation_identities = {}
        identity = build_identity_from_parts(
            platform="telegram",
            chat_id="chat-1",
            user_id="user-1",
            chat_type="private",
        )
        self.conversation_identities[identity.runtime_key] = identity

    def _identity_for_runtime_key(self, key):
        return self.conversation_identities[key]

    _scope_key_for_identity = NoraController._scope_key_for_identity
    _scope_key_for_runtime_key = NoraController._scope_key_for_runtime_key
    _get_scope_queue_for_runtime = NoraController._get_scope_queue_for_runtime
    _runtime_keys_for_scope = NoraController._runtime_keys_for_scope
    get_busy_backend_runtime = NoraController.get_busy_backend_runtime
    _has_pending_task = NoraController._has_pending_task
    _has_pending_front_task = NoraController._has_pending_front_task
    get_active_runtime_keys = NoraController.get_active_runtime_keys

    def _platform_chat_id(self, key):
        return self._identity_for_runtime_key(key).platform_chat_id

    async def _send_platform_message(self, key, text, **kwargs):
        return await self.adapter.send_message(self._platform_chat_id(key), text, **kwargs)

    def _cancel_followup_timer(self, chat_id):
        self._followup_delay_override.pop(chat_id, None)


def test_cmd_stop_private_writes_storage_id_not_platform_chat_id():
    controller = _DummyController()

    asyncio.run(controller._cmd_stop("telegram:chat-1", chat_type="private", user_id="user-1"))

    assert controller.message_history.calls
    call = controller.message_history.calls[0]
    assert call["platform"] == "telegram"
    assert call["chat_id"] == "user-1"
