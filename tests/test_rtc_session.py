"""RTC 会话/注册表测试（P1）：状态机 + 时长上限 + 单并发。"""

import asyncio

import pytest

from rtc.session import CallSession, CallState, SessionRegistry


def _session(user_id: str = "42") -> CallSession:
    return CallSession(session_id="s1", user_id=user_id, platform="telegram")


class TestCallSession:
    def test_state_flow(self):
        s = _session()
        assert s.state is CallState.CONNECTING
        s.activate()
        assert s.state is CallState.ACTIVE
        assert s.begin_ending()
        assert s.state is CallState.ENDING
        assert not s.begin_ending()  # 幂等
        s.close()
        assert s.state is CallState.CLOSED

    def test_activate_requires_connecting(self):
        s = _session()
        s.activate()
        with pytest.raises(RuntimeError):
            s.activate()

    def test_mute_flag(self):
        s = _session()
        assert not s.muted
        s.muted = True
        assert s.muted

    def test_snapshot_fields(self):
        s = _session()
        snap = s.snapshot()
        assert snap["session_id"] == "s1"
        assert snap["state"] == "connecting"
        assert snap["user_id"] == "42"


class TestLimitTimer:
    @pytest.mark.anyio
    async def test_limit_fires_and_calls_callback(self):
        s = _session()
        s.activate()
        called = []

        async def on_limit(sess):
            called.append(sess.session_id)

        s.arm_limit_timer(0.05, on_limit)
        await asyncio.sleep(0.15)
        assert called == ["s1"]
        s.close()

    @pytest.mark.anyio
    async def test_close_cancels_timer(self):
        s = _session()
        s.activate()
        called = []

        async def on_limit(sess):
            called.append(1)

        s.arm_limit_timer(0.05, on_limit)
        s.close()
        await asyncio.sleep(0.15)
        assert called == []

    @pytest.mark.anyio
    async def test_ending_skips_callback(self):
        s = _session()
        s.activate()
        called = []

        async def on_limit(sess):
            called.append(1)

        s.arm_limit_timer(0.05, on_limit)
        s.begin_ending()  # 正常收尾路径先走，limit 到点不重复发 bye
        await asyncio.sleep(0.15)
        assert called == []


class TestSessionRegistry:
    @pytest.mark.anyio
    async def test_single_concurrency(self):
        reg = SessionRegistry()
        s1, s2 = _session("a"), _session("b")
        ok, _ = await reg.try_open(s1)
        assert ok
        ok2, holder = await reg.try_open(s2)
        assert not ok2 and holder == "a"
        # 原 session 结束后可再开
        s1.close()
        reg.release(s1)
        ok3, _ = await reg.try_open(s2)
        assert ok3

    @pytest.mark.anyio
    async def test_release_only_own_session(self):
        reg = SessionRegistry()
        s1, s2 = _session("a"), _session("b")
        await reg.try_open(s1)
        reg.release(s2)  # 不是自己，不该影响占用
        assert reg.busy()
        reg.release(s1)
        assert not reg.busy()

    @pytest.mark.anyio
    async def test_busy_states(self):
        reg = SessionRegistry()
        s = _session()
        await reg.try_open(s)
        assert reg.busy()  # CONNECTING 也算占用
        s.activate()
        assert reg.busy()
        s.begin_ending()
        assert reg.busy()  # ENDING 仍占（收尾中）
        s.close()
        reg.release(s)
        assert not reg.busy()
