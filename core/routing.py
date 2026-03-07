import re


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


def has_image_input(text: str) -> bool:
    """
    判断消息中是否包含图片输入痕迹。
    兼容统一媒体标识格式（如 [image: path]）及常见图片扩展名链接/路径。
    """
    if not text:
        return False

    s = text.lower()

    # 结构化标记（适配器常见格式）
    if "[image:" in s:
        return True

    # 常见图片 URL/路径扩展名
    image_ext_pattern = r"\.(?:png|jpe?g|webp|bmp|gif)(?:[?#][^\s\]]*)?"
    return re.search(image_ext_pattern, s, re.IGNORECASE) is not None


# --- 前脑路由信号检测 ---

# 前脑回复中触发后脑的标记
_BACKEND_SIGNAL = "[NEED_BACKEND]"


def parse_front_brain_response(response_text: str) -> dict:
    """
    解析前脑回复，提取路由信号和用户可见文本。

    前脑回复可能包含 [NEED_BACKEND] 标记，表示需要后脑接管执行工具/技能。
    该标记会从最终发送给用户的文本中移除。

    Returns:
        {
            "needs_backend": bool,      # 是否需要后脑
            "user_reply": str,          # 给用户看的清理后文本
        }
    """
    if not response_text:
        return {"needs_backend": False, "user_reply": ""}

    needs_backend = _BACKEND_SIGNAL in response_text

    # 清理标记，生成用户可见文本
    user_reply = response_text.replace(_BACKEND_SIGNAL, "").strip()
    # 清理多余空行
    user_reply = re.sub(r'\n{3,}', '\n\n', user_reply)

    return {
        "needs_backend": needs_backend,
        "user_reply": user_reply,
    }
