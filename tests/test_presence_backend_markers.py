"""`[NEED_BACKEND]` + `[SEMI_ONLINE]` 同时出现时，后脑不能被半在线提前吞掉。

背景（2026-10-19 生产）：主人说「以后不用专门记录下来了」，前脑按
`front_brain.jinja` 的明确指引把记录任务交给后脑，并在末尾加了 `[SEMI_ONLINE]`
（「把活交完就该让位给后脑」）。主对话轮先应用在线标记、`presence_ended` 直接
return，后脑整轮任务被静默丢弃——debug 里能看到任务指示，日志里连
「前脑判定需要后脑」都没有。半在线必须等后脑（含审查轮）收尾后再落。
"""

import inspect

from core.message_handler import MessageHandlerMixin, should_defer_semi_online_until_backend
from core.polling import PollingMixin
from core.routing import parse_front_brain_response


def test_routing_keeps_needs_backend_and_semi_online_together():
    result = parse_front_brain_response(
        "好，我记下了。"
        "[TASK_INSTRUCTION]更新 USER.md 和 SCHEDULE.md[/TASK_INSTRUCTION]"
        "[NEED_BACKEND][SEMI_ONLINE]"
    )

    assert result["needs_backend"] is True
    assert result["force_semi_online"] is True
    assert result["task_instruction"].strip().startswith("更新 USER.md")


def test_defer_helper_only_when_backend_is_involved():
    assert should_defer_semi_online_until_backend(
        {"needs_backend": True, "force_semi_online": True}
    )
    # 只有后脑、没有半在线：本来就是正常后脑路径。
    assert not should_defer_semi_online_until_backend(
        {"needs_backend": True, "force_semi_online": False}
    )
    # 只有半在线：纯聊天收尾，主对话轮直接切半在线是对的。
    assert not should_defer_semi_online_until_backend(
        {"needs_backend": False, "force_semi_online": True}
    )
    assert not should_defer_semi_online_until_backend({})


def test_presence_early_return_sits_behind_backend_start():
    """源码级接线断言：不能先 return 再启动后脑。

    撤销修复即可变红：把 `_apply_presence_markers` 挪回无条件调用、
    `if presence_ended: return` 放回 `needs_backend` 分支之前，
    下面的位置关系与标记名都会失配。
    """
    src = inspect.getsource(MessageHandlerMixin.handle_new_message)

    defer_pos = src.index(
        "defer_semi_online = should_defer_semi_online_until_backend(front_result)"
    )
    backend_pos = src.index('if front_result["needs_backend"]:')
    guard_pos = src.index("if defer_semi_online:")
    return_pos = src.index("if presence_ended:", defer_pos)

    assert defer_pos < backend_pos, "延迟半在线的判定必须发生在后脑启动之前"
    assert guard_pos < return_pos < backend_pos, (
        "presence_ended 的提前 return 必须在非延迟分支里，且不能挡在后脑启动之前"
    )
    assert (
        'backend_context["_force_semi_online_after_backend"] = True' in src
    ), "延迟的半在线标记必须交给后脑上下文，否则永远不会生效"


def test_polling_loop_applies_deferred_semi_online_at_teardown():
    """源码级接线断言：轮询循环收尾时消费延迟标记，切换到半在线。"""
    src = inspect.getsource(PollingMixin._run_polling_loop)

    assert '_force_semi_online_after_backend' in src
    assert "force_semi_online=True" in src
    # 被打断（用户又发了新消息）时不能落半在线，否则会把新一轮的会话段关掉。
    assert "interrupted_early" in src
    # 必须落在 finally 的收尾段：后脑 + 审查都跑完才落半在线。
    assert src.index("finally:") < src.index("_force_semi_online_after_backend")
