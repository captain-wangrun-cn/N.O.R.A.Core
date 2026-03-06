from core.routing import parse_route_decision_response, has_image_input


def test_parse_route_backend():
    assert parse_route_decision_response("ROUTE:backend") == "backend"


def test_parse_route_front():
    assert parse_route_decision_response("ROUTE:front") == "front"


def test_parse_route_with_extra_text():
    assert parse_route_decision_response("好的，ROUTE:backend") == "backend"


def test_parse_route_fallback_to_front():
    assert parse_route_decision_response("我觉得这是聊天") == "front"


def test_has_image_input_with_tag():
    assert has_image_input("[图片: downloads/telegram/photo_xxx.jpg]\n帮我看看这张图") is True


def test_has_image_input_with_image_url():
    assert has_image_input("请分析这个链接 https://example.com/a.png") is True


def test_has_image_input_false_for_plain_text():
    assert has_image_input("今天我们聊聊天") is False
