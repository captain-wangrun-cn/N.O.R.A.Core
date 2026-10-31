import asyncio
from types import SimpleNamespace

import pytest

from adapters.delivery import DeliveryContext, DeliveryError, DeliveryReceipt
from core.conversation_identity import build_identity_from_parts
from core.message_handler import MessageHandlerMixin
from core.scheduler_mixin import SchedulerMixin
from memory.message_history import MessageHistory


class ProactiveProbe(SchedulerMixin, MessageHandlerMixin):
    def __init__(self, result):
        self.identity = build_identity_from_parts(platform='qqbot', chat_id='user', user_id='owner', chat_type='private')
        self.result = result
        self.saved = []
        self.sent = []
        self.sessions = {}
        self.message_history = SimpleNamespace(get_context_messages=lambda *args, **kwargs: [], add_message=lambda **kwargs: self.saved.append(kwargs))
        self.generation_tasks = {}
        self.idle = False

    def _identity_for_runtime_key(self, key):
        return self.identity

    async def _generate_front_chat_response(self, *args, **kwargs):
        return {'user_reply': 'complete original', 'needs_backend': False}

    def _log_route_resolution(self, *args, **kwargs):
        pass

    async def _send_split_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def _mark_scheduler_idle(self, chat_id):
        self.idle = True


@pytest.mark.anyio
@pytest.mark.parametrize('result,expected', [(DeliveryError('fail'), ''), (DeliveryError('partial', sent_ids=['1'], sent_texts=['only sent']), 'only sent'), (DeliveryReceipt(['1'], 'complete original'), 'complete original')])
async def test_actual_proactive_history_requires_real_receipt(monkeypatch, result, expected):
    import core.scheduler_mixin as scheduler
    monkeypatch.setattr(scheduler, '_load_yesterday_daily_summary', lambda: '')
    monkeypatch.setattr(scheduler.config, 'get_message_history_config', lambda: {'timezone': 'Asia/Shanghai'})
    probe = ProactiveProbe(result)
    await probe._send_proactive_message('qqbot:user', 'test alarm', event_type='alarm', message_kind='explicit')
    snapshot = probe.sent[0][2]['delivery_context']
    assert snapshot == DeliveryContext.proactive('qqbot', 'user', 'private')
    assistants = [h for h in probe.sessions['qqbot:user']['history'] if h['role'] == 'assistant']
    if expected:
        assert probe.saved[0]['content'] == expected
        assert probe.saved[0]['metadata']['platform_message_ids'] == ['1']
        assert assistants == [{'role': 'assistant', 'content': expected}]
    else:
        assert not probe.saved and not assistants
    assert probe.idle


@pytest.mark.anyio
async def test_undo_uses_target_features_and_does_not_claim_partial_success():
    probe = MessageHandlerMixin()
    probe._identity_for_runtime_key = lambda key: SimpleNamespace(platform='qqbot', storage_id='user')
    probe.message_history = SimpleNamespace(delete_last_assistant_message=lambda **kwargs: {'metadata': {'platform_message_ids': ['1', '2']}})
    probe.sessions = {'qqbot:user': {'history': [{'role': 'assistant', 'content': 'sent'}]}}
    probe.adapter = SimpleNamespace(platform_features=SimpleNamespace(supports_delete_message=False))
    probe._adapter_for_key = lambda key: SimpleNamespace(platform_features=SimpleNamespace(supports_delete_message=True))
    results = iter([True, False])
    async def delete(*args):
        return next(results)
    probe._delete_platform_message = delete
    assert await probe._undo_last_assistant('qqbot:user') == {'delete_attempted': True, 'delete_ok': False}


def test_model_helper_persists_only_valid_alias_to_mock_config(monkeypatch):
    import config
    saved = []
    original = {'llm': {'models': {'fast': 'old'}, 'providers': {'test': {'type': 'openai'}}}}
    monkeypatch.setattr(config, '_safe_config', lambda: original)
    monkeypatch.setattr(config, 'save_config', lambda cfg: saved.append(cfg))
    config.set_llm_model_by_alias('fast', 'new')
    assert saved[0]['llm']['models']['fast'] == 'new'
    assert original['llm']['models']['fast'] == 'old'
    for alias, name in [('unknown', 'x'), ('fast', ''), ('fast', 'name\nwith newline')]:
        with pytest.raises(ValueError):
            config.set_llm_model_by_alias(alias, name)
    assert len(saved) == 1
