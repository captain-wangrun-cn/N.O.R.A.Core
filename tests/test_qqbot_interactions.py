from types import SimpleNamespace

import pytest

from adapters.qqbot.commands import before_command
from adapters.qqbot.interactions import InteractionStore, handle_interaction
from qqbot_fakes import API


def identity(owner=True, user='u', chat='g', chat_type='group'):
    return SimpleNamespace(is_owner=owner, actor_user_id=user, platform_chat_id=chat, chat_type=chat_type, runtime_key='qqbot:' + chat)


class Adapter:
    def __init__(self, owner=True):
        self.interactions = InteractionStore()
        self.client = API()
        self.sent = []
        self.dispatched = []
        self.owner = owner
        from adapters.qqbot.state import QQBotState
        self.state = QQBotState()
        class Controller:
            def _identity(inner, context):
                return identity(owner, context['user_id'], context['chat_id'], context['chat_type'])
            async def handler(inner, context):
                pass
        self._message_handler = Controller().handler

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return ['sent']

    async def dispatch_message(self, context):
        self.dispatched.append(context)


def test_tokens_bound_expiring_single_use_and_old_menu_invalid():
    now = [0.0]
    store = InteractionStore(clock=lambda: now[0])
    menu = store.menu('u', 'g', 'group', 'clear', [('clear', '/clear')])
    token = menu['content']['rows'][0]['buttons'][0]['action']['data']
    assert store.consume(token, 'other', 'g', 'group') is None
    assert store.consume(token, 'u', 'other', 'group') is None
    assert store.consume(token, 'u', 'g', 'private') is None
    action = store.consume(token, 'u', 'g', 'group')
    assert action and store.consume(token, 'u', 'g', 'group') is None
    grant = store.grant(action)
    assert store.consume_grant(grant, 'u', 'g', 'group', '/clear')
    assert not store.consume_grant(grant, 'u', 'g', 'group', '/clear')
    first = store.menu('u', 'g', 'group', 'clear', [('clear', '/clear')])
    old = first['content']['rows'][0]['buttons'][0]['id']
    store.menu('u', 'g', 'group', 'clear', [('clear', '/clear')])
    assert store.consume(old, 'u', 'g', 'group') is None
    latest = next(reversed(store.actions))
    now[0] = 181
    assert store.consume(latest, 'u', 'g', 'group') is None


@pytest.mark.anyio
async def test_ack_d_id_not_outer_event_and_duplicate_once():
    adapter = Adapter()
    menu = adapter.interactions.menu('u', 'g', 'group', 'clear', [('clear', '/clear')])
    token = menu['content']['rows'][0]['buttons'][0]['id']
    packet = {'id': 'OUTER_EVENT', 'd': {'id': 'INTERACTION_CREATE:INNER_ID', 'type': 11, 'scene': 'group', 'group_openid': 'g', 'group_member_openid': 'u', 'data': {'resolved': {'button_data': token}}}}
    await handle_interaction(adapter, packet)
    await handle_interaction(adapter, packet)
    assert adapter.client.calls[0][1] == '/interactions/INNER_ID'
    assert adapter.client.calls[0][2]['json'] == {'code': 0}
    assert len(adapter.client.calls) == 1 and len(adapter.dispatched) == 1
    credential = adapter.state.credential_for('group', 'g', ('INNER_ID',))
    assert credential.kind == 'event_id' and credential.value == 'OUTER_EVENT'
    context = adapter.dispatched[0]
    assert not await before_command(adapter, context, identity())  # 准许已确认 /clear。
    assert await before_command(adapter, context, identity())  # grant 已消费，必须重新确认。


@pytest.mark.anyio
@pytest.mark.parametrize('owner', [False, True])
async def test_bad_button_ack_denied_no_business(owner):
    adapter = Adapter(owner)
    packet = {'d': {'id': 'i', 'type': 11, 'scene': 'c2c', 'user_openid': 'u', 'data': {'resolved': {'button_data': '/clear'}}}}
    await handle_interaction(adapter, packet)
    assert adapter.client.calls[0][2]['json'] == {'code': 4}
    assert not adapter.dispatched


@pytest.mark.anyio
@pytest.mark.parametrize('text', ['/clear', 'hello\n/clear', 'A: hello\n/clear', '/status\n/clear'])
async def test_unconfirmed_clear_never_reaches_core(text):
    adapter = Adapter()
    assert await before_command(adapter, {'text': text}, identity())
    assert adapter.sent


@pytest.mark.anyio
async def test_nonowner_mutations_denied_and_settings_use_helpers(monkeypatch):
    adapter = Adapter(False)
    for command in ['/clear', '/model smart x', '/effort fast high', '/set_stream off', '/undo']:
        assert await before_command(adapter, {'text': command}, identity(False))
        assert '主人' in adapter.sent[-1][1]
    changes = []
    monkeypatch.setattr('config.set_llm_effort_by_alias', lambda *args: changes.append(args))
    assert await before_command(adapter, {'text': '/effort smart high'}, identity(True))
    assert changes == [('smart', 'high')]
    assert await before_command(adapter, {'text': '/effort smart INVALID'}, identity(True))
    assert changes == [('smart', 'high')]


@pytest.mark.anyio
async def test_menu_buttons_correct_callback_type_and_user_permission():
    adapter = Adapter()
    assert await before_command(adapter, {'text': '/set_stream'}, identity())
    keyboard = adapter.sent[-1][2]['keyboard']
    action = keyboard['content']['rows'][0]['buttons'][0]['action']
    assert action['type'] == 1
    assert action['permission'] == {'type': 0, 'specify_user_ids': ['u']}
    assert action['data'] != '/set_stream on'


@pytest.mark.anyio
async def test_nested_records_and_multi_actor_batch_cannot_execute_commands():
    adapter = Adapter()
    assert await before_command(adapter, {'text': '/clear', 'qqbot_has_nested_elements': True}, identity())
    assert await before_command(adapter, {'text': '/clear', 'group_message_batch': [{'user_id': 'visitor'}, {'user_id': 'owner'}]}, identity())
    assert all('确认永久清空' not in str(item) for item in adapter.sent)


@pytest.mark.anyio
async def test_nora_preferences_validation_no_nan_or_unknown(monkeypatch):
    adapter = Adapter()
    changes = []
    monkeypatch.setattr('config.get_nora_preferences', lambda: {'verbosity_preference': 0.5})
    monkeypatch.setattr('config.set_nora_preferences', lambda arg: changes.append(arg))
    for arg in ['unknown 0.2', 'verbosity_preference nan', 'verbosity_preference 1.1']:
        assert await before_command(adapter, {'text': '/nora_prefs ' + arg}, identity())
    assert not changes
    await before_command(adapter, {'text': '/nora_prefs verbosity_preference 0.2'}, identity())
    assert changes == [{'verbosity_preference': 0.2}]
