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
# 前脑回复中触发主动追问/追话的标记
_FOLLOW_SIGNAL = "[NEED_FOLLOW]"
# 可选：不向用户回复的标记
_NO_REPLY_SIGNAL = "[NO_REPLY]"
# 可选：立即进入半在线状态
_SEMI_ONLINE_SIGNAL = "[SEMI_ONLINE]"
# 可选：允许当前消息段暂不闭合，并暂停 followup detect，直到下次长时间沉默
_KEEP_SEGMENT_OPEN_SIGNAL = "[KEEP_SEGMENT_OPEN]"
# 可选：撤回历史某条 assistant 消息
_RETRACT_MESSAGE_PATTERN = re.compile(r"\[RETRACT_MESSAGE(?::([^\]]+))?\]", re.IGNORECASE)
_RETRACT_INT_PATTERN = re.compile(r"^-?\d+$")
# 可选：强制使用图片模型的标记
_USE_IMAGE_MODEL_PATTERN = re.compile(r"\[USE_IMAGE_MODEL\]|use_image_model\s*[:=]\s*(true|1|yes)", re.IGNORECASE)
# 可选：任务指示块
_TASK_INSTRUCTION_PATTERN = re.compile(r"\[TASK_INSTRUCTION\](.*?)\[/TASK_INSTRUCTION\]", re.IGNORECASE | re.DOTALL)

# 前脑审查中任务完成的标记
_TASK_DONE_SIGNAL = "[TASK_DONE]"

# 支持审查输出使用 action: continue / [CONTINUE]，优先于老的 [NEED_BACKEND]
_CONTINUE_PATTERN = re.compile(r"(?:\baction\s*:\s*continue\b|\[CONTINUE\])", re.IGNORECASE)

_TIMESTAMP_PATTERN = re.compile(
    r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?(?: [^\]]+)?\]\s*",
    re.IGNORECASE,
)
_INTERNAL_NOTE_BLOCK_PATTERN = re.compile(
    r"\[内部备注[^\]]*\][\s\S]*?(?=(?:\n\s*\n)|$)",
    re.IGNORECASE,
)
_SYSTEM_NOTE_BLOCK_PATTERN = re.compile(
    r"\[系统备注\][\s\S]*?(?=(?:\n\s*\n)|$)",
    re.IGNORECASE,
)
_SOURCE_LABEL_PATTERN = re.compile(
    r"\[来自\s+[A-Za-z0-9_.-]+:[^\]\s/]+(?:\s*/\s*[^\]]+)?\]\s*",
    re.IGNORECASE,
)


def _strip_timestamp_markers(text: str) -> str:
    """移除消息中的自动时间戳标记，避免泄漏到用户侧。"""
    if not text:
        return ""
    return _TIMESTAMP_PATTERN.sub("", text).strip()


def sanitize_user_visible_text(text: str) -> str:
    """清理不应暴露给用户的内部备注/控制标记。"""
    if not text:
        return ""

    cleaned = _INTERNAL_NOTE_BLOCK_PATTERN.sub("", text)
    cleaned = _SYSTEM_NOTE_BLOCK_PATTERN.sub("", cleaned)
    cleaned = _strip_timestamp_markers(cleaned)
    cleaned = _SOURCE_LABEL_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


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
            "use_image_model": False,
            "need_follow": False,
            "force_semi_online": False,
            "keep_segment_open": False,
            "retract_message_target": "",
        }

    needs_backend = _BACKEND_SIGNAL in response_text
    need_follow = _FOLLOW_SIGNAL in response_text
    force_image_model = bool(_USE_IMAGE_MODEL_PATTERN.search(response_text))
    should_reply = _NO_REPLY_SIGNAL not in response_text
    force_semi_online = _SEMI_ONLINE_SIGNAL in response_text
    keep_segment_open = _KEEP_SEGMENT_OPEN_SIGNAL in response_text
    retract_target = ""
    retract_match = _RETRACT_MESSAGE_PATTERN.search(response_text)
    if retract_match:
        raw_target = (retract_match.group(1) or "last").strip() or "last"
        if _RETRACT_INT_PATTERN.match(raw_target):
            retract_target = raw_target
        else:
            retract_target = raw_target

    # 提取任务指示（可为空）
    task_instruction = ""
    task_match = _TASK_INSTRUCTION_PATTERN.search(response_text)
    if task_match:
        task_instruction = task_match.group(1).strip()

    # 仅有后脑标记、却没有明确任务指示时，默认视为普通聊天，避免把
    # “嗯嗯/好的/收到/晚安” 这类轻量回复误升级为后脑任务。
    if needs_backend and not task_instruction:
        needs_backend = False

    # SEMI_ONLINE 与 NEED_FOLLOW 语义互斥：进入半在线就不应再触发对话延续。
    # prompt 已声明互斥，但模型偶尔会同时输出，这里做兜底。
    if force_semi_online and need_follow:
        need_follow = False

    # 不闭段意味着本轮先不要进入 followup detect。
    if keep_segment_open:
        need_follow = False
        force_semi_online = False

    # 清理标记，生成用户可见文本
    user_reply = response_text
    user_reply = user_reply.replace(_BACKEND_SIGNAL, "")
    user_reply = user_reply.replace(_FOLLOW_SIGNAL, "")
    user_reply = user_reply.replace(_NO_REPLY_SIGNAL, "")
    user_reply = user_reply.replace(_SEMI_ONLINE_SIGNAL, "")
    user_reply = user_reply.replace(_KEEP_SEGMENT_OPEN_SIGNAL, "")
    user_reply = _RETRACT_MESSAGE_PATTERN.sub("", user_reply)
    user_reply = _USE_IMAGE_MODEL_PATTERN.sub("", user_reply)
    user_reply = _TASK_INSTRUCTION_PATTERN.sub("", user_reply)
    user_reply = sanitize_user_visible_text(user_reply)

    if not should_reply:
        user_reply = ""

    return {
        "needs_backend": needs_backend,
        "user_reply": user_reply,
        "task_instruction": task_instruction,
        "should_reply": should_reply,
        "use_image_model": force_image_model,
        "need_follow": need_follow,
        "force_semi_online": force_semi_online,
        "keep_segment_open": keep_segment_open,
        "retract_message_target": retract_target,
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
            "use_image_model": False,
            "need_follow": False,
            "force_semi_online": False,
            "keep_segment_open": False,
            "retract_message_target": "",
        }

    has_done = _TASK_DONE_SIGNAL in response_text
    has_backend = _BACKEND_SIGNAL in response_text
    need_follow = _FOLLOW_SIGNAL in response_text
    has_continue = bool(_CONTINUE_PATTERN.search(response_text)) or has_backend
    force_image_model = bool(_USE_IMAGE_MODEL_PATTERN.search(response_text))
    should_reply = _NO_REPLY_SIGNAL not in response_text
    force_semi_online = _SEMI_ONLINE_SIGNAL in response_text
    keep_segment_open = _KEEP_SEGMENT_OPEN_SIGNAL in response_text
    retract_target = ""
    retract_match = _RETRACT_MESSAGE_PATTERN.search(response_text)
    if retract_match:
        raw_target = (retract_match.group(1) or "last").strip() or "last"
        if _RETRACT_INT_PATTERN.match(raw_target):
            retract_target = raw_target
        else:
            retract_target = raw_target
    task_instruction = ""
    task_match = _TASK_INSTRUCTION_PATTERN.search(response_text)
    if task_match:
        task_instruction = task_match.group(1).strip()

    # 互斥兜底：进入半在线就不应再触发 follow-up，也不应继续后脑追跑。
    if force_semi_online:
        if need_follow:
            need_follow = False
        if has_continue:
            has_continue = False
    if keep_segment_open:
        need_follow = False
        force_semi_online = False

    # 清理标记，生成用户可见文本
    user_reply = response_text
    user_reply = user_reply.replace(_TASK_DONE_SIGNAL, "")
    user_reply = user_reply.replace(_BACKEND_SIGNAL, "")
    user_reply = user_reply.replace(_FOLLOW_SIGNAL, "")
    user_reply = user_reply.replace(_NO_REPLY_SIGNAL, "")
    user_reply = user_reply.replace(_SEMI_ONLINE_SIGNAL, "")
    user_reply = user_reply.replace(_KEEP_SEGMENT_OPEN_SIGNAL, "")
    user_reply = _RETRACT_MESSAGE_PATTERN.sub("", user_reply)
    user_reply = _USE_IMAGE_MODEL_PATTERN.sub("", user_reply)
    # 清理 continue 标记文本，避免泄漏给用户
    user_reply = _CONTINUE_PATTERN.sub("", user_reply)
    user_reply = _TASK_INSTRUCTION_PATTERN.sub("", user_reply)
    user_reply = sanitize_user_visible_text(user_reply)

    if not should_reply:
        user_reply = ""

    if has_continue:
        return {
            "action": "continue",
            "user_reply": user_reply,
            "task_instruction": task_instruction,
            "should_reply": should_reply,
            "use_image_model": force_image_model,
            "need_follow": need_follow,
            "force_semi_online": force_semi_online,
            "keep_segment_open": keep_segment_open,
            "retract_message_target": retract_target,
        }
    elif has_done:
        return {
            "action": "done",
            "user_reply": user_reply,
            "task_instruction": task_instruction,
            "should_reply": should_reply,
            "use_image_model": force_image_model,
            "need_follow": need_follow,
            "force_semi_online": force_semi_online,
            "keep_segment_open": keep_segment_open,
            "retract_message_target": retract_target,
        }
    else:
        # 无标记 = 纯聊天，也结束轮询
        return {
            "action": "chat",
            "user_reply": user_reply,
            "task_instruction": task_instruction,
            "should_reply": should_reply,
            "use_image_model": force_image_model,
            "need_follow": need_follow,
            "force_semi_online": force_semi_online,
            "keep_segment_open": keep_segment_open,
            "retract_message_target": retract_target,
        }
