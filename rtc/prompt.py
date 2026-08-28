"""RTC 通话提示词组装（P1）。

设计文档 docs/architecture/rtc.md §8（身份/人设缝合）：
- Live 的 systemInstruction 是单块文本；`[SPLIT]`/`[reply:]` 等文本标记协议
  **不注入**，另写 rtc 专用指令块（口语化、短句、无 markdown）
- 身份注入**精简版**：只带 SOUL + USER（"必要文件"）。APPEARANCE 是给生图的、
  MEMORY 很长、SCHEDULE 通话用不上——Live 每轮重算全上下文，注入越多每轮越贵
- **当前消息段**：`MessageHistory.get_current_segment_messages()`——
  conversation_sessions 里 session_id IS NULL 的活跃段，即"你们电话前正在聊的
  这轮对话"。注入它 Nora 才能接着电话前的话头说。
- `rtc/rules.md`（可选）：owner 的私有通话规则追加块（至高法则）
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parent / "rules.md"

# RTC 通话专用指令块：口语化、短句、无 markdown、无文本标记协议
RTC_STYLE_INSTRUCTION = """【通话模式须知】
你正在和你的主人打语音电话。这不是打字聊天，规则完全不同：
- 用简短口语化中文回答，每次两三句话以内，像打电话一样自然
- 不要用任何格式（列表、标题、markdown、emoji 都不要）
- 你看到的人设文件里可能提到 [SPLIT]、分段发送之类的打字聊天习惯——那些
  只用于文字聊天，打电话时彻底忘掉，说话就是说话，不要分段不要停顿标记
- 不要提"消息""发""打字"这类书面交流词汇，这是实时对话
- 对方随时可能插话打断你，被打断就停下听
- 听不清就直说"没听清，再说一遍"，不要猜
- 挂断前的告别要短，说"拜拜"就行，不要总结这次通话"""

# 当前消息段注入上限（条数）——只取最近这段对话的尾部，控制每轮成本
MAX_SEGMENT_MESSAGES = 30

# 入库消息里可能带文本侧的消息控制标记，通话场景不该让模型学到这些写法
# （实测：注入含 [SPLIT:0.8] 的历史后，语音转写里模型真的说了 "[SPLIT:0.8]"）
import re as _re

_MARKER_PATTERNS = [
    _re.compile(r"\[SPLIT(?::[\d.]+)?\]"),
    _re.compile(r"\[reply:\d+\]"),
    _re.compile(r"\[at:[^\]]*\]"),
    _re.compile(r"\[poke:[^\]]*\]"),
    _re.compile(r"\[NO_REPLY\]"),
    _re.compile(r"\[NEED_FOLLOW\]"),
    _re.compile(r"\[VOICE\].*?\[/VOICE\]", _re.DOTALL),
    _re.compile(r"\[DRAW:[^\]]*\]"),
    # 时间戳前缀（入库加工）："[2026-08-27 23:36:20 Thursday] "
    _re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Za-z]+\]\s*"),
]


def _strip_text_markers(text: str) -> str:
    """剥掉最近对话里的文本侧控制标记与时间戳前缀（通话 prompt 不该带）。"""
    for pat in _MARKER_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def load_rules() -> str:
    """读 rtc/rules.md（owner 私有通话规则，可选）。不存在返回空串。"""
    if not RULES_PATH.exists():
        return ""
    try:
        text = RULES_PATH.read_text(encoding="utf-8").strip()
        return text
    except OSError as e:
        logger.warning("rtc/rules.md 读取失败: %s", e)
        return ""


def _load_file_block(name: str, tag: str) -> str:
    """从 workspace 读身份文件，包成 <tag> 块。文件缺失返回空串。

    身份文件（尤其 SOUL.md）里可能写着 [SPLIT]/[VOICE] 等文本协议用法说明，
    通话模型不需要也不该学——统一剥掉标记本体（说明文字留着无害）。
    """
    try:
        from workspace_config import get_workspace_manager

        root = Path(get_workspace_manager().workspace_root)
        path = root / name
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        return f"<{tag}>\n{_strip_text_markers(text)}\n</{tag}>"
    except Exception as e:  # noqa: BLE001 - 缺文件不该阻断通话
        logger.warning("RTC 身份文件 %s 读取失败: %s", name, e)
        return ""


def load_essential_identity() -> str:
    """精简身份：SOUL（人格）+ USER（主人档案）。刻意不带 APPEARANCE/MEMORY/SCHEDULE。"""
    blocks = [
        _load_file_block("SOUL.md", "soul"),
        _load_file_block("USER.md", "user_profile"),
    ]
    if not any(blocks):
        return ""
    head = "【你是谁（通话前速览）】"
    return "\n".join([head] + [b for b in blocks if b])


def load_recent_conversation(platform: str = "telegram", chat_id: str = "") -> str:
    """最近对话注入：当前活跃段 + 上一个已封闭段（两段都要）。

    Live 的 systemInstruction 是静态单块（通话中不能追加），所以最近对话以
    "通话前的上文"形式注入。找不到 MessageHistory 或两段全空时返回空串。

    段的语义（见 docs/onboarding/QUICK_REFERENCE.md §消息历史）：
    - 当前段（session_id IS NULL）：你们正在聊的这轮
    - 上一个封闭段：上一轮聊完（AI 进入半在线）封闭的对话
    """
    if not chat_id:
        return ""
    try:
        from memory.message_history import MessageHistory

        history = MessageHistory()

        blocks: list[str] = []
        # ---- 上一个封闭段（先注入旧的）
        prev_rows, prev_note = _closed_segment(history, platform, chat_id, offset=0)
        prev_text = _rows_to_dialog(prev_rows)
        if prev_text:
            blocks.append(
                "<更早的一段对话" + prev_note + ">\n" + prev_text +
                "\n</更早的一段对话>"
            )

        # ---- 当前活跃段
        cur_rows = history.get_current_segment_messages(platform, chat_id)
        cur_text = _rows_to_dialog(cur_rows)
        if cur_text:
            blocks.append("<通话前的最近对话>\n" + cur_text + "\n</通话前的最近对话>")

        if not blocks:
            return ""
        return "\n\n".join(blocks)
    except Exception as e:  # noqa: BLE001 - 对话注入失败不阻断通话
        logger.warning("RTC 当前消息段读取失败: %s", e)
        return ""


def _rows_to_dialog(rows) -> str:
    """消息行 → "主人: .../你: ..." 对话文本（尾部 MAX_SEGMENT_MESSAGES 条，
    剥文本侧控制标记与时间戳）。"""
    lines = []
    for row in rows[-MAX_SEGMENT_MESSAGES:]:
        role = "主人" if row.get("role") == "user" else "你"
        content = _strip_text_markers(str(row.get("content") or ""))
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _closed_segment(history, platform: str, chat_id: str, offset: int = 0):
    """第 offset+1 个最近封闭段的原始消息行。返回 (rows, 说明后缀)。

    封闭段有 summary（入库时异步生成），但注入原始消息更能保住语气细节；
    段太长时由 _rows_to_dialog 的尾部截断控制。
    """
    try:
        sessions = history.get_recent_sessions(platform, chat_id,
                                                limit=offset + 1)
        if len(sessions) <= offset:
            return [], ""
        session = sessions[offset]
        session_id = session.get("id") or session.get("session_id")
        if not session_id:
            return [], ""
        rows = history.get_session_messages(platform, chat_id, session_id)
        return rows, ""
    except Exception as e:  # noqa: BLE001
        logger.warning("RTC 封闭段读取失败(offset=%d): %s", offset, e)
        return [], ""


def build_system_instruction(
    identity_context: str = "",
    recent_conversation: str = "",
    voice_source: str = "live",
) -> str:
    """组装 Live 会话的 systemInstruction。

    结构：精简身份 → 最近对话 → 通话模式须知 → 发音控制（仅 tts 模式）→ 私有规则。
    identity_context 为空时自动回落全量 load_identity_context（保持旧行为兜底）。
    voice_source="tts" 时追加 TTS provider 的语音标记指南：Nora 台词的文本会
    原样进 TTS，情绪/音素标记不会被念出，可用于控制语气和指定词语发音。
    """
    parts: list[str] = []
    if identity_context:
        parts.append(identity_context.strip())
    if recent_conversation:
        parts.append(recent_conversation.strip())
    parts.append(RTC_STYLE_INSTRUCTION)
    if str(voice_source or "").strip().lower() == "tts":
        guidance = _load_voice_guidance()
        if guidance:
            parts.append(guidance)
    rules = load_rules()
    if rules:
        parts.append(rules)
    return "\n\n".join(p for p in parts if p)


def _load_voice_guidance() -> str:
    """voice_source=tts 时取 TTS provider 的写法指南（情绪标记+音素标记）。

    provider 未配置/加载失败返回空串（不阻断通话；TTS 侧未实现流式时会
    回落 Live 音频，此时指南无用但也无害）。
    """
    try:
        from tts.registry import build_tts_provider

        p = build_tts_provider()
        if p is None:
            return ""
        text = str(p.get_text_guidance() or "").strip()
        if not text:
            return ""
        return ("【语音标记说明】\n你的台词文本会被原样送进语音合成（不经过用户阅读），"
                "以下标记只影响发音、不会被念出来：\n" + text)
    except Exception as e:  # noqa: BLE001 - 指南加载失败不该阻断通话
        logger.warning("RTC TTS 语音标记指南加载失败: %s", e)
        return ""


def load_identity_for_call() -> str:
    """拉取精简身份上下文（SOUL/USER）。

    通话是 owner 私聊场景：is_owner=True / private。schedule 不注入
    （通话中用不上，省 token）。
    """
    essential = load_essential_identity()
    if essential:
        return essential
    # 精简路失败（如 workspace 结构不同）回落旧的全量注入
    try:
        from brain.prompts import load_identity_context

        return load_identity_context(
            include_schedule=False, is_owner=True, chat_type="private"
        )
    except Exception as e:  # noqa: BLE001 - 身份加载失败不该阻断通话，带日志降级
        logger.error("RTC 身份上下文加载失败，仅用通话指令: %s", e)
        return ""
