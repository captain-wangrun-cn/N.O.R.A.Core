import asyncio
from types import SimpleNamespace

from brain.prompts import render_template
from core.conversation_identity import build_identity_from_parts
from core.front_brain import FrontBrainMixin


class _DummyHistory:
    def get_context_messages(self, *args, **kwargs):
        return []


class _FrontBrainProbe(FrontBrainMixin):
    def __init__(self, responses):
        self.responses = list(responses)
        self.system_prompts = []
        self.llm = object()
        self.front_brain_partial = {}
        self.cost_tracking_enabled = False
        self.cost_tracker = None
        self.message_history = _DummyHistory()
        self.rag = SimpleNamespace(enabled=False)
        self.tool_manager = SimpleNamespace(get_tool_intros=lambda: {})
        self.worker_status = {}
        self.task_queues = {}

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
        return build_identity_from_parts(platform=platform, chat_id=chat_id, chat_type="group")

    def get_busy_backend_runtime(self, memory_scope_id):
        return ""

    def _get_scope_queue_for_runtime(self, chat_id, create=False):
        return None

    async def _send_debug(self, chat_id, message):
        return None

    def _strip_thinking_content(self, text):
        return text

    def _chat_stream_wrapper(self, model_client, chat_id, **kwargs):
        self.system_prompts.append(kwargs.get("system_prompt", ""))
        response = self.responses.pop(0)

        async def _gen():
            yield {"type": "text", "content": response}

        return _gen()


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
