import asyncio
import re
from types import SimpleNamespace

import pytest

from adapters.delivery import DeliveryContext, DeliveryError, DeliveryReceipt, delivered_text
from adapters.qqbot.main import QQBotAdapter
from core.controller import NoraController
from core.message_handler import MessageHandlerMixin
from memory.message_history import MessageHistory


class SenderProbe(MessageHandlerMixin):
    _SPLIT_MARKER_PATTERN = re.compile(r'\[SPLIT(?::([0-9.]+))?\]', re.I)

    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def _send_platform_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.anyio
async def test_split_preserves_snapshot_and_partial_error():
    snapshot = DeliveryContext('qqbot', 'u', 'private', ('old',))
    probe = SenderProbe([DeliveryReceipt(['1'], 'first'), DeliveryError('failed', sent_ids=['2'], sent_texts=['second-part'])])
    with pytest.raises(DeliveryError) as caught:
        await probe._send_split_message('qqbot:u', 'first[SPLIT]second', delivery_context=snapshot)
    assert caught.value.sent_ids == ['1', '2']
    assert caught.value.visible_text == 'first\nsecond-part'
    assert all(c[2]['delivery_context'] is snapshot for c in probe.calls)


@pytest.mark.anyio
async def test_visible_reply_total_failure_and_partial_return_only_visible():
    probe = SenderProbe([DeliveryError('fail')])
    assert await probe._send_visible_reply('qqbot:u', 'unsent') == ([], '')
    probe = SenderProbe([DeliveryError('partial', sent_ids=['1'], sent_texts=['sent-only'])])
    assert await probe._send_visible_reply('qqbot:u', 'full-original') == (['1'], 'sent-only')
    assert delivered_text([], 'unsent') == ''


@pytest.mark.anyio
async def test_platform_send_redirect_drops_source_credentials(tmp_path):
    captured = []
    class Adapter:
        platform_name = 'qqbot'
        platform_features = SimpleNamespace(supports_message_controls=True)
        async def send_message(self, chat_id, text, **kwargs):
            captured.append(kwargs)
            return ['sent']
    adapter = Adapter()
    controller = NoraController.__new__(NoraController)
    controller.adapters = {'qqbot': adapter}
    controller.adapter = adapter
    controller.conversation_identities = {}
    snapshot = DeliveryContext('qqbot', 'source', 'private', ('m',))
    await controller._send_platform_message('qqbot:dest', 'x', delivery_context=snapshot, chat_type='group')
    assert captured[0]['delivery_context'].mode == 'proactive'
    assert captured[0]['delivery_context'].source_message_ids == ()
    assert captured[0]['chat_type'] == 'group'


@pytest.mark.anyio
@pytest.mark.parametrize('text', ['/clear', 'hi\n/clear', '/status\n/clear'])
async def test_actual_core_entry_gate_blocks_clear_before_business(tmp_path, text):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    sent = []
    async def send(chat_id, text, **kwargs):
        sent.append(text)
        return ['prompt']
    adapter.send_message = send
    controller = NoraController.__new__(NoraController)
    controller._identity = lambda context: SimpleNamespace(runtime_key='qqbot:g', platform_chat_id='g', platform='qqbot', storage_id='g', actor_user_id='u', chat_type='group', memory_scope_id='scope', place_scope_id='place', actor_display_name='Owner', is_owner=True)
    controller._adapter_for_key = lambda key: adapter
    async def forbidden(*args):
        raise AssertionError('clear business reached without confirmation')
    controller._cmd_clear = forbidden
    # 其余 controller 状态完全不装配：闸门必须在所有命令快路径/取消任务之前返回。
    await controller.handle_new_message({'platform': 'qqbot', 'chat_id': 'g', 'user_id': 'u', 'chat_type': 'group', 'text': text, '_group_listener_promoted': True})
    assert sent


def test_archive_multiple_platforms_keeps_one_message_with_sources():
    history = MessageHistory.__new__(MessageHistory)
    rows = [{'id': 1, 'platform': 'telegram', 'chat_id': 'tg-id', 'summary_text': 'TG archive', 'message_count': 3},
            {'id': 2, 'platform': 'qqbot', 'chat_id': 'qq-id', 'summary_text': 'QQ archive', 'message_count': 4}]
    result = history._build_archive_message(rows)
    assert result['role'] == 'system'
    assert 'platform=telegram, scene=tg-id' in result['content']
    assert 'platform=qqbot, scene=qq-id' in result['content']
    assert 'TG archive' in result['content'] and 'QQ archive' in result['content']


@pytest.mark.anyio
async def test_semi_ignored_full_does_not_swallow_later_at(tmp_path):
    from qqbot_fakes import packet
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    contexts = []
    async def handler(context):
        contexts.append(context)
        return True
    adapter._group_message_handler = handler
    adapter._group_presence_for = lambda key: 'semi_online'
    await adapter.handle_event(packet(mid='full'))
    await adapter.handle_event(packet(event='GROUP_AT_MESSAGE_CREATE', mid='at'))
    assert len(contexts) == 1


def test_backend_internal_results_are_not_dropped_for_lack_of_ids():
    from adapters.delivery import backend_archive_text
    assert backend_archive_text('internal result', [], internal=True) == 'internal result'
    assert backend_archive_text('unsent', [], internal=False) == ''
    assert backend_archive_text('complete original', ['confirmed part'], internal=False) == 'confirmed part'


@pytest.mark.anyio
async def test_cancellation_propagates_and_preserves_partial_ids():
    from adapters.delivery import DeliveryCancelled
    probe = SenderProbe([DeliveryReceipt(['1'], 'first'), asyncio.CancelledError()])
    with pytest.raises(DeliveryCancelled) as caught:
        await probe._send_split_message('qqbot:u', 'first[SPLIT]second')
    assert caught.value.sent_ids == ['1']
    assert caught.value.visible_text == 'first'


@pytest.mark.anyio
async def test_cancelled_visible_reply_archives_only_partial_and_still_cancels():
    from adapters.delivery import DeliveryCancelled
    probe = SenderProbe([DeliveryCancelled(sent_ids=['1'], sent_texts=['visible-only'])])
    probe._identity_for_runtime_key = lambda key: SimpleNamespace(platform='qqbot', storage_id='u', memory_scope_id='s', place_scope_id='p')
    saved = []
    probe.message_history = SimpleNamespace(add_message=lambda **kwargs: saved.append(kwargs))
    probe.sessions = {}
    with pytest.raises(DeliveryCancelled):
        await probe._send_visible_reply('qqbot:u', 'complete original')
    assert saved[0]['content'] == 'visible-only'
    assert saved[0]['metadata']['platform_message_ids'] == ['1']


def test_other_bot_mentions_do_not_wake_self():
    from adapters.qqbot.message import event_to_context
    from qqbot_fakes import packet
    event = packet(content='<@other-bot> hello')
    event['d']['mentions'] = [{'member_openid': 'other-bot', 'bot': True}]
    context = event_to_context(event, self_id='self')
    assert not context['explicit_bot_mention']
