'''
Author: WR(captain-wangrun-cn)
Date: 2026-03-07 01:10:30
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-04-16 13:39:03
FilePath: \N.O.R.A.Core\tests\test_routing.py
'''
from core.routing import parse_route_decision_response, has_image_input, parse_front_brain_response


def test_parse_route_backend():
    assert parse_route_decision_response("ROUTE:backend") == "backend"


def test_parse_route_front():
    assert parse_route_decision_response("ROUTE:front") == "front"


def test_parse_route_with_extra_text():
    assert parse_route_decision_response("好的，ROUTE:backend") == "backend"


def test_parse_route_fallback_to_front():
    assert parse_route_decision_response("我觉得这是聊天") == "front"


def test_has_image_input_with_tag():
    assert has_image_input("[image: downloads/telegram/photo_xxx.jpg]\n帮我看看这张图") is True


def test_has_image_input_with_image_url():
    assert has_image_input("请分析这个链接 https://example.com/a.png") is True


def test_has_image_input_false_for_plain_text():
    assert has_image_input("今天我们聊聊天") is False


# --- 前脑路由信号解析测试 ---

def test_front_brain_needs_backend():
    result = parse_front_brain_response("好的，让我来帮你查一下~ [NEED_BACKEND]")
    assert result["needs_backend"] is True
    assert result["user_reply"] == "好的，让我来帮你查一下~"
    assert "[NEED_BACKEND]" not in result["user_reply"]


def test_front_brain_no_backend():
    result = parse_front_brain_response("今天天气不错呢，你有什么计划吗？")
    assert result["needs_backend"] is False
    assert result["user_reply"] == "今天天气不错呢，你有什么计划吗？"


def test_front_brain_empty_response():
    result = parse_front_brain_response("")
    assert result["needs_backend"] is False
    assert result["user_reply"] == ""


def test_front_brain_backend_signal_at_end():
    result = parse_front_brain_response("马上处理 ✨ [NEED_BACKEND]")
    assert result["needs_backend"] is True
    assert result["user_reply"] == "马上处理 ✨"


def test_front_brain_backend_signal_mid_text():
    """信号在文本中间的情况也应正常处理"""
    result = parse_front_brain_response("好的 [NEED_BACKEND] 让我来看看")
    assert result["needs_backend"] is True
    assert "好的" in result["user_reply"]
    assert "让我来看看" in result["user_reply"]
    assert "[NEED_BACKEND]" not in result["user_reply"]


def test_front_brain_no_reply_signal_silences_user_reply():
    result = parse_front_brain_response("收到，我去处理一下 [NO_REPLY]")
    assert result["should_reply"] is False
    assert result["user_reply"] == ""


def test_front_brain_backend_signal_without_task_instruction_does_not_escalate():
    result = parse_front_brain_response("嗯嗯 [NEED_BACKEND]")
    assert result["needs_backend"] is False
    assert result["user_reply"] == "嗯嗯"


def test_front_brain_backend_signal_with_task_instruction_still_escalates():
    result = parse_front_brain_response(
        "我记住了。[TASK_INSTRUCTION]把这次耳痛情况记录到今日记忆中[/TASK_INSTRUCTION] [NEED_BACKEND]"
    )
    assert result["needs_backend"] is True
    assert result["task_instruction"] == "把这次耳痛情况记录到今日记忆中"
    assert result["user_reply"] == "我记住了。"


def test_front_brain_busy_chat_reply_is_preserved_even_with_backend_task():
    result = parse_front_brain_response(
        "先抱抱你，我在呢。[TASK_INSTRUCTION]把这次左耳疼的情况记录下来[/TASK_INSTRUCTION] [NEED_BACKEND]"
    )
    assert result["needs_backend"] is True
    assert result["user_reply"] == "先抱抱你，我在呢。"
    assert result["should_reply"] is True


def test_front_brain_review_no_reply_signal_silences_user_reply():
    from core.routing import parse_front_brain_review

    result = parse_front_brain_review("处理完成啦 [TASK_DONE] [NO_REPLY]")
    assert result["action"] == "done"
    assert result["should_reply"] is False
    assert result["user_reply"] == ""
