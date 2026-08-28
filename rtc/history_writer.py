"""RTC 通话总结回写（P2 核心链路，rtc.md §5.4）。

通话结束后（挂断 / 超限 / 异常终止都走这里）：
1. 双侧转写（LiveClient.full_transcript）→ fast 模型总结成一段通顺摘要
   （转写无标点无说话人边界，靠总结清洗）
2. 摘要写入 message_history（telegram 私聊 scope，content 带 [RTC通话] 前缀）
   ——让文本侧的 Nora"知道"电话里聊了什么
3. 原始转写全文落 message_log.db 镜像库（查底账用，不进上下文）

严格时序（设计文档 §5.4）：通话结束后先完成回写，"挂断后到摘要落库前"
抵达的入站消息要等摘要先完成——P2 首版先做成 fire-and-forget task + 入库，
消息独占（§5.5 通话期间/收尾窗口的入站拦截）后续补。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 转写超长时截断（送总结模型前）：30 分钟通话的转写可能上万字
MAX_TRANSCRIPT_CHARS = 12000


def format_transcript(transcript: list) -> str:
    """[(who, text)] → 对话文本（带说话人标签，供总结模型读）。"""
    lines = []
    for who, text in transcript:
        speaker = "主人" if who == "user" else "Nora"
        lines.append(f"{speaker}：{text}")
    return "\n".join(lines)


async def summarize_call(transcript: list, duration_seconds: float = 0.0,
                         video_summary: str = "") -> str:
    """fast 模型总结通话。失败/无内容时返回空串（调用方跳过写入）。

    video_summary：视频段描述（recorder 产出，带时间锚点）——有则并入总结输入，
    让 Nora 记得"给你看过什么"。
    """
    text = format_transcript(transcript)
    if not text.strip() and not video_summary:
        return ""
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[-MAX_TRANSCRIPT_CHARS:]  # 保最近的，截前面

    system_prompt = (
        "你是通话记录整理器。下面是一通语音通话的实时转写：没有标点、"
        "没有说话人边界、可能有识别错误。请整理成一段通顺的通话摘要：\n"
        "- 保留两人各自说了什么的关键内容（口语转述，不逐字引用）\n"
        "- 保留情绪氛围（开玩笑/撒娇/认真讨论等）\n"
        "- 有约定/待办要明确写出\n"
        "- 长度不限，内容重要就多写，琐碎就少写，以不丢关键信息为准\n"
        "- 一段话或自然分段皆可，不要列表不要标题\n"
        "- 用第三人称（\"他们聊了…\"）或直接转述（\"主人说…Nora回答…\"）皆可，自然为先"
    )
    user_prompt = f"通话时长约 {int(duration_seconds // 60)} 分钟。"
    if text:
        user_prompt += f"\n\n【通话转写】\n{text}"
    if video_summary:
        user_prompt += f"\n\n{video_summary}"

    try:
        from brain.llm import get_llm_client

        fast = get_llm_client(model_alias="fast")
        summary = await fast.chat(
            system_prompt=system_prompt, user_prompt=user_prompt, history=[]
        )
        summary = str(summary or "").strip()
        if summary.startswith("Error"):
            logger.warning("RTC 通话总结模型返回错误文本，跳过: %s", summary[:100])
            return ""
        return summary
    except Exception as e:  # noqa: BLE001 - 总结失败不阻断收尾
        logger.error("RTC 通话总结失败: %s", e)
        return ""


async def write_call_to_history(
    transcript: list,
    *,
    platform: str = "telegram",
    chat_id: str = "",
    duration_seconds: float = 0.0,
    bytes_up: int = 0,
    bytes_down: int = 0,
    video_summary: str = "",
) -> bool:
    """总结 + 双写（message_history 摘要 / message_log 原始转写）。

    返回 True 表示摘要已写入。任何一步失败都打日志，不抛。
    """
    if (not transcript and not video_summary) or not chat_id:
        return False

    # ---- 1. fast 总结
    summary = await summarize_call(transcript, duration_seconds,
                                   video_summary=video_summary)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
    minutes = max(1, round(duration_seconds / 60)) if duration_seconds else 1

    # ---- 2. 摘要入 message_history（下次对话 Nora 就"记得"这通电话）
    if summary:
        content = (
            f"[{now}]\n[RTC通话 {minutes}分钟] {summary}"
        )
        try:
            from memory.message_history import MessageHistory

            history = MessageHistory()
            history.add_message(platform, chat_id, "assistant", content)
            logger.info(
                "RTC 通话摘要已写入 message_history: %s字（%d分钟通话）",
                len(summary), minutes,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("RTC 摘要写入 message_history 失败: %s", e)

    # ---- 3. 原始转写落镜像库（message_log.db，只追溯不进上下文）
    # 走 MessageLog（context_store.py）的官方 add_message（表结构由它管，
    # 别手写 SQL——曾因列名猜错报 "no column named content"）
    try:
        import time as _time

        from memory.context_store import MessageLog
        from workspace_config import get_workspace_manager

        raw = format_transcript(transcript)
        meta = (
            f"[RTC通话原始转写 {minutes}分钟 up={bytes_up // 1024}KB "
            f"down={bytes_down // 1024}KB]\n{raw}"
        )
        mirror_path = (Path(get_workspace_manager().data_dir)
                       / "memory" / "message_log.db")
        if mirror_path.exists():
            store = MessageLog(db_path=str(mirror_path))
            store.add_message(
                message_id=0,  # 无对应正式消息（镜像独立条目）
                platform=platform,
                chat_id=chat_id,
                role="assistant",
                content_with_timestamp=meta,
                raw_content=raw,
                timestamp=_time.time(),
                metadata={"source": "rtc_call", "duration_minutes": minutes},
            )
            logger.info("RTC 原始转写已落镜像库: %d字", len(raw))
    except Exception as e:  # noqa: BLE001
        logger.warning("RTC 原始转写落镜像库失败（不阻断）: %s", e)

    return bool(summary)
