r'''
Author: WR(captain-wangrun-cn)
Date: 2026-03-07 01:10:30
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-04-16 13:39:03
FilePath: \N.O.R.A.Core\tests\test_routing.py
'''
from core.routing import (
    parse_route_decision_response,
    has_image_input,
    parse_front_brain_response,
    sanitize_adapter_output_text,
    sanitize_user_visible_text,
)


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
    result = parse_front_brain_response(
        "好的，让我来帮你查一下~ [TASK_INSTRUCTION]查询新闻并整理摘要[/TASK_INSTRUCTION] [NEED_BACKEND]"
    )
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
    result = parse_front_brain_response(
        "马上处理 ✨ [TASK_INSTRUCTION]写一个待办脚本[/TASK_INSTRUCTION] [NEED_BACKEND]"
    )
    assert result["needs_backend"] is True
    assert result["user_reply"] == "马上处理 ✨"


def test_front_brain_backend_signal_mid_text():
    """信号在文本中间的情况也应正常处理"""
    result = parse_front_brain_response(
        "好的 [NEED_BACKEND] 让我来看看 [TASK_INSTRUCTION]查看历史媒体并回答用户问题[/TASK_INSTRUCTION]"
    )
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


def test_front_brain_online_signal_is_parsed_and_hidden():
    result = parse_front_brain_response("我继续听着。[ONLINE]")
    assert result["force_online"] is True
    assert result["force_semi_online"] is False
    assert result["user_reply"] == "我继续听着。"


def test_front_brain_presence_conflict_prefers_semi_online():
    result = parse_front_brain_response("先安静一下。[ONLINE][SEMI_ONLINE]")
    assert result["force_online"] is False
    assert result["force_semi_online"] is True
    assert "[ONLINE]" not in result["user_reply"]
    assert "[SEMI_ONLINE]" not in result["user_reply"]


def test_front_brain_review_online_signal_is_parsed_and_hidden():
    from core.routing import parse_front_brain_review

    result = parse_front_brain_review("处理完成啦 [TASK_DONE] [ONLINE]")
    assert result["action"] == "done"
    assert result["force_online"] is True
    assert result["user_reply"] == "处理完成啦"


def test_front_brain_keep_segment_open_signal_is_parsed():
    result = parse_front_brain_response("你想改哪个文件呀？ [KEEP_SEGMENT_OPEN]")
    assert result["keep_segment_open"] is True
    assert result["need_follow"] is False
    assert result["force_semi_online"] is False
    assert result["user_reply"] == "你想改哪个文件呀？"


def test_front_brain_retract_message_signal_is_parsed():
    result = parse_front_brain_response("我上一条说重了，我重说一下。 [RETRACT_MESSAGE]")
    assert result["retract_message_target"] == "last"
    assert result["user_reply"] == "我上一条说重了，我重说一下。"


def test_front_brain_retract_message_signal_supports_nth_from_end():
    result = parse_front_brain_response("我把前两条收一下。[RETRACT_MESSAGE:-2]")
    assert result["retract_message_target"] == "-2"
    assert result["user_reply"] == "我把前两条收一下。"


def test_front_brain_strips_internal_routing_notes_from_user_reply():
    result = parse_front_brain_response(
        "我去看一下。\n\n[内部备注·上一轮路由记录]\n已触发后脑接管处理。\n已下达后脑任务指示: 更新SOUL.md"
    )
    assert result["user_reply"] == "我去看一下。"


def test_sanitize_user_visible_text_removes_internal_and_system_notes():
    cleaned = sanitize_user_visible_text(
        "先这样。\n\n[系统备注] 这段仅供模型参考。\n\n[内部备注·上一轮路由记录]\n已触发后脑接管处理。"
    )
    assert cleaned == "先这样。"


def test_sanitize_user_visible_text_removes_source_labels():
    cleaned = sanitize_user_visible_text("[来自 onebotv11:749042488] 你好\n你好，大半夜的还没睡呢")
    assert cleaned == "你好\n你好，大半夜的还没睡呢"


def test_sanitize_user_visible_text_removes_source_labels_with_actor():
    cleaned = sanitize_user_visible_text("[来自 telegram:user_a / 张三] 远端消息")
    assert cleaned == "远端消息"


# --- 跨场景投递标记 [platform:X][scene:type:id] 测试 ---

def test_parse_route_markers_extracts_platform_and_typed_scene():
    from core.routing import parse_route_markers

    route, cleaned = parse_route_markers("[platform:telegram][scene:group:123456]去群里说：晚安")
    assert route["platform"] == "telegram"
    assert route["scene_id"] == "123456"
    assert route["scene_type"] == "group"
    assert "[platform:" not in cleaned
    assert "[scene:" not in cleaned
    assert cleaned.strip() == "去群里说：晚安"


def test_parse_route_markers_platform_alias_normalized():
    from core.routing import parse_route_markers

    route, _ = parse_route_markers("[platform:qq][scene:private:88888]hi")
    assert route["platform"] == "onebotv11"
    assert route["scene_type"] == "private"


def test_parse_route_markers_bare_scene_leaves_type_empty():
    from core.routing import parse_route_markers

    route, _ = parse_route_markers("[scene:99999]hi")
    assert route["scene_id"] == "99999"
    assert route["scene_type"] == ""


def test_front_brain_response_carries_route():
    result = parse_front_brain_response("[platform:onebotv11][scene:group:700]收到，我去群里说 [NEED_BACKEND]")
    assert result["route"]["platform"] == "onebotv11"
    assert result["route"]["scene_id"] == "700"
    assert result["route"]["scene_type"] == "group"
    assert "[platform:" not in result["user_reply"]
    assert "[scene:" not in result["user_reply"]


def test_front_brain_response_preserves_native_message_controls_for_adapter():
    result = parse_front_brain_response(
        "[reply:777][at:6112866979]你单独发一个，其他什么也不要说"
    )

    assert result["user_reply"] == (
        "[reply:777][at:6112866979]你单独发一个，其他什么也不要说"
    )


def test_front_brain_review_preserves_native_message_controls_for_adapter():
    from core.routing import parse_front_brain_review

    result = parse_front_brain_review(
        "[at:6112866979]处理完成啦 [TASK_DONE]"
    )

    assert result["action"] == "done"
    assert result["user_reply"] == "[at:6112866979]处理完成啦"


def test_front_brain_response_no_marker_empty_route():
    result = parse_front_brain_response("就在这儿说说话")
    assert result["route"] == {"platform": "", "scene_id": "", "scene_type": ""}


def test_sanitize_user_visible_text_strips_route_markers_as_safety_net():
    cleaned = sanitize_user_visible_text("[platform:telegram][scene:group:1]漏网的标记也要抹掉")
    assert "[platform:" not in cleaned
    assert "[scene:" not in cleaned
    assert cleaned == "漏网的标记也要抹掉"


def test_sanitize_user_visible_text_strips_presence_markers_as_safety_net():
    cleaned = sanitize_user_visible_text("还在听。[ONLINE][SEMI_ONLINE]")
    assert cleaned == "还在听。"


def test_adapter_output_sanitizer_preserves_native_message_controls():
    cleaned = sanitize_adapter_output_text(
        "[platform:telegram][scene:group:1][reply:22][at:33]正文"
    )

    assert cleaned == "[reply:22][at:33]正文"


def test_user_visible_sanitizer_strips_native_message_controls():
    cleaned = sanitize_user_visible_text("[reply:22][at:33]正文[reply:bad id][at]")

    assert cleaned == "正文"


def test_front_brain_draw_marker_is_parsed_and_hidden():
    result = parse_front_brain_response("等我拍一张！[DRAW:自拍,书桌前,微笑]")

    assert result["draw_request"] == "自拍,书桌前,微笑"
    assert result["user_reply"] == "等我拍一张！"
    assert "[DRAW:" not in result["user_reply"]
    assert result["needs_backend"] is False


def test_front_brain_draw_marker_absent():
    result = parse_front_brain_response("今天天气不错呀")

    assert result["draw_request"] == ""


def test_front_brain_review_draw_marker_is_stripped():
    """轮询审查轮不触发生图，但标记必须剥掉，否则会作为字面文本泄漏给用户。"""
    from core.routing import parse_front_brain_review

    result = parse_front_brain_review("弄好啦[DRAW:自拍] [TASK_DONE]")

    assert result["action"] == "done"
    assert result["user_reply"] == "弄好啦"
    assert "[DRAW:" not in result["user_reply"]


def test_front_brain_draw_coexists_with_backend():
    result = parse_front_brain_response(
        "好我看看[DRAW:自拍,微笑][TASK_INSTRUCTION]\n查一下今天天气\n[/TASK_INSTRUCTION] [NEED_BACKEND]"
    )

    assert result["draw_request"] == "自拍,微笑"
    assert result["needs_backend"] is True
    assert result["task_instruction"] == "查一下今天天气"
    assert result["user_reply"] == "好我看看"


def test_adapter_output_sanitizer_strips_draw_marker_as_safety_net():
    """生图标记只在前脑主对话轮被消费；任何其它发送路径出现都只是泄漏。"""
    assert sanitize_adapter_output_text("拍好了[DRAW:自拍]") == "拍好了"
    assert sanitize_user_visible_text("拍好了[DRAW:自拍]") == "拍好了"
