"""RTC 视频段录制 + 关键帧总结（P2，rtc.md §5.4 recorder 实现注记）。

设计：
- **旁路缓存**：video_frame 到达 server 时本来就要转发 Google（realtimeInput.video），
  录制 = 同一条链加第二个消费者，零额外采集成本
- **封段**：video_on → video_off 之间是一段；缓存 (monotonic, JPEG) 序列
- **总结**：每段抽关键帧 → **每 5 张一批**送 image 模型多图输入（用户拍板：
  分批送，不是一次全喂）→ 每批出一段描述 → 合并成该段的画面描述（带时间锚点）
- 段描述最终汇入 history_writer 的通话总结（"聊着聊着给你看了个东西"的还原）

为什么不编码真视频：省 ffmpeg；image 模型多图链路现成（brain/llm.py）。
若未来 image 链路要求真视频文件再降级 ffmpeg（引入时更新 requirements/文档）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 关键帧抽取间隔（秒）：1FPS 原始帧里每 N 秒取 1 张
KEYFRAME_INTERVAL_SECONDS = 5.0
# 每批送 image 模型的张数（用户拍板：分批，一次 5 张）
BATCH_SIZE = 5
# 单段帧数上限（防异常长段：2 分钟 × 1FPS = 120 帧）
MAX_FRAMES_PER_SEGMENT = 180


@dataclass
class VideoSegment:
    """一段连续的视频展示（video_on → video_off）。"""

    started_at: float  # monotonic
    frames: list = field(default_factory=list)  # [(monotonic, jpeg_bytes)]
    ended_at: float = 0.0

    @property
    def duration_seconds(self) -> float:
        if not self.frames:
            return 0.0
        end = self.ended_at or self.frames[-1][0]
        return max(0.0, end - self.started_at)

    def keyframes(self, interval: float = KEYFRAME_INTERVAL_SECONDS) -> list:
        """按时间间隔抽关键帧（保首帧：首帧是用户刚开摄像头看到的东西）。"""
        if not self.frames:
            return []
        picked = [self.frames[0]]
        last_t = self.frames[0][0]
        for t, jpeg in self.frames[1:]:
            if t - last_t >= interval:
                picked.append((t, jpeg))
                last_t = t
        return picked

    def snapshot(self) -> dict:
        return {
            "frames": len(self.frames),
            "duration": round(self.duration_seconds, 1),
        }


class VideoRecorder:
    """通话级录制器：start_segment / add_frame / end_segment，多段累积。"""

    def __init__(self):
        self.segments: list[VideoSegment] = []
        self._current: VideoSegment | None = None

    @property
    def recording(self) -> bool:
        return self._current is not None

    def start_segment(self) -> None:
        if self._current is not None:
            return  # 已在录（video_on 幂等）
        self._current = VideoSegment(started_at=time.monotonic())
        logger.info("RTC 视频段开始录制")

    def add_frame(self, jpeg: bytes) -> None:
        """旁路消费者：server 每收到一帧 video_frame 调一次。"""
        if self._current is None:
            return  # 没开段（video_off 状态的迟到帧）丢弃
        if len(self._current.frames) >= MAX_FRAMES_PER_SEGMENT:
            return  # 超限静默丢弃（防异常长段撑爆内存）
        self._current.frames.append((time.monotonic(), jpeg))

    def end_segment(self) -> VideoSegment | None:
        """封段。返回该段（None = 没有开着的段）。"""
        seg = self._current
        self._current = None
        if seg is None:
            return None
        seg.ended_at = time.monotonic()
        if seg.frames:
            self.segments.append(seg)
            logger.info("RTC 视频段封段: %s", seg.snapshot())
        return seg if seg.frames else None

    async def close(self) -> list[VideoSegment]:
        """通话结束收尾：封掉未关的段，返回全部段。"""
        if self._current is not None:
            self.end_segment()
        return self.segments


async def summarize_segment(segment: VideoSegment,
                            call_started_at: float) -> str:
    """一段视频的画面描述：关键帧每 5 张一批送 image 模型，合并各批描述。

    call_started_at 是通话开始的 monotonic 时间（把段内相对时间转成
    "通话第 N 分钟"的锚点）。
    """
    keys = segment.keyframes()
    if not keys:
        return ""

    def rel_minutes(t: float) -> float:
        return max(0.0, (t - call_started_at) / 60.0)

    batch_descriptions: list[str] = []
    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i:i + BATCH_SIZE]
        desc = await _describe_frame_batch(batch, rel_minutes)
        if desc:
            batch_descriptions.append(desc)

    if not batch_descriptions:
        return ""

    start_min = rel_minutes(segment.started_at)
    end_min = rel_minutes(segment.ended_at or segment.frames[-1][0])
    header = (f"视频段（通话第 {start_min:.1f}~{end_min:.1f} 分钟，"
              f"约 {segment.duration_seconds:.0f} 秒）")
    return header + "：" + "；".join(batch_descriptions)


async def _describe_frame_batch(batch: list, rel_minutes) -> str:
    """一批关键帧（≤5 张）→ image 模型描述。失败返回空串。"""
    import base64

    from brain.llm import get_llm_client

    # multimodal_images 的格式：[{path, mime_type, base64}, ...]（Dict 列表，
    # 不是裸路径串——曾因此报 'str' object has no attribute 'get'）
    images = []
    for _t, jpeg in batch:
        images.append({
            "mime_type": "image/jpeg",
            "base64": base64.b64encode(jpeg).decode("ascii"),
        })

    image_llm = get_llm_client(model_alias="image")
    times = ", ".join(f"第{rel_minutes(t):.1f}分钟" for t, _ in batch)
    prompt = (
        "这些是同一次视频通话里用户摄像头画面的截图（按时间顺序，"
        f"分别拍摄于通话{times}）。请描述用户在这批画面里展示了什么："
        "场景、物体、变化。一两句话，客观描述，不要猜测意图。"
    )
    try:
        desc = await image_llm.chat(
            system_prompt="你是图像内容描述器。描述画面本身，不寒暄不提问。",
            user_prompt=prompt,
            history=[],
            multimodal_images=images,
        )
    except Exception as e:  # noqa: BLE001 - 单批失败不阻断其它批
        logger.warning("RTC 视频帧批次描述异常: %s", e)
        return ""
    desc = str(desc or "").strip()
    if desc.startswith("Error"):
        logger.warning("RTC 视频帧批次描述失败: %s", desc[:80])
        return ""
    return desc


async def summarize_all_segments(segments: list, call_started_at: float) -> str:
    """全部视频段的描述合并（无视频段返回空串）。"""
    if not segments:
        return ""
    parts = []
    for seg in segments:
        desc = await summarize_segment(seg, call_started_at)
        if desc:
            parts.append(desc)
    if not parts:
        return ""
    return "\n\n【通话中展示的画面】\n" + "\n".join(parts)
