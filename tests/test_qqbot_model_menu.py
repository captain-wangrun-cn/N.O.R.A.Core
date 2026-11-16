"""qqbot /model 与 /effort 按钮菜单链：别名 → Provider → 模型列表（分页）→ 切换/清除。"""

import copy
from types import SimpleNamespace

import pytest

from adapters.qqbot.commands import CatalogStore, CatalogEntry, before_command
from adapters.qqbot.interactions import InteractionStore


def identity(owner=True, user='u', chat='g', chat_type='group'):
    return SimpleNamespace(is_owner=owner, actor_user_id=user, platform_chat_id=chat,
                           chat_type=chat_type, runtime_key='qqbot:' + chat)


class Adapter:
    def __init__(self, owner=True):
        self.interactions = InteractionStore()
        self.model_catalogs = CatalogStore()
        self.sent = []
        self.reloads = []
        self.owner = owner

        class Controller:
            def _identity(inner, context):
                return identity(owner, context['user_id'], context['chat_id'], context['chat_type'])

            async def handler(inner, context):
                pass

            async def _cmd_reload_models(inner, runtime_key):
                self.reloads.append(runtime_key)

        self._message_handler = Controller().handler

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return ['sent']


def button_commands(adapter, keyboard):
    # 按钮 action.data 是 token，真实命令挂在 InteractionStore.actions 里。
    out = []
    for row in keyboard['content']['rows']:
        for button in row['buttons']:
            action = adapter.interactions.actions[button['action']['data']]
            out.append((button['render_data']['label'], action.command))
    return out


def find_command(adapter, keyboard, prefix):
    return [item for item in button_commands(adapter, keyboard) if item[1].startswith(prefix)]


def config_stub(monkeypatch, llm=None):
    state = {'llm': copy.deepcopy(llm or {'providers': {'openrouter': {'type': 'openai', 'api_key': 'k'},
                                                     'gemini': {'type': 'gemini', 'api_key': 'k'}},
                                          'models': {'smart': 'gpt-4o'}})}
    saved = []
    monkeypatch.setattr('config.get_config', lambda: copy.deepcopy(state))
    monkeypatch.setattr('config.save_config', lambda cfg: saved.append(copy.deepcopy(cfg)))
    return state, saved


@pytest.mark.anyio
async def test_model_alias_menu_lists_current_models_with_buttons(monkeypatch):
    config_stub(monkeypatch)
    adapter = Adapter()
    await before_command(adapter, {'text': '/model'}, identity())
    text, kwargs = adapter.sent[-1][1], adapter.sent[-1][2]
    assert '**🧠 模型切换**' in text
    keyboard = kwargs['keyboard']
    smart = find_command(adapter, keyboard, '/model_pick smart')
    assert smart and smart[0][0].startswith('smart: gpt-4o')
    rows = keyboard['content']['rows']
    assert sum(len(row['buttons']) for row in rows) == len(__import__('config').LLM_MODEL_ALIASES) + 1
    # 10 个别名 + 取消，3 列排布应 ≤ 5 行（QQ 官方 keyboard 行数上限）。
    assert len(rows) <= 5


@pytest.mark.anyio
async def test_model_pick_renders_provider_menu(monkeypatch):
    config_stub(monkeypatch)
    adapter = Adapter()
    await before_command(adapter, {'text': '/model_pick smart'}, identity())
    keyboard = adapter.sent[-1][2]['keyboard']
    commands = [cmd for _, cmd in button_commands(adapter, keyboard)]
    assert '/model_list smart openrouter' in commands
    assert '/model_list smart gemini' in commands
    assert '/model' in commands and '/qqbot_cancel' in commands


@pytest.mark.anyio
async def test_model_pick_rejects_unknown_alias(monkeypatch):
    config_stub(monkeypatch)
    adapter = Adapter()
    await before_command(adapter, {'text': '/model_pick nope'}, identity())
    assert '❌' in adapter.sent[-1][1]


@pytest.mark.anyio
async def test_model_list_paginates_and_model_set_applies_and_reloads(monkeypatch):
    state, saved = config_stub(monkeypatch)
    models = [f'model-{i:02d}-x' for i in range(15)]
    monkeypatch.setattr('adapters.qqbot.commands.fetch_provider_models', lambda *args: list(models))
    adapter = Adapter()

    await before_command(adapter, {'text': '/model_list smart openrouter'}, identity())
    keyboard = adapter.sent[-1][2]['keyboard']
    options = find_command(adapter, keyboard, '/model_set smart ')
    assert len(options) == 6
    assert [label for label, _ in options] == models[:6]
    # 6 选项(2列=3行) + 翻页 + 返回/取消 ≤ 5 行。
    assert len(keyboard['content']['rows']) <= 5
    assert find_command(adapter, keyboard, '/model_page ')
    page_token = find_command(adapter, keyboard, '/model_page ')[0][1].split()[1]
    assert adapter.reloads == []

    # 翻页：第二页显示后续模型。
    await before_command(adapter, {'text': f'/model_page {page_token} 6'}, identity())
    options2 = find_command(adapter, adapter.sent[-1][2]['keyboard'], '/model_set smart ')
    assert [label for label, _ in options2] == models[6:12]
    # 翻页目录按归属校验。
    wrong = identity(user='other')
    await before_command(adapter, {'text': f'/model_page {page_token} 12'}, wrong)
    assert '目录已过期' in adapter.sent[-1][1]

    # 点击模型：写入 models + model_providers 并触发重载。
    option_token = options[0][1].split()[2]
    await before_command(adapter, {'text': f'/model_set smart {option_token}'}, identity())
    assert saved[-1]['llm']['models']['smart'] == models[0]
    assert saved[-1]['llm']['model_providers']['smart'] == 'openrouter'
    assert saved and adapter.reloads == ['qqbot:g']
    assert models[0] in adapter.sent[-1][1]


@pytest.mark.anyio
async def test_model_set_rejects_expired_or_mismatched_token(monkeypatch):
    config_stub(monkeypatch)
    monkeypatch.setattr('adapters.qqbot.commands.fetch_provider_models', lambda *args: ['m1'])
    adapter = Adapter()
    now = [100.0]
    store = CatalogStore(clock=lambda: now[0])
    adapter.model_catalogs = store
    token = store.put(CatalogEntry(user_id='u', chat_id='g', chat_type='group', alias='fast',
                                   provider='p', model='m1', expires_at=now[0] + 180))
    await before_command(adapter, {'text': f'/model_set smart {token}'}, identity())  # 别名不匹配
    assert '选项已过期' in adapter.sent[-1][1]
    now[0] += 181  # TTL 过期
    await before_command(adapter, {'text': f'/model_set fast {token}'}, identity())
    assert '选项已过期' in adapter.sent[-1][1]
    assert adapter.sent[-1][2].get('keyboard') is None


@pytest.mark.anyio
async def test_model_list_empty_falls_back_to_manual_input(monkeypatch):
    config_stub(monkeypatch)
    monkeypatch.setattr('adapters.qqbot.commands.fetch_provider_models', lambda *args: [])
    adapter = Adapter()
    await before_command(adapter, {'text': '/model_list smart gemini'}, identity())
    assert '手动输入' in adapter.sent[-1][1]
    assert find_command(adapter, adapter.sent[-1][2]['keyboard'], '/model_pick smart')


@pytest.mark.anyio
async def test_model_clear_only_for_optional_aliases(monkeypatch):
    state, saved = config_stub(monkeypatch, llm={'models': {'video': 'v1', 'smart': 's1'},
                                                 'model_providers': {'video': 'p'}})
    adapter = Adapter()
    await before_command(adapter, {'text': '/model_clear smart'}, identity())
    assert '不支持清除' in adapter.sent[-1][1] and not saved
    await before_command(adapter, {'text': '/model_clear video'}, identity())
    assert 'video' not in saved[-1]['llm']['models'] and 'video' not in saved[-1]['llm']['model_providers']
    assert saved


@pytest.mark.anyio
async def test_effort_menus_two_levels(monkeypatch):
    config_stub(monkeypatch)
    changes = []
    monkeypatch.setattr('config.set_llm_effort_by_alias', lambda *args: changes.append(args))
    adapter = Adapter()
    await before_command(adapter, {'text': '/effort'}, identity())
    assert '/effort_pick smart' in [cmd for _, cmd in button_commands(adapter, adapter.sent[-1][2]['keyboard'])]
    await before_command(adapter, {'text': '/effort_pick smart'}, identity())
    commands = [cmd for _, cmd in button_commands(adapter, adapter.sent[-1][2]['keyboard'])]
    for level in __import__('config').EFFORT_LEVELS:
        assert f'/effort smart {level}' in commands
    assert '/effort smart default' in commands
    # 按钮发出的档位命令走原有文本路径保存。
    await before_command(adapter, {'text': '/effort smart high'}, identity())
    assert changes == [('smart', 'high')]


@pytest.mark.anyio
async def test_model_and_hidden_commands_owner_only(monkeypatch):
    config_stub(monkeypatch)
    adapter = Adapter(owner=False)
    for command in ['/model', '/model_pick smart', '/model_list smart p', '/model_set smart t',
                    '/model_page t 0', '/model_clear video', '/effort_pick smart']:
        assert await before_command(adapter, {'text': command}, identity(False))
        assert '主人' in adapter.sent[-1][1]


@pytest.mark.anyio
async def test_long_model_names_truncated_in_button_labels(monkeypatch):
    config_stub(monkeypatch)
    long_name = 'x' * 60
    monkeypatch.setattr('adapters.qqbot.commands.fetch_provider_models', lambda *args: [long_name])
    adapter = Adapter()
    await before_command(adapter, {'text': '/model_list smart openrouter'}, identity())
    labels = [label for label, _ in button_commands(adapter, adapter.sent[-1][2]['keyboard'])]
    assert long_name in labels or any(len(label) <= 30 for label in labels)


def test_catalog_store_ttl_and_offset():
    now = [0.0]
    store = CatalogStore(clock=lambda: now[0])
    token = store.put(CatalogEntry(user_id='u', chat_id='g', chat_type='group', alias='smart',
                                   provider='p', models=('a', 'b', 'c'), expires_at=now[0] + 180))
    assert store.get(token, 'u', 'g', 'group').models == ('a', 'b', 'c')
    entry = store.set_offset(token, 2)
    assert entry.offset == 2 and store.get(token, 'u', 'g', 'group').offset == 2
    assert store.get(token, 'u', 'g', 'private') is None
    now[0] = 181
    assert store.get(token, 'u', 'g', 'group') is None


def test_interaction_menu_columns_layout():
    store = InteractionStore()
    choices = [(str(i), f'/cmd {i}') for i in range(5)]
    keyboard = store.menu('u', 'g', 'group', 'm', choices, columns=2)
    rows = keyboard['content']['rows']
    assert [len(row['buttons']) for row in rows] == [2, 2, 1]
    single = store.menu('u', 'g', 'group', 'm', choices)
    assert all(len(row['buttons']) == 1 for row in single['content']['rows'])
