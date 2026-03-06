def parse_route_decision_response(response_text: str) -> str:
    """
    解析路由模型输出。

    期望格式：ROUTE:front 或 ROUTE:backend
    返回值："front" | "backend"
    解析失败时默认 "front"（避免误触发后脑忙碌）
    """
    if not response_text:
        return "front"

    text = response_text.strip().lower()

    # 优先解析显式 route 标签
    if "route:" in text:
        route = text.split("route:", 1)[1].strip()
        if route.startswith("backend"):
            return "backend"
        if route.startswith("front"):
            return "front"

    # 兜底兼容
    if "backend" in text:
        return "backend"
    return "front"
