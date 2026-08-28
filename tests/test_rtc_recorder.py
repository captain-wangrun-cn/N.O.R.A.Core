"""RTC 视频段录制/关键帧总结测试（P2）。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rtc.recorder import (
    BATCH_SIZE, VideoRecorder, VideoSegment, summarize_segment,
)


def _fake_jpeg(n: int = 100) -> bytes:
    return b"\xff\xd8" + bytes([n % 256]) * n + b"\xff\xd9"


class TestVideoRecorder:
    def test_segment_lifecycle(self):
        rec = VideoRecorder()
        assert not rec.recording
        rec.start_segment()
        assert rec.recording
        rec.add_frame(_fake_jpeg(10))
        rec.add_frame(_fake_jpeg(20))
        seg = rec.end_segment()
        assert seg is not None and len(seg.frames) == 2
        assert not rec.recording

    def test_start_segment_idempotent(self):
        rec = VideoRecorder()
        rec.start_segment()
        rec.start_segment()  # 重复开不重置
        rec.add_frame(_fake_jpeg())
        assert len(rec._current.frames) == 1

    def test_frames_without_segment_dropped(self):
        rec = VideoRecorder()
        rec.add_frame(_fake_jpeg())  # 没开段
        assert rec.end_segment() is None

    def test_end_empty_segment_discarded(self):
        rec = VideoRecorder()
        rec.start_segment()
        assert rec.end_segment() is None  # 没帧的段直接丢
        assert rec.segments == []

    @pytest.mark.anyio
    async def test_close_seals_open_segment(self):
        rec = VideoRecorder()
        rec.start_segment()
        rec.add_frame(_fake_jpeg())
        segs = await rec.close()
        assert len(segs) == 1

    def test_frame_cap(self):
        rec = VideoRecorder()
        rec.start_segment()
        for _ in range(200):  # 超过 MAX_FRAMES_PER_SEGMENT=180
            rec.add_frame(_fake_jpeg(1))
        assert len(rec._current.frames) == 180


class TestVideoSegment:
    def test_keyframes_first_frame_always_picked(self):
        import time as _t

        t0 = _t.monotonic()
        seg = VideoSegment(started_at=t0)
        seg.frames = [(t0 + i * 1.0, _fake_jpeg(i)) for i in range(30)]  # 30 秒
        keys = seg.keyframes(interval=5.0)
        # 首帧保住 + 每 5 秒一张 ≈ 6 张
        assert keys[0] is seg.frames[0]
        assert 4 <= len(keys) <= 8

    def test_duration(self):
        import time as _t

        t0 = _t.monotonic()
        seg = VideoSegment(started_at=t0)
        seg.frames = [(t0, b""), (t0 + 10, b"")]
        seg.ended_at = t0 + 12
        assert 11.9 < seg.duration_seconds <= 12.1


class TestSummarizeSegment:
    @pytest.mark.anyio
    async def test_batched_by_five(self):
        """关键帧 >5 张时分批送，每批 ≤5 张（用户拍板：一次 5 张）。"""
        import time as _t

        t0 = _t.monotonic()
        seg = VideoSegment(started_at=t0)
        # 60 秒的段、interval=5 → ~12 关键帧 → 3 批（5+5+2）
        seg.frames = [(t0 + i * 1.0, _fake_jpeg(i)) for i in range(60)]
        seg.ended_at = t0 + 60

        call_batches = []

        async def fake_describe(batch, rel_minutes):
            call_batches.append(len(batch))
            return f"第{len(call_batches)}批描述"

        with patch("rtc.recorder._describe_frame_batch", side_effect=fake_describe):
            desc = await summarize_segment(seg, call_started_at=t0 - 30)

        assert call_batches == [5, 5, 2]  # 分批正确
        assert "第1批描述" in desc and "视频段" in desc

    @pytest.mark.anyio
    async def test_no_frames_returns_empty(self):
        import time as _t

        seg = VideoSegment(started_at=_t.monotonic())
        assert await summarize_segment(seg, _t.monotonic()) == ""

    @pytest.mark.anyio
    async def test_all_batches_fail_returns_empty(self):
        import time as _t

        t0 = _t.monotonic()
        seg = VideoSegment(started_at=t0)
        seg.frames = [(t0 + i, _fake_jpeg(i)) for i in range(10)]
        seg.ended_at = t0 + 10

        async def failing(batch, rel_minutes):
            return ""

        with patch("rtc.recorder._describe_frame_batch", side_effect=failing):
            desc = await summarize_segment(seg, t0)
        assert desc == ""
