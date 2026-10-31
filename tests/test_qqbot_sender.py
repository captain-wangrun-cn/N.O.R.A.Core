import asyncio

import pytest

from adapters.delivery import DeliveryContext, DeliveryError
from adapters.qqbot.client import QQBotAPIError
from adapters.qqbot.sender import QQBotSender
from adapters.qqbot.state import QQBotState
from qqbot_fakes import API, Media


@pytest.mark.anyio
async def test_response_snapshot_never_borrows_latest_or_reply_target():
    state = QQBotState()
    state.remember_credential('group', 'g', 'old')
    snapshot = DeliveryContext('qqbot', 'g', 'group', ('old',))
    state.remember_credential('group', 'g', 'new')
    sender = QQBotSender(API(), state, Media())
    await sender.send('g', '[reply:new]answer', chat_type='group', delivery_context=snapshot)
    assert sender.client.calls[0][2]['json']['msg_id'] == 'old'
    await sender.send('g', 'notification', chat_type='group')
    assert 'msg_id' not in sender.client.calls[1][2]['json']


@pytest.mark.anyio
async def test_budget_shared_across_calls_and_media_then_active():
    state = QQBotState()
    state.remember_credential('private', 'u', 'm')
    sender = QQBotSender(API(), state, Media())
    snapshot = DeliveryContext('qqbot', 'u', 'private', ('m',))
    for _ in range(3):
        await sender.send('u', 'front/back', chat_type='private', delivery_context=snapshot)
    await sender.send('u', '[voice: data/a.oga]', chat_type='private', delivery_context=snapshot)
    await sender.send('u', 'tail', chat_type='private', delivery_context=snapshot)
    bodies = [c[2]['json'] for c in sender.client.calls]
    assert [b.get('msg_seq') for b in bodies] == [1, 2, 3, 4, None]
    assert bodies[3]['msg_type'] == 7
    assert 'msg_id' not in bodies[-1]


@pytest.mark.anyio
async def test_same_scene_concurrent_users_keep_own_credentials():
    state = QQBotState()
    for mid in ['a', 'b']:
        state.remember_credential('group', 'g', mid)
    sender = QQBotSender(API(), state, Media())
    await asyncio.gather(*(sender.send('g', mid, chat_type='group', delivery_context=DeliveryContext('qqbot', 'g', 'group', (mid,))) for mid in ['a', 'b', 'a', 'b']))
    calls = [c[2]['json'] for c in sender.client.calls]
    for mid in ['a', 'b']:
        assert [c['msg_seq'] for c in calls if c['msg_id'] == mid] == [1, 2]


@pytest.mark.anyio
async def test_explicit_expiry_falls_back_active_not_invalid_permission():
    state = QQBotState()
    state.remember_credential('group', 'g', 'm')
    api = API([QQBotAPIError(400, 40034005), {'id': 'sent'}])
    sender = QQBotSender(api, state, Media())
    ids = await sender.send('g', 'x', chat_type='group', delivery_context=DeliveryContext('qqbot', 'g', 'group', ('m',)))
    assert ids == ['sent']
    assert api.calls[0][2]['json']['msg_id'] == 'm'
    assert 'msg_id' not in api.calls[1][2]['json']
    api.results = [QQBotAPIError(403, 40034024)]
    with pytest.raises(DeliveryError):
        await sender.send('g', 'x', chat_type='group')
    assert len(api.calls) == 3


@pytest.mark.anyio
async def test_expired_clock_and_redirect_use_active():
    now = [0.0]
    state = QQBotState(clock=lambda: now[0])
    state.remember_credential('private', 'u', 'm', ttl=300)
    sender = QQBotSender(API(), state, Media())
    snapshot = DeliveryContext('qqbot', 'u', 'private', ('m',))
    now[0] = 301
    await sender.send('u', 'x', chat_type='private', delivery_context=snapshot)
    await sender.send('g', 'x', chat_type='group', delivery_context=snapshot)
    assert all('msg_id' not in c[2]['json'] for c in sender.client.calls)


@pytest.mark.anyio
async def test_timeout_is_uncertain_and_never_retried_as_active():
    api = API([asyncio.TimeoutError()])
    state = QQBotState()
    state.remember_credential('private', 'u', 'm')
    sender = QQBotSender(api, state, Media())
    with pytest.raises(DeliveryError) as caught:
        await sender.send('u', 'x', chat_type='private', delivery_context=DeliveryContext('qqbot', 'u', 'private', ('m',)))
    assert caught.value.uncertain and not caught.value.sent_ids
    assert len(api.calls) == 1


@pytest.mark.anyio
async def test_failed_upload_logs_stage_without_file_or_exception_secret(caplog):
    class FailedMedia:
        async def upload(self, *args):
            raise ValueError('PRIVATE_SECRET')

    api = API()
    sender = QQBotSender(api, QQBotState(), FailedMedia())
    with caplog.at_level('WARNING', logger='adapters.qqbot.sender'):
        with pytest.raises(DeliveryError):
            await sender.send('PRIVATE_SCENE', '[file: PRIVATE_PATH]', chat_type='private')
    assert 'stage=upload kind=file sent=0' in caplog.text
    assert 'error=ValueError' in caplog.text
    for value in ['PRIVATE_SECRET', 'PRIVATE_PATH', 'PRIVATE_SCENE']:
        assert value not in caplog.text
    assert not api.calls


@pytest.mark.anyio
async def test_partial_failure_keeps_only_confirmed_chunks():
    api = API([{'id': 'first'}, QQBotAPIError(403, 40054013)])
    sender = QQBotSender(api, QQBotState(), Media(), chunk_size=64)
    with pytest.raises(DeliveryError) as caught:
        await sender.send('u', 'a' * 100, chat_type='private')
    assert caught.value.sent_ids == ['first']
    assert caught.value.visible_text == 'a' * 64
    assert len(api.calls) == 2


@pytest.mark.anyio
async def test_rate_limit_retry_reuses_sequence(monkeypatch):
    async def no_sleep(delay):
        pass
    monkeypatch.setattr('adapters.qqbot.sender.asyncio.sleep', no_sleep)
    api = API([QQBotAPIError(429, retry_after=1), {'id': 'sent'}])
    state = QQBotState()
    state.remember_credential('private', 'u', 'm')
    sender = QQBotSender(api, state, Media())
    await sender.send('u', 'x', chat_type='private', delivery_context=DeliveryContext('qqbot', 'u', 'private', ('m',)))
    assert [c[2]['json']['msg_seq'] for c in api.calls] == [1, 1]


@pytest.mark.anyio
async def test_native_reply_requires_scene_mapping_and_returns_receipt():
    state = QQBotState()
    state.remember_reference('group', 'g', 'incoming', 'msg-idx')
    sender = QQBotSender(API(), state, Media())
    receipt = await sender.send('g', '[reply:incoming][at:user][poke:user]hello', chat_type='group')
    body = sender.client.calls[0][2]['json']
    assert body['message_reference'] == {'message_id': 'msg-idx'}
    assert body['markdown']['content'] == 'hello'
    assert receipt.visible_text == 'hello'
    sent = state.reference_for('group', 'g', receipt[0])
    assert sent.index == 'ref-1' and sent.is_bot
    await sender.send('other', '[reply:incoming]hello', chat_type='group')
    assert 'message_reference' not in sender.client.calls[-1][2]['json']


@pytest.mark.anyio
async def test_delete_only_own_message_within_two_minutes():
    now = [0.0]
    state = QQBotState(clock=lambda: now[0])
    sender = QQBotSender(API(), state, Media())
    state.remember_reference('group', 'g', 'user', 'i', is_bot=False)
    state.remember_reference('group', 'g', 'bot', 'b', is_bot=True)
    assert not await sender.delete('g', 'user', chat_type='group')
    assert not await sender.delete('other', 'bot', chat_type='group')
    assert await sender.delete('g', 'bot', chat_type='group')
    now[0] = 120
    assert not await sender.delete('g', 'bot', chat_type='group')


@pytest.mark.anyio
async def test_empty_controls_only_do_not_send_and_unknown_scene_rejected():
    sender = QQBotSender(API(), QQBotState(), Media())
    assert await sender.send('u', '[poke:u]', chat_type='private') == []
    with pytest.raises(ValueError):
        await sender.send('opaque', 'x', chat_type='')


@pytest.mark.anyio
async def test_send_cancellation_is_not_wrapped_as_retryable_error():
    from adapters.delivery import DeliveryCancelled
    sender = QQBotSender(API([{'id': '1'}, asyncio.CancelledError()]), QQBotState(), Media(), chunk_size=64)
    with pytest.raises(DeliveryCancelled) as caught:
        await sender.send('u', 'a' * 100, chat_type='private')
    assert caught.value.sent_ids == ['1']
    assert caught.value.visible_text == 'a' * 64
    assert len(sender.client.calls) == 2


@pytest.mark.anyio
async def test_missing_id_is_uncertain_not_fake_success():
    sender = QQBotSender(API([{}]), QQBotState(), Media())
    with pytest.raises(DeliveryError) as caught:
        await sender.send('u', 'x', chat_type='private')
    assert caught.value.uncertain
