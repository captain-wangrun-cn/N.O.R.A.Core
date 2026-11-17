"""预设注入的实际请求边界与命令路由回归。"""

import copy
import inspect
from types import SimpleNamespace

import pytest

import config
from core import preset_manager as pm
from core import message_handler as mh
from core.controller import NoraController
from core.preset_commands import run_preset_command
from core.preset_injection import apply_preset_to_request, prepend_preset_head


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "get_active_preset_name", lambda: "none")
    manager = pm.PresetManager(tmp_path)
    (manager.directory / "style.yml").write_text("name: 风格\nhead_prompt: 头部规则\n", encoding="utf-8")
    manager.reload_presets()
    monkeypatch.setattr(pm, "get_preset_manager", lambda: manager)
    return manager


def _preset(depth=1, role="system", scopes=("forebrain", "hindbrain")):
    return pm.PresetData("style", "风格", scopes=scopes, head_prompt="头部规则",
                         depth_injection=pm.DepthInjection(depth, role, "深度规则"),
                         parameters={"temperature": 0.0, "top_p": 0.9})


@pytest.mark.parametrize("depth, expected", [(0, None), (1, 6), (2, 2), (3, 0), (99, 0)])
def test_depth_counts_users_without_splitting_tool_pair(depth, expected):
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"},
               {"role": "user", "content": "recent"}, {"role": "assistant", "content": "answer2"},
               {"role": "tool_call", "tool_name": "read_file", "tool_args": {}},
               {"role": "tool_response", "tool_name": "read_file", "content": "result"}]
    before = copy.deepcopy(history)
    original = {"system_prompt": "人设", "user_prompt": "current", "history": history}
    result = apply_preset_to_request(original, _preset(depth))
    assert history == before
    assert "request_tail" not in original
    if depth == 0:
        assert result["history"] is history
        assert result["request_tail"] == [{"role": "system", "content": "深度规则"}]
    else:
        assert result["history"][expected]["content"] == "深度规则"
        call_index = next(i for i, item in enumerate(result["history"]) if item["role"] == "tool_call")
        assert result["history"][call_index + 1]["role"] == "tool_response"


def test_no_current_user_and_no_history_are_still_injected():
    result = apply_preset_to_request({"system_prompt": "人设", "history": [], "user_prompt": None}, _preset(1))
    assert result["history"] == [{"role": "system", "content": "深度规则"}]
    assert result["system_prompt"].startswith("头部规则")
    tail_result = apply_preset_to_request({"history": [], "user_prompt": None}, _preset(0))
    assert tail_result["request_tail"][0]["content"] == "深度规则"


def test_head_idempotency_and_default_template_bytes():
    from brain.prompts import env
    template = env.get_template("system.jinja")
    default = template.render(persona_prompt="人设", instructions=[], platform_specific_prompt="")
    assert default.startswith("人设\n")
    head = template.render(head_prompt="头部规则", persona_prompt="人设", instructions=[], platform_specific_prompt="")
    assert head.startswith("头部规则\n\n---\n人设")
    assert prepend_preset_head(head, _preset()) == head
    assert prepend_preset_head(default, None) == default


class _Host(NoraController):
    def __init__(self):
        self.non_stream_flags = {}
        self.default_non_stream = False

    def _build_system_environment_context(self, chat_id):
        return "【系统环境信息】\n固定环境"

    def append_current_time_to_user_prompt(self, text):
        return "【当前时间】\n" + text


class _Probe:
    def __init__(self):
        self.requests = []
        self.last_tool_calls = []
        self.last_usage = None

    async def chat_stream(self, **kwargs):
        self.requests.append(kwargs)
        yield {"type": "text", "content": "ok"}

    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        return "ok"


@pytest.mark.anyio
@pytest.mark.parametrize("non_stream", [True, False])
async def test_wrapper_opt_in_scopes_and_disable(manager, non_stream, monkeypatch):
    manager._presets["style"] = _preset(scopes=("forebrain",))
    manager.set_active_preset("style")
    monkeypatch.setattr("core.controller.get_lexicon_global_system_prompt_block", lambda: "")
    host, model = _Host(), _Probe()
    host.default_non_stream = non_stream
    history = [{"role": "user", "content": "历史"}]
    for scope in (None, "hindbrain", "forebrain"):
        stream = host._chat_stream_wrapper(model, "telegram:123", preset_scope=scope,
                                           system_prompt="人设", user_prompt="你好", history=history)
        assert [item async for item in stream]
    assert model.requests[0]["system_prompt"].startswith("人设")
    assert model.requests[1]["system_prompt"].startswith("人设")
    req = model.requests[2]
    assert req["system_prompt"].startswith("头部规则\n\n---\n人设")
    assert req["temperature"] == 0
    assert req["top_p"] == 0.9
    assert req["history"][-1]["content"] == "深度规则"
    assert history == [{"role": "user", "content": "历史"}]
    manager.clear_active_preset()
    assert [item async for item in host._chat_stream_wrapper(model, "telegram:123", preset_scope="forebrain",
                                                            system_prompt="人设", user_prompt="你好", history=history)]
    assert "temperature" not in model.requests[-1]
    assert model.requests[-1]["history"] is history


def test_shared_commands_and_cli_cross_process_reload(manager):
    assert "style" in run_preset_command(manager)
    assert "✅" in run_preset_command(manager, "use style.yaml")
    assert manager.active_name == "style"
    other = pm.PresetManager(manager.directory.parent)
    (manager.directory / "style.yml").write_text("head_prompt: 修改后\n", encoding="utf-8")
    assert "✅" in run_preset_command(other, "reload")
    assert manager.get_active_preset().head_prompt == "修改后"
    assert "ℹ️" in run_preset_command(other, "off")
    assert manager.get_active_preset() is None
    assert "❌" in run_preset_command(manager, "use missing")
    assert "用法" in run_preset_command(manager, "unknown")


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["telegram", "onebotv11", "qqbot"])
@pytest.mark.parametrize("owner", [True, False])
async def test_core_preset_command_uses_inbound_actor_and_does_not_interrupt(manager, monkeypatch, owner, platform):
    monkeypatch.setattr(mh, "owner_resolution_available", lambda: True)
    sent = []
    identity = SimpleNamespace(runtime_key=f"{platform}:123", platform_chat_id="123", platform=platform,
        storage_id="123", actor_user_id="456", chat_type="private", memory_scope_id="scope",
        place_scope_id="place", actor_display_name="访客", is_owner=owner)
    class Host(mh.MessageHandlerMixin):
        def _identity(self, context):
            return identity
        async def _send_platform_message(self, key, text):
            sent.append(text)
        def _adapter_for_key(self, key):
            class Adapter:
                async def before_command(self, context, identity):
                    return False
            return Adapter()
    # Host 不含 history/presence/任务字段；命令越过任何聊天处理即会报错。
    await Host().handle_new_message({"text": "/preset use style", "platform": platform})
    assert len(sent) == 1
    assert manager.active_name == ("style" if owner else "none")


@pytest.mark.anyio
async def test_cli_subcommand_uses_shared_manager(manager, monkeypatch, capsys):
    import cli
    import sys
    monkeypatch.setattr(cli, "ensure_config_exists", lambda: True)
    monkeypatch.setattr(sys, "argv", ["cli.py", "preset", "use", "style"])
    cli.main()
    assert manager.active_name == "style"
    assert "✅" in capsys.readouterr().out


def test_get_system_prompt_optional_head(manager, monkeypatch):
    from brain import prompts
    monkeypatch.setattr(prompts, "_ensure_workspace_identity_files", lambda: None)
    monkeypatch.setattr(prompts, "_read_file_safe", lambda *args, **kwargs: "人设")
    monkeypatch.setattr(prompts, "load_identity_context", lambda **kwargs: "")
    monkeypatch.setattr(prompts, "load_custom_prompt", lambda: "")
    monkeypatch.setattr(prompts, "load_adapter_prompt", lambda *args: "")
    monkeypatch.setattr(prompts, "get_always_lexicon_system_prompt_block", lambda: "")
    monkeypatch.setattr(prompts, "get_user_preferences_prompt_block", lambda: "")
    monkeypatch.setattr(config, "_config", {})
    manager.set_active_preset("style")
    assert prompts.get_system_prompt(preset_scope="hindbrain").startswith("头部规则\n\n---\n人设")
    assert prompts.get_system_prompt().startswith("人设")


@pytest.mark.anyio
async def test_database_history_is_not_polluted_by_wrapper(manager, monkeypatch, tmp_path):
    from memory.message_history import MessageHistory
    monkeypatch.setattr(config, "_config", {})
    monkeypatch.setattr("core.controller.get_lexicon_global_system_prompt_block", lambda: "")
    history_store = MessageHistory(db_path=str(tmp_path / "history.db"), mirror_db_path=str(tmp_path / "mirror.db"),
                                  context_db_path=str(tmp_path / "context.db"), compress_window=9999)
    history_store.add_message("telegram", "123", "user", "原始消息")
    before = history_store.get_context_messages("telegram", "123")
    request_history = [{"role": row["role"], "content": row["content"]} for row in before]
    manager._presets["style"] = _preset()
    manager.set_active_preset("style")
    assert [item async for item in _Host()._chat_stream_wrapper(_Probe(), "telegram:123", preset_scope="forebrain",
                        system_prompt="人设", user_prompt="当轮", history=request_history)]
    after = history_store.get_context_messages("telegram", "123")
    assert [row["content"] for row in after] == [row["content"] for row in before]
    assert all("深度规则" not in row["content"] and "头部规则" not in row["content"] for row in after)


def test_registration_and_explicit_scope_wiring():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    telegram = (root / "adapters/telegram/main.py").read_text(encoding="utf-8")
    assert "CommandHandler('preset', self._preset_command)" in telegram
    assert 'BotCommand("preset",' in telegram
    from adapters.qqbot.commands import _OWNER_COMMANDS
    assert "preset" in _OWNER_COMMANDS
    from core.back_brain import BackBrainMixin
    assert 'preset_scope=None if tool_media_analysis_round else "hindbrain"' in inspect.getsource(BackBrainMixin)
    from core.front_brain import FrontBrainMixin
    assert inspect.getsource(FrontBrainMixin).count('preset_scope="forebrain"') == 2
    from core.scheduler_mixin import SchedulerMixin
    for name in ("_detect_followup_intent", "_generate_daily_plan_via_llm", "_generate_group_listen_plan_via_llm"):
        assert "preset_scope" not in inspect.getsource(getattr(SchedulerMixin, name))
