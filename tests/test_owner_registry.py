"""主人识别注册表测试。使用临时文件，不触碰生产 owner_bindings.json。"""

import json
import os

from core.owner_registry import OwnerRegistry


def _new_registry(tmp_path):
    state = os.path.join(str(tmp_path), "owner_bindings.json")
    return OwnerRegistry(state_path=state), state


def test_first_private_chatter_auto_bound_as_owner(tmp_path):
    reg, _ = _new_registry(tmp_path)
    # 首个私聊者 → 自动绑定为主人
    assert reg.resolve_is_owner("telegram", "111", "private") is True
    assert reg.is_bound("telegram", "111") is True


def test_second_private_chatter_not_owner(tmp_path):
    reg, _ = _new_registry(tmp_path)
    assert reg.resolve_is_owner("telegram", "111", "private") is True
    # 同平台已有主人，第二个私聊者不是主人
    assert reg.resolve_is_owner("telegram", "222", "private") is False
    assert reg.is_bound("telegram", "222") is False


def test_group_unbound_is_visitor(tmp_path):
    reg, _ = _new_registry(tmp_path)
    # 群聊里未绑定的人是访客，且不会触发自动绑定
    assert reg.resolve_is_owner("telegram", "999", "group") is False
    assert reg.is_bound("telegram", "999") is False


def test_bound_owner_recognized_in_group(tmp_path):
    reg, _ = _new_registry(tmp_path)
    # 先在私聊绑定主人
    assert reg.resolve_is_owner("telegram", "111", "private") is True
    # 同一身份在群聊里依然是主人
    assert reg.resolve_is_owner("telegram", "111", "group") is True


def test_per_platform_independent_binding(tmp_path):
    reg, _ = _new_registry(tmp_path)
    assert reg.resolve_is_owner("telegram", "tg-1", "private") is True
    # 另一个平台首个私聊者也会各自绑定
    assert reg.resolve_is_owner("web", "web-1", "private") is True
    assert reg.resolve_is_owner("telegram", "tg-1", "private") is True


def test_seed_from_config_predeclares_owner(tmp_path):
    reg, _ = _new_registry(tmp_path)
    added = reg.seed_from_config(
        [{"platform": "telegram", "user_id": "555", "display_name": "主人"}]
    )
    assert added == 1
    # 预声明后，群聊里立即识别为主人
    assert reg.resolve_is_owner("telegram", "555", "group") is True
    # 既然该平台已有绑定，别的私聊者就不是主人
    assert reg.resolve_is_owner("telegram", "777", "private") is False


def test_seed_from_config_idempotent(tmp_path):
    reg, _ = _new_registry(tmp_path)
    ids = [{"platform": "telegram", "user_id": "555"}]
    assert reg.seed_from_config(ids) == 1
    assert reg.seed_from_config(ids) == 0  # 重复注入不新增


def test_bindings_persist_across_instances(tmp_path):
    reg, state = _new_registry(tmp_path)
    reg.resolve_is_owner("telegram", "111", "private")

    # 新实例从磁盘重新加载
    reloaded = OwnerRegistry(state_path=state)
    assert reloaded.is_bound("telegram", "111") is True

    # 文件结构正确
    with open(state, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["owner_id"] == "owner:default"
    assert any(b["user_id"] == "111" for b in data["bindings"])


def test_empty_inputs_never_bind(tmp_path):
    reg, _ = _new_registry(tmp_path)
    assert reg.resolve_is_owner("", "111", "private") is False
    assert reg.resolve_is_owner("telegram", "", "private") is False
    assert reg.list_bindings() == []
