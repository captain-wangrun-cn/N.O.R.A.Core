import asyncio

from core.back_brain import BackBrainMixin
from core.conversation_identity import build_identity_from_parts
from core.polling import PollingMixin


class _PollingHistory:
    def __init__(self):
        self.saved = []

    def get_messages_since(self, *args, **kwargs):
        return []

    def add_message(self, **kwargs):
        self.saved.append(kwargs)


class _PollingProbe(BackBrainMixin, PollingMixin):
    MAX_POLLING_ROUNDS = 2
    FOLLOWUP_NEED_FOLLOW_DELAY = 15

    def __init__(self, actions=None):
        self.contexts = []
        self.reviews = []
        self.debug = []
        self.sent = []
        self.backend_history_seen = []
        self.sessions = {"onebotv11:10001": {"history": []}}
        self.actions = list(actions or ["continue", "continue"])
        self.message_history = _PollingHistory()
        self.worker_status = {}
        self.interrupted = False
        self.processed_pending = False

    def _identity(self, context):
        return build_identity_from_parts(
            platform=context.get("platform", "onebotv11"),
            chat_id=context.get("chat_id", "10001"),
            user_id=context.get("user_id", "owner"),
            chat_type=context.get("chat_type", "group"),
        )

    async def _send_debug(self, chat_id, text):
        self.debug.append((chat_id, text))

    async def _generate_response(self, context):
        self.contexts.append(dict(context))
        session = self.sessions["onebotv11:10001"]
        work_history = list(session.get("_polling_backend_history") or [])
        self.backend_history_seen.append(list(work_history))
        if len(self.contexts) == 1:
            work_history.append({"role": "user", "content": "initial backend prompt"})
            work_history.append({"role": "user", "content": "【Tool Output for read_file】\nfull file content"})
        work_history.append({
            "role": "assistant",
            "content": f"backend report {len(self.contexts)}",
        })
        session["_polling_backend_history"] = work_history

    def _was_interrupted(self, chat_id):
        return self.interrupted

    def _strip_timestamp_markers(self, text):
        return text

    async def _front_brain_review(self, context, backend_result, new_user_texts):
        self.reviews.append((dict(context), backend_result, list(new_user_texts)))
        action = self.actions.pop(0) if self.actions else "done"
        return {
            "action": action,
            "user_reply": "继续处理",
            "task_instruction": "下一步" if action == "continue" else "",
            "should_reply": False,
        }

    async def _send_split_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return ["msg-1"]

    def _get_scope_queue_for_runtime(self, chat_id):
        return None

    async def _process_pending_messages(self, chat_id):
        self.processed_pending = True

    def _start_followup_timer(self, *args, **kwargs):
        pass

    def _transition_to_semi_online(self, *args, **kwargs):
        pass


def test_polling_continue_at_limit_does_not_start_extra_round():
    probe = _PollingProbe()

    asyncio.run(probe._run_polling_loop({
        "platform": "onebotv11",
        "chat_id": "10001",
        "user_id": "owner",
        "chat_type": "group",
        "text": "帮我处理",
    }))

    assert len(probe.contexts) == 2
    assert probe.contexts[1]["_polling_previous_backend_result"].startswith("backend report 1")
    assert probe.contexts[1]["task_instruction"] == "下一步"
    assert probe.reviews[1][1] == "backend report 2"
    assert len(probe.reviews) == 2
    assert "_polling_backend_history" not in probe.sessions["onebotv11:10001"]
    assert probe.processed_pending is True


def test_polling_keeps_backend_history_until_front_brain_done():
    probe = _PollingProbe(actions=["continue", "done"])

    asyncio.run(probe._run_polling_loop({
        "platform": "onebotv11",
        "chat_id": "10001",
        "user_id": "owner",
        "chat_type": "group",
        "text": "帮我处理",
    }))

    assert len(probe.contexts) == 2
    assert probe.reviews[0][1] == "backend report 1"
    assert probe.reviews[1][1] == "backend report 2"
    assert probe.backend_history_seen[0] == []
    assert "【Tool Output for read_file】\nfull file content" in [
        h["content"] for h in probe.backend_history_seen[1]
    ]
    assert "_polling_backend_history" not in probe.sessions["onebotv11:10001"]
