import asyncio

import pytest

from adapters.qqbot.main import QQBotAdapter
from adapters.qqbot.message import event_to_context, render_elements, scene_ext
from adapters.qqbot.state import QQBotState
from qqbot_fakes import packet


def test_event_opaque_ids_and_three_identifiers_are_separate():
    event = packet(user='OPEN_user', chat='OPEN_group', mid='official-id', idx='native-index')
    context = event_to_context(event)
    assert context['chat_id'] == 'OPEN_group'
    assert context['platform_message_ids'] == ['official-id']
    assert scene_ext(event['d'])['msg_idx'] == 'native-index'
    assert 'auth_token' not in str(context)
    private = event_to_context(packet(event='C2C_MESSAGE_CREATE', user='OPEN_user'))
    assert private['chat_id'] == 'OPEN_user' and private['chat_type'] == 'private'


def test_mentions_bot_and_all_are_not_user_controls():
    event = packet(content='<@bot> <@member> <@all> hi')
    event['d']['mentions'] = [{'member_openid': 'bot', 'bot': True}, {'member_openid': 'member', 'username': 'Other'}]
    context = event_to_context(event, self_id='bot')
    assert context['explicit_bot_mention']
    assert context['mentioned_user_ids'] == ['member']
    assert context['text'].startswith('[at:member]')
    assert '[at:all]' not in context['text'] and '@全体成员' in context['text']


@pytest.mark.parametrize('mention', [
    {'id': 'ready-id', 'member_openid': 'group-bot-id'},
    {'id': 'different-id', 'member_openid': 'group-bot-id', 'bot': True, 'is_you': True},
    {'is_you': True},
])
def test_full_group_self_mention_uses_server_identity(mention):
    event = packet(content='<@group-bot-id> hello')
    event['d']['mentions'] = [mention, {'member_openid': 'other-bot', 'bot': True}]
    context = event_to_context(event, self_id='ready-id')
    assert context['explicit_bot_mention']
    assert 'group-bot-id' not in context['mentioned_user_ids'] or not mention.get('member_openid')
    assert 'other-bot' in context['mentioned_user_ids']


@pytest.mark.parametrize('flag', [False, 'true', 1, None])
def test_full_group_other_bot_is_not_self_from_truthy_flag(flag):
    event = packet(content='<@other-bot> hello')
    event['d']['mentions'] = [{'id': 'other-id', 'member_openid': 'other-bot', 'bot': True, 'is_you': flag}]
    assert not event_to_context(event, self_id='ready-id')['explicit_bot_mention']


@pytest.mark.anyio
async def test_full_group_marked_self_mention_passes_semi_gate(tmp_path):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    adapter.self_id = 'ready-id'
    adapter._group_presence_for = lambda key: 'semi_online'
    contexts = []

    async def handler(context):
        contexts.append(context)

    adapter._message_handler = handler
    event = packet(content='<@group-bot-id> hello')
    event['d']['mentions'] = [{'member_openid': 'group-bot-id', 'is_you': True, 'bot': True}]
    await adapter.handle_event(event)
    assert len(contexts) == 1 and contexts[0]['explicit_bot_mention']
    assert '[at:group-bot-id]' not in contexts[0]['text']


def test_reply_to_own_reference_resolves_message_id():
    state = QQBotState()
    state.remember_reference('group', 'g1', 'bot-msg', 'ref-idx', is_bot=True)
    event = packet()
    event['d']['message_scene']['ext'].append('ref_msg_idx=ref-idx')
    context = event_to_context(event, state=state)
    assert context['reply_to_bot']
    assert context['reply_to_message_id'] == 'bot-msg'
    assert context['platform_message_ids'] == ['m1']


def test_recursive_records_are_bounded_with_attachment_placeholders():
    element = {'author': {'username': 'A'}, 'content': 'text', 'attachments': [{'content_type': 'image/gif', 'url': 'https://secret.invalid?rkey=secret'}]}
    element['msg_elements'] = [element]
    rendered = render_elements([element], max_depth=2)
    assert 'image附件' in rendered
    assert '预算' in rendered and 'secret' not in rendered
    assert len(render_elements([{'content': 'x' * 50000}])) < 8200


@pytest.mark.anyio
@pytest.mark.parametrize('online,directed,expected', [(True, False, True), (False, False, False), (False, True, True)])
async def test_group_presence_gate_before_download_and_aggregation(tmp_path, online, directed, expected):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    contexts = []
    async def group_handler(context):
        contexts.append(context)
        return True
    adapter._group_presence_for = lambda key: 'online' if online else 'semi_online'
    adapter._group_message_handler = group_handler
    event = packet(event='GROUP_AT_MESSAGE_CREATE' if directed else 'GROUP_MESSAGE_CREATE')
    await adapter.handle_event(event)
    assert bool(contexts) == expected
    assert bool(adapter._event_queue.maxsize)


@pytest.mark.anyio
async def test_full_at_overlap_and_resume_replay_deduplicated(tmp_path):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    adapter._group_presence_for = lambda key: 'online'
    seen = []
    async def handler(context):
        seen.append(context)
        return True
    adapter._group_message_handler = handler
    await adapter.handle_event(packet(mid='full'))
    await adapter.handle_event(packet(event='GROUP_AT_MESSAGE_CREATE', mid='at'))
    await adapter.handle_event(packet(mid='full'))
    await adapter.handle_event(packet(mid='different', idx='idx-2'))
    assert len(seen) == 2


@pytest.mark.anyio
async def test_aggregated_snapshot_keeps_all_ids_and_extra_fields(tmp_path):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    contexts = []
    async def handler(context):
        contexts.append(context)
    adapter._message_handler = handler
    from adapters.aggregator import MessageAggregator
    adapter._aggregator = MessageAggregator(0.001, adapter.on_aggregator_complete)
    for mid in ['one', 'two']:
        await adapter.handle_event(packet(event='C2C_MESSAGE_CREATE', mid=mid, idx=mid))
    await asyncio.gather(*list(adapter._aggregator._timers.values()))
    assert len(contexts) == 1
    assert contexts[0]['delivery_context'].source_message_ids == ('one', 'two')
    assert len(contexts[0]['contributors']) == 2


@pytest.mark.anyio
async def test_group_senders_are_not_aggregated_together(tmp_path):
    from adapters.aggregator import MessageAggregator
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    contexts = []
    async def handler(context):
        contexts.append(context)
    adapter._message_handler = handler
    adapter._aggregator = MessageAggregator(0.001, adapter.on_aggregator_complete)
    for user in ['a', 'b']:
        await adapter.handle_event(packet(event='GROUP_AT_MESSAGE_CREATE', user=user, mid=user, idx=user))
    await asyncio.gather(*list(adapter._aggregator._timers.values()))
    assert len(contexts) == 2


def test_metadata_and_capabilities_without_loading_private_config(tmp_path):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    assert adapter.metadata.adapter_id == 'qqbot'
    assert adapter.platform_features.supports_delete_message
    assert not adapter.platform_features.supports_typing_indicator
    assert not adapter.platform_features.supports_edit_message
    assert adapter.get_adapter_tools() == []


def test_actual_qqbot_prompt_is_loaded_by_platform_name():
    from brain.prompts import load_adapter_prompt
    block = load_adapter_prompt('qqbot')
    assert 'QQ 官方机器人' in block
    assert 'OneBot/NapCat' in block


def test_selection_three_platforms_and_qq_alias(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('qqbot_test_entrypoint', Path(__file__).resolve().parents[1] / 'main.py')
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    cfg = {'telegram': {'bot_token': '123:ABC'}, 'onebotv11': {'websocket_url': 'ws://localhost'}, 'qqbot': {'app_id': 'app', 'app_secret': 'secret'}}
    monkeypatch.setattr(main, 'read_adapter_config', lambda name: cfg.get(name))
    assert main.resolve_adapter_names('auto') == ['onebotv11', 'telegram', 'qqbot']
    assert main.resolve_adapter_names('qq,qqbot,telegram,qqbot') == ['onebotv11', 'qqbot', 'telegram']
    cfg['qqbot']['app_secret'] = 'YOUR_SECRET'
    assert not main._qqbot_configured()


@pytest.mark.anyio
async def test_aggregation_preserves_nested_command_safety_flag(tmp_path):
    from adapters.aggregator import MessageAggregator
    contexts = []
    async def handler(context):
        contexts.append(context)
    aggregator = MessageAggregator(0.001, handler)
    base = {'platform': 'qqbot', 'chat_type': 'private', 'chat_id': 'u', 'user_id': 'u'}
    await aggregator.add_message('u', 'quoted record', {**base, 'platform_message_id': '1', 'qqbot_has_nested_elements': True})
    await aggregator.add_message('u', '/clear', {**base, 'platform_message_id': '2', 'qqbot_has_nested_elements': False})
    await asyncio.gather(*list(aggregator._timers.values()))
    assert contexts[0]['qqbot_has_nested_elements']


@pytest.mark.anyio
async def test_relation_reject_and_receive_toggle_target(tmp_path):
    from adapters.delivery import DeliveryError
    from adapters.qqbot.sender import QQBotSender
    from qqbot_fakes import API, Media
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    adapter.sender = QQBotSender(API(), adapter.state, Media())
    await adapter.handle_event({'t': 'C2C_MSG_REJECT', 'd': {'openid': 'u'}})
    with pytest.raises(DeliveryError):
        await adapter.send_message('u', 'notification', chat_type='private')
    await adapter.handle_event({'t': 'C2C_MSG_RECEIVE', 'd': {'openid': 'u'}})
    assert await adapter.send_message('u', 'notification', chat_type='private')


@pytest.mark.anyio
async def test_shutdown_cancels_workers_aggregator_and_interactions(tmp_path):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
    adapter._worker = tasks[0]
    adapter._interaction_tasks.add(tasks[1])
    adapter._aggregator = type('Aggregator', (), {'_timers': {'x': tasks[2]}})()
    await adapter.shutdown()
    assert all(task.cancelled() for task in tasks)
    assert not adapter.is_running


@pytest.mark.anyio
async def test_group_diagnostics_distinguish_gateway_gate_and_core_without_payload(tmp_path, caplog):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    adapter._group_presence_for = lambda key: 'semi_online'
    contexts = []

    async def handler(context):
        contexts.append(context)

    adapter._message_handler = handler
    event = packet(content='PRIVATE_BODY', chat='PRIVATE_GROUP_ID', user='PRIVATE_USER_ID')
    event['d']['attachments'] = [{'url': 'https://example.invalid?rkey=PRIVATE_RKEY'}]
    with caplog.at_level('INFO', logger='adapters.qqbot.main'):
        await adapter._enqueue_event(event)
        await adapter.handle_event(adapter._event_queue.get_nowait())
        await adapter.handle_event(packet(event='GROUP_AT_MESSAGE_CREATE', content='PRIVATE_BODY', chat='PRIVATE_GROUP_ID', user='PRIVATE_USER_ID'))
    assert len(contexts) == 1
    for marker in ['gateway dispatch: event=GROUP_MESSAGE_CREATE', 'action=ignore_non_directed', 'action=accept', 'core dispatch:']:
        assert marker in caplog.text
    for secret in ['PRIVATE_BODY', 'PRIVATE_GROUP_ID', 'PRIVATE_USER_ID', 'PRIVATE_RKEY', 'auth_token']:
        assert secret not in caplog.text


@pytest.mark.anyio
async def test_group_diagnostics_missing_ids_and_bot_author(tmp_path, caplog):
    adapter = QQBotAdapter({}, workspace_root=tmp_path)
    event = packet()
    event['d']['author'] = {}
    with caplog.at_level('INFO', logger='adapters.qqbot.main'):
        await adapter.handle_event(event)
        event['d']['author'] = {'bot': True}
        await adapter.handle_event(event)
    assert 'reason=missing_ids' in caplog.text
    assert 'reason=bot_author' in caplog.text


def test_state_caches_bounded_and_scene_isolated():
    state = QQBotState(max_size=2)
    for mid in ['a', 'b', 'c']:
        state.remember_credential('private', 'u', mid)
        state.is_duplicate('private', 'u', mid)
    assert len(state.credentials) == 2 and len(state.seen) == 2
    assert state.credential_for('group', 'u', ('c',)) is None
