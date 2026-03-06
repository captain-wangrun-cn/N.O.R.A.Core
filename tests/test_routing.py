from core.routing import parse_route_decision_response


def test_parse_route_backend():
    assert parse_route_decision_response("ROUTE:backend") == "backend"


def test_parse_route_front():
    assert parse_route_decision_response("ROUTE:front") == "front"


def test_parse_route_with_extra_text():
    assert parse_route_decision_response("好的，ROUTE:backend") == "backend"


def test_parse_route_fallback_to_front():
    assert parse_route_decision_response("我觉得这是聊天") == "front"
