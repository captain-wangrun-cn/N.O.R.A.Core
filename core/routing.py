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
# 可选：不向用户回复的标记
_NO_REPLY_SIGNAL = "[NO_REPLY]"
# 可选：任务指示块
_TASK_INSTRUCTION_PATTERN = re.compile(r"\[TASK_INSTRUCTION\](.*?)\[/TASK_INSTRUCTION\]", re.IGNORECASE | re.DOTALL)

# 前脑审查中任务完成的标记
_TASK_DONE_SIGNAL = "[TASK_DONE]"

_TIMESTAMP_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*")


def _strip_timestamp_markers(text: str) -> str:
    """移除消息中的自动时间戳标记，避免泄漏到用户侧。"""
    if not text:
        return ""
    return _TIMESTAMP_PATTERN.sub("", text).strip()


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
        return {
            "needs_backend": False,
            "user_reply": "",
            "task_instruction": "",
            "should_reply": False,
        }

    needs_backend = _BACKEND_SIGNAL in response_text
    should_reply = _NO_REPLY_SIGNAL not in response_text

    # 提取任务指示（可为空）
    task_instruction = ""
    task_match = _TASK_INSTRUCTION_PATTERN.search(response_text)
    if task_match:
        task_instruction = task_match.group(1).strip()

    # 清理标记，生成用户可见文本
    user_reply = response_text
    user_reply = user_reply.replace(_BACKEND_SIGNAL, "")
    user_reply = user_reply.replace(_NO_REPLY_SIGNAL, "")
    user_reply = _TASK_INSTRUCTION_PATTERN.sub("", user_reply)
    user_reply = user_reply.strip()
    user_reply = _strip_timestamp_markers(user_reply)
    # 清理多余空行
    user_reply = re.sub(r'\n{3,}', '\n\n', user_reply)

    if not should_reply:
        user_reply = ""

    return {
        "needs_backend": needs_backend,
        "user_reply": user_reply,
        "task_instruction": task_instruction,
        "should_reply": should_reply,
    }


def parse_front_brain_review(response_text: str) -> dict:
    """
    解析前脑轮询审查的回复，提取路由信号和用户可见文本。

    前脑审查回复可能包含:
    - [TASK_DONE] 标记: 任务完成，结束轮询
    - [NEED_BACKEND] 标记: 需要后脑继续工作
    - 无标记: 纯聊天回应，结束轮询

    Returns:
        {
            "action": "done" | "continue" | "chat",
            "user_reply": str,  # 给用户看的清理后文本
        }
    """
    if not response_text:
        return {
            "action": "done",
            "user_reply": "",
            "task_instruction": "",
            "should_reply": False,
        }

    has_done = _TASK_DONE_SIGNAL in response_text
    has_backend = _BACKEND_SIGNAL in response_text
    should_reply = _NO_REPLY_SIGNAL not in response_text

    task_instruction = ""
    task_match = _TASK_INSTRUCTION_PATTERN.search(response_text)
    if task_match:
        task_instruction = task_match.group(1).strip()

    # 清理标记，生成用户可见文本
    user_reply = response_text
    user_reply = user_reply.replace(_TASK_DONE_SIGNAL, "")
    user_reply = user_reply.replace(_BACKEND_SIGNAL, "")
    user_reply = user_reply.replace(_NO_REPLY_SIGNAL, "")
    user_reply = _TASK_INSTRUCTION_PATTERN.sub("", user_reply)
    user_reply = user_reply.strip()
    user_reply = _strip_timestamp_markers(user_reply)
    user_reply = re.sub(r'\n{3,}', '\n\n', user_reply)

    if not should_reply:
        user_reply = ""

    if has_backend:
        return {
            "action": "continue",
            "user_reply": user_reply,
            "task_instruction": task_instruction,
            "should_reply": should_reply,
        }
    elif has_done:
        return {
            "action": "done",
            "user_reply": user_reply,
            "task_instruction": task_instruction,
            "should_reply": should_reply,
        }
    else:
        # 无标记 = 纯聊天，也结束轮询
        return {
            "action": "chat",
            "user_reply": user_reply,
            "task_instruction": task_instruction,
            "should_reply": should_reply,
        }
