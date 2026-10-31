import asyncio

import pytest

from adapters.qqbot.client import QQBotAPIError, QQBotClient
from adapters.qqbot.constants import INTENTS, TOKEN_URL
from qqbot_fakes import Response, Session, WebSocket


@pytest.mark.anyio
async def test_token_refresh_mutex_and_expiry():
    now = [0.0]
    session = Session([Response({'access_token': 'a', 'expires_in': 7200}), Response({'access_token': 'b', 'expires_in': 7200})])
    client = QQBotClient('app', 'secret', session=session, clock=lambda: now[0])
    assert await asyncio.gather(*(client.access_token() for _ in range(20))) == ['a'] * 20
    assert len(session.calls) == 1
    assert session.calls[0][1] == TOKEN_URL
    assert session.calls[0][2]['json'] == {'appId': 'app', 'clientSecret': 'secret'}
    now[0] = 7141
    assert await client.access_token() == 'b'
    await client.close()
    assert not session.closed  # 注入会话由调用者管理。


@pytest.mark.anyio
async def test_rest_401_refresh_once_and_sanitized_error():
    session = Session([Response({'access_token': 'a'}), Response({}, 401), Response({'access_token': 'b'}), Response({'code': 40034024, 'message': 'secret URL'}, 403)])
    client = QQBotClient('app', 'secret', session=session)
    with pytest.raises(QQBotAPIError) as caught:
        await client.request('POST', '/v2/users/user/messages', json={'content': 'x'})
    assert caught.value.code == 40034024
    assert 'secret' not in str(caught.value)
    calls = [c for c in session.calls if not c[1].endswith('getAppAccessToken')]
    assert [c[2]['headers']['Authorization'] for c in calls] == ['QQBot a', 'QQBot b']
    assert all(c[2]['allow_redirects'] is False for c in calls)


@pytest.mark.anyio
async def test_hello_identify_ready_dispatch_and_resume():
    client = QQBotClient('app', 'secret', session=Session([Response({'access_token': 'a'})]))
    events, ready = [], []
    async def on_event(event):
        events.append(event)
    async def on_ready():
        ready.append(True)
    ws = WebSocket([{'op': 10, 'd': {'heartbeat_interval': 45000}}, {'op': 0, 's': 1, 't': 'READY', 'd': {'session_id': 'session', 'user': {'id': 'bot'}}}, {'op': 0, 's': 2, 't': 'C2C_MESSAGE_CREATE', 'd': {}}, {'op': 1}])
    await client._receive(ws, on_event, on_ready)
    await client._stop_heartbeat()
    assert ws.sent[0] == {'op': 2, 'd': {'token': 'QQBot a', 'intents': INTENTS, 'shard': [0, 1], 'properties': {}}}
    assert ws.sent[1] == {'op': 1, 'd': 2}
    assert client.seq == 2 and client.session_id == 'session'
    assert len(events) == 1 and len(ready) == 1
    resumed = WebSocket([{'op': 10, 'd': {'heartbeat_interval': 45000}}, {'op': 0, 's': 3, 't': 'RESUMED', 'd': {}}])
    await client._receive(resumed, on_event, on_ready)
    await client._stop_heartbeat()
    assert resumed.sent[0]['op'] == 6
    assert resumed.sent[0]['d'] == {'token': 'QQBot a', 'session_id': 'session', 'seq': 2}
    assert len(ready) == 2
    await client.close()


@pytest.mark.anyio
async def test_queue_failure_does_not_advance_sequence():
    client = QQBotClient('app', 'secret', session=Session())
    client.seq = 4
    async def fail(event):
        raise asyncio.QueueFull
    async def ready():
        pass
    with pytest.raises(asyncio.QueueFull):
        await client._receive(WebSocket([{'op': 0, 's': 5, 't': 'MESSAGE', 'd': {}}]), fail, ready)
    assert client.seq == 4


@pytest.mark.anyio
async def test_invalid_session_clears_resume_state():
    client = QQBotClient('app', 'secret', session=Session())
    client.session_id, client.seq = 's', 42
    async def ignore(*args):
        pass
    await client._receive(WebSocket([{'op': 9, 'd': False}]), ignore, ignore)
    assert client.session_id == '' and client.seq is None


@pytest.mark.anyio
async def test_missing_heartbeat_ack_closes_socket():
    client = QQBotClient('app', 'secret', session=Session())
    client._ws = WebSocket([])
    client._awaiting_ack = True
    await client._heartbeat(0.001)
    assert client._ws.closed


@pytest.mark.anyio
async def test_close_owns_session_and_cancels_heartbeat():
    client = QQBotClient('app', 'secret', session=Session())
    client._owns_session = True
    client._heartbeat_task = asyncio.create_task(asyncio.Event().wait())
    await client.close()
    assert client.session.closed and client._heartbeat_task is None


@pytest.mark.anyio
async def test_path_rejects_absolute_and_protocol_relative():
    client = QQBotClient('app', 'secret', session=Session())
    for path in ['https://attacker.invalid', '//attacker.invalid']:
        with pytest.raises(ValueError):
            await client.request('GET', path)
