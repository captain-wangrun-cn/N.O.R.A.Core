import logging
import re

from adapters.message_controls import strip_message_control_markers

logger = logging.getLogger(__name__)


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
# 可选：将当前群聊切换为在线监听
_ONLINE_SIGNAL = "[ONLINE]"
# 可选：允许当前消息段暂不闭合，并暂停 followup detect，直到下次长时间沉默
_KEEP_SEGMENT_OPEN_SIGNAL = "[KEEP_SEGMENT_OPEN]"
# 可选：撤回历史某条 assistant 消息
_RETRACT_MESSAGE_PATTERN = re.compile(r"\[RETRACT_MESSAGE(?::([^\]]+))?\]", re.IGNORECASE)
_RETRACT_INT_PATTERN = re.compile(r"^-?\d+$")
# 可选：强制使用图片模型的标记
_USE_IMAGE_MODEL_PATTERN = re.compile(r"\[USE_IMAGE_MODEL\]|use_image_model\s*[:=]\s*(true|1|yes)", re.IGNORECASE)
# 可选：任务指示块
_TASK_INSTRUCTION_PATTERN = re.compile(r"\[TASK_INSTRUCTION\](.*?)\[/TASK_INSTRUCTION\]", re.IGNORECASE | re.DOTALL)
# 可选：生成形象图（旁路生图，独立于 NEED_BACKEND）。要求文本里不能带 `]`，
# front_brain.jinja 已明确禁止，这里非贪婪匹配到第一个 `]` 为止。
_DRAW_PATTERN = re.compile(r"\[DRAW\s*:\s*([^\]]*)\]", re.IGNORECASE)
# 可选：语音合成（旁路 TTS，独立于 NEED_BACKEND）。用块格式而不是 [VOICE:...] 短格式——
# 语音文本里可能带 provider 的方括号情绪标记（如 fish_audio 的 [happy] [break]），
# 短格式在第一个 `]` 截断会把它们切碎。短格式仅作兜底（模型习惯性输出时也能提取）。
_VOICE_BLOCK_PATTERN = re.compile(r"\[VOICE\](.*?)\[/VOICE\]", re.IGNORECASE | re.DOTALL)
# ⚠️ 短格式必须**大小写敏感**（只认大写 [VOICE:...]）：小写 [voice: path] 是平台媒体
# 发送协议（Telegram FILE_PATTERN / OneBot record 段），IGNORECASE 会把它当 TTS 标记
# 在 sanitize 链路里剥掉，语音文件就永远发不出去（2026-08-27 生产踩过）。
_VOICE_SHORT_PATTERN = re.compile(r"\[VOICE\s*:\s*([^\]]*)\]")
# 未闭合的裸 VOICE 标记残 token（sanitize 兜底用）。媒体标签必带冒号，不会被它误伤。
_VOICE_BARE_PATTERN = re.compile(r"\[/?VOICE\]", re.IGNORECASE)

# ── 路由重定向标记（多平台/多场景）──
# [platform:X]  指定目标平台（telegram / onebotv11 / qq...）
# [scene:id]           指定目标场景，类型由身份缓存/启发式推断
# [scene:group:id]     显式群聊场景
# [scene:private:id]   显式私聊场景
# 缺一个则取「活跃的」那个（活跃平台 / 活跃场景）。
_ROUTE_PLATFORM_PATTERN = re.compile(r"\[platform\s*:\s*([A-Za-z0-9_.\-]+)\s*\]", re.IGNORECASE)
_ROUTE_SCENE_PATTERN = re.compile(
    r"\[scene\s*:\s*(?:(private|group)\s*:\s*)?([^\]\s]+)\s*\]",
    re.IGNORECASE,
)

# 平台别名归一（与 main.resolve_adapter_names 保持一致，避免相互导入）。
_PLATFORM_ALIASES = {"onebot": "onebotv11", "onebot-11": "onebotv11", "qq": "onebotv11"}


def _canonical_platform(name: str) -> str:
    n = str(name or "").strip().lower()
    return _PLATFORM_ALIASES.get(n, n)


def parse_route_markers(text: str) -> tuple[dict, str]:
    """从文本中提取 [platform:X] / [scene:...] 路由标记并抹除。

    返回 (route, cleaned_text)：
      route = {"platform": str, "scene_id": str, "scene_type": str}
              未出现的字段为空串。
      cleaned_text = 移除路由标记后的文本（其余清理由调用方/sanitize 负责）。
    """
    route = {"platform": "", "scene_id": "", "scene_type": ""}
    if not text:
        return route, text or ""

    m = _ROUTE_PLATFORM_PATTERN.search(text)
    if m:
        route["platform"] = _canonical_platform(m.group(1))

    m = _ROUTE_SCENE_PATTERN.search(text)
    if m:
        route["scene_type"] = (m.group(1) or "").strip().lower()
        route["scene_id"] = (m.group(2) or "").strip()

    cleaned = _ROUTE_PLATFORM_PATTERN.sub("", text)
    cleaned = _ROUTE_SCENE_PATTERN.sub("", cleaned)
    return route, cleaned


def _empty_route() -> dict:
    return {"platform": "", "scene_id": "", "scene_type": ""}

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


def strip_timestamp_markers(text: str) -> str:
    """移除 add_message 注入的自动时间戳标记，避免泄漏到用户/模型可见文本。

    时间戳格式由 memory.message_history.timestamp_format 配置（默认带秒和星期），
    因此这里的模式必须宽松匹配到「秒 + 任意尾部字段」。全项目共用此实现，
    不要再各自复制窄版正则——历史上正是三份窄正则跟不上配置格式，导致
    时间戳既泄漏给模型，又让依赖字符串比较的去重逻辑长期失效。
    """
    if not text:
        return ""
    return _TIMESTAMP_PATTERN.sub("", text).strip()


# 向后兼容的内部别名
_strip_timestamp_markers = strip_timestamp_markers


def sanitize_adapter_output_text(text: str) -> str:
    """清理内部信息，同时保留应由目标 Adapter 解释的消息控制标记。"""
    if not text:
        return ""

    cleaned = _INTERNAL_NOTE_BLOCK_PATTERN.sub("", text)
    cleaned = _SYSTEM_NOTE_BLOCK_PATTERN.sub("", cleaned)
    cleaned = _strip_timestamp_markers(cleaned)
    cleaned = _SOURCE_LABEL_PATTERN.sub("", cleaned)
    cleaned = _ROUTE_PLATFORM_PATTERN.sub("", cleaned)
    cleaned = _ROUTE_SCENE_PATTERN.sub("", cleaned)
    cleaned = cleaned.replace(_SEMI_ONLINE_SIGNAL, "")
    cleaned = cleaned.replace(_ONLINE_SIGNAL, "")
    # 兜底剥离生图标记：它只在前脑主对话轮被消费，其它任何发送路径（后脑最终文本、
    # 主动消息、轮询审查）出现都只是泄漏，绝不能作为字面文本发给用户。
    cleaned = _DRAW_PATTERN.sub("", cleaned)
    # 同理剥离语音标记（块/短/裸三种形态，覆盖未闭合残 token）。
    cleaned = _VOICE_BLOCK_PATTERN.sub("", cleaned)
    cleaned = _VOICE_SHORT_PATTERN.sub("", cleaned)
    cleaned = _VOICE_BARE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def sanitize_user_visible_text(text: str) -> str:
    """清理不应暴露给用户的内部备注及回复/@控制标记。"""
    return strip_message_control_markers(sanitize_adapter_output_text(text)).strip()


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
            "force_online": False,
            "keep_segment_open": False,
            "retract_message_target": "",
            "draw_request": "",
            "voice_text": "",
            "route": _empty_route(),
        }

    # 提取路由标记（[platform:X] / [scene:...]）并抹除
    route, response_text = parse_route_markers(response_text)

    needs_backend = _BACKEND_SIGNAL in response_text
    need_follow = _FOLLOW_SIGNAL in response_text
    force_image_model = bool(_USE_IMAGE_MODEL_PATTERN.search(response_text))
    should_reply = _NO_REPLY_SIGNAL not in response_text
    force_semi_online = _SEMI_ONLINE_SIGNAL in response_text
    force_online = _ONLINE_SIGNAL in response_text
    if force_semi_online and force_online:
        logger.warning("前脑同时输出 ONLINE 与 SEMI_ONLINE，按 SEMI_ONLINE 处理")
        force_online = False
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

    # 提取生图要求（旁路生图；与 needs_backend 互不影响）。
    # 只取第一个：单轮只画一张，多余的标记会被清理掉但不触发第二次生图。
    draw_request = ""
    draw_match = _DRAW_PATTERN.search(response_text)
    if draw_match:
        draw_request = draw_match.group(1).strip()
        if not draw_request:
            logger.warning("前脑输出了空的 [DRAW:] 标记，已忽略")

    # 提取语音文本（旁路 TTS；与 needs_backend / draw_request 互不影响）。
    # 块格式优先；短格式兜底（情绪方括号标记在块内才安全）。只取第一个。
    voice_text = ""
    voice_block = _VOICE_BLOCK_PATTERN.search(response_text)
    if voice_block:
        voice_text = voice_block.group(1).strip()
        if not voice_text:
            logger.warning("前脑输出了空的 [VOICE] 块，已忽略")
    if not voice_text:
        voice_short = _VOICE_SHORT_PATTERN.search(response_text)
        if voice_short:
            voice_text = voice_short.group(1).strip()
            if voice_text:
                logger.info("前脑输出短格式 [VOICE:...] 标记，已提取（推荐用 [VOICE]...[/VOICE] 块格式）")
            else:
                logger.warning("前脑输出了空的 [VOICE:] 标记，已忽略")

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
        force_online = False

    # 清理内部标记，同时保留应由目标 Adapter 解释的 reply/@ 控制标记
    user_reply = response_text
    user_reply = user_reply.replace(_BACKEND_SIGNAL, "")
    user_reply = user_reply.replace(_FOLLOW_SIGNAL, "")
    user_reply = user_reply.replace(_NO_REPLY_SIGNAL, "")
    user_reply = user_reply.replace(_SEMI_ONLINE_SIGNAL, "")
    user_reply = user_reply.replace(_ONLINE_SIGNAL, "")
    user_reply = user_reply.replace(_KEEP_SEGMENT_OPEN_SIGNAL, "")
    user_reply = _RETRACT_MESSAGE_PATTERN.sub("", user_reply)
    user_reply = _USE_IMAGE_MODEL_PATTERN.sub("", user_reply)
    user_reply = _TASK_INSTRUCTION_PATTERN.sub("", user_reply)
    user_reply = _DRAW_PATTERN.sub("", user_reply)
    user_reply = _VOICE_BLOCK_PATTERN.sub("", user_reply)
    user_reply = _VOICE_SHORT_PATTERN.sub("", user_reply)
    user_reply = sanitize_adapter_output_text(user_reply)

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
        "force_online": force_online,
        "keep_segment_open": keep_segment_open,
        "retract_message_target": retract_target,
        "draw_request": draw_request,
        "voice_text": voice_text,
        "route": route,
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
            "force_online": False,
            "keep_segment_open": False,
            "retract_message_target": "",
            "route": _empty_route(),
        }
    # 提取路由标记（[platform:X] / [scene:...]）并抹除
    route, response_text = parse_route_markers(response_text)

    has_done = _TASK_DONE_SIGNAL in response_text
    has_backend = _BACKEND_SIGNAL in response_text
    need_follow = _FOLLOW_SIGNAL in response_text
    has_continue = bool(_CONTINUE_PATTERN.search(response_text)) or has_backend
    force_image_model = bool(_USE_IMAGE_MODEL_PATTERN.search(response_text))
    should_reply = _NO_REPLY_SIGNAL not in response_text
    force_semi_online = _SEMI_ONLINE_SIGNAL in response_text
    force_online = _ONLINE_SIGNAL in response_text
    if force_semi_online and force_online:
        logger.warning("前脑同时输出 ONLINE 与 SEMI_ONLINE，按 SEMI_ONLINE 处理")
        force_online = False
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
        force_online = False

    # 清理内部标记，同时保留应由目标 Adapter 解释的 reply/@ 控制标记
    user_reply = response_text
    user_reply = user_reply.replace(_TASK_DONE_SIGNAL, "")
    user_reply = user_reply.replace(_BACKEND_SIGNAL, "")
    user_reply = user_reply.replace(_FOLLOW_SIGNAL, "")
    user_reply = user_reply.replace(_NO_REPLY_SIGNAL, "")
    user_reply = user_reply.replace(_SEMI_ONLINE_SIGNAL, "")
    user_reply = user_reply.replace(_ONLINE_SIGNAL, "")
    user_reply = user_reply.replace(_KEEP_SEGMENT_OPEN_SIGNAL, "")
    user_reply = _RETRACT_MESSAGE_PATTERN.sub("", user_reply)
    user_reply = _USE_IMAGE_MODEL_PATTERN.sub("", user_reply)
    # 清理 continue 标记文本，避免泄漏给用户
    user_reply = _CONTINUE_PATTERN.sub("", user_reply)
    user_reply = _TASK_INSTRUCTION_PATTERN.sub("", user_reply)
    # 轮询审查轮**不触发**生图（生图只从主对话轮进入），但标记必须剥掉，
    # 否则模型在审查轮输出 [DRAW:...] 会作为字面文本泄漏给用户。
    if _DRAW_PATTERN.search(user_reply):
        logger.info("前脑审查轮出现 [DRAW:] 标记，已剥离（审查轮不触发生图）")
    user_reply = _DRAW_PATTERN.sub("", user_reply)
    # 语音标记同理：审查轮只剥离不合成。
    if _VOICE_BLOCK_PATTERN.search(user_reply) or _VOICE_SHORT_PATTERN.search(user_reply):
        logger.info("前脑审查轮出现 [VOICE] 标记，已剥离（审查轮不触发语音合成）")
    user_reply = _VOICE_BLOCK_PATTERN.sub("", user_reply)
    user_reply = _VOICE_SHORT_PATTERN.sub("", user_reply)
    user_reply = sanitize_adapter_output_text(user_reply)

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
            "force_online": force_online,
            "keep_segment_open": keep_segment_open,
            "retract_message_target": retract_target,
            "route": route,
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
            "force_online": force_online,
            "keep_segment_open": keep_segment_open,
            "retract_message_target": retract_target,
            "route": route,
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
            "force_online": force_online,
            "keep_segment_open": keep_segment_open,
            "retract_message_target": retract_target,
            "route": route,
        }
