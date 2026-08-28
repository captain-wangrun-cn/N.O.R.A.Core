"""RTC TTS 下行链路测试（voice_source=tts）。"""

import pytest

from rtc.tts_downlink import TtsDownlink


class _FakeLiveSession:
    """符合 BaseTTSLiveSession 约定的假流式会话。"""

    def __init__(self):
        self.fed = []
        self.flushed = 0
        self.closed = False

    async def feed_text(self, text):
        self.fed.append(text)

    async def flush(self):
        self.flushed += 1

    async def end_turn(self):
        self.flushed += 1

    async def close(self):
        self.closed = True


class _StreamingProvider:
    name = "fake_stream"

    def open_live_session(self, on_audio):
        return _FakeLiveSession()


class _NonStreamingProvider:
    """没实现 open_live_session（基类默认返回 None 的等价物）。"""
    name = "fake_plain"


class TestTtsDownlink:
    @pytest.mark.anyio
    async def test_streaming_flow(self):
        """流式 provider：转写增量即时喂、turn 收口 flush、close 关会话。"""
        chunks = []
        dl = TtsDownlink(lambda b: chunks.append(b))
        with pytest.MonkeyPatch().context() as m:
            m.setattr("rtc.tts_downlink.TtsDownlink._get_provider",
                      lambda self: _StreamingProvider())
            await dl.feed_transcript("你")
            await dl.feed_transcript("好")
            session = dl._live
            assert session.fed == ["你", "好"]
            assert not dl.unavailable
            await dl.end_turn()
            assert session.flushed == 1
            await dl.close()
            assert session.closed

    @pytest.mark.anyio
    async def test_non_streaming_falls_back(self):
        """未实现 open_live_session：unavailable=True（server 据此照常发 Live 音频）。"""
        dl = TtsDownlink(lambda b: None)
        with pytest.MonkeyPatch().context() as m:
            m.setattr("rtc.tts_downlink.TtsDownlink._get_provider",
                      lambda self: _NonStreamingProvider())
            await dl.feed_transcript("你好")
            assert dl.unavailable is True
            # 后续 feed/end_turn 全部无害 no-op
            await dl.feed_transcript("继续")
            await dl.end_turn()
            await dl.close()

    @pytest.mark.anyio
    async def test_no_provider_falls_back(self):
        """没配置 provider：unavailable=True。"""
        dl = TtsDownlink(lambda b: None)
        with pytest.MonkeyPatch().context() as m:
            m.setattr("rtc.tts_downlink.TtsDownlink._get_provider",
                      lambda self: None)
            await dl.feed_transcript("x")
            assert dl.unavailable is True

    @pytest.mark.anyio
    async def test_empty_text_ignored(self):
        dl = TtsDownlink(lambda b: None)
        await dl.feed_transcript("")
        await dl.feed_transcript("   ")
        assert dl._live is None

    @pytest.mark.anyio
    async def test_open_session_exception_falls_back(self):
        """open_live_session 抛异常：回落而不是崩。"""
        class _Boom:
            name = "boom"

            def open_live_session(self, on_audio):
                raise RuntimeError("boom")

        dl = TtsDownlink(lambda b: None)
        with pytest.MonkeyPatch().context() as m:
            m.setattr("rtc.tts_downlink.TtsDownlink._get_provider",
                      lambda self: _Boom())
            await dl.feed_transcript("hi")
            assert dl.unavailable is True
