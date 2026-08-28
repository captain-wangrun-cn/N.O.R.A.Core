"""RTC P0 冒烟脚本：纯后端跑通 Gemini Live 会话。

用法（在仓库根目录）:
    .venv\\Scripts\\python scripts\\rtc_smoke.py              # 文本触发对话
    .venv\\Scripts\\python scripts\\rtc_smoke.py --text 你好   # 指定开场白
    .venv\\Scripts\\python scripts\\rtc_smoke.py --mic        # 本地麦克风实时对讲（P0 测试页的命令行版）
    .venv\\Scripts\\python scripts\\rtc_smoke.py --video      # 视频计时实测（开放问题④）

验证项（docs/architecture/rtc.md §11 P0）：
1. 起 Live 会话（setup → setupComplete）
2. 双侧转写累积（input/outputAudioTranscription）
3. 下行音频落盘成 wav（收完可人工听）
4. 连接层事件（goAway / sessionResumptionUpdate）日志可见
5. --video：发视频帧实测会话预算行为

音频链路：无输入文件依赖——文本触发（clientContent.turns）让模型开口，
用麦克风时上行 16k PCM16。跑不起代理时可设 rtc/config.json proxy。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import wave
from pathlib import Path

# 仓库根目录进 sys.path（脚本方式运行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rtc import config_utils
from rtc.live_client import LiveClient, LiveCallbacks, LiveSessionConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rtc_smoke")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "logs" / "rtc_smoke"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class SmokeSink:
    """冒烟结果收集：音频落 wav、转写打印、turn 文本累积。"""

    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.turns: list[dict] = []  # {"user":..., "assistant":...}
        self.transcript_lines: list[str] = []

    async def on_audio(self, pcm: bytes) -> None:
        self.audio_chunks.append(pcm)
        logger.info("↓ audio %d bytes (累计 %d KB)", len(pcm),
                    sum(len(c) for c in self.audio_chunks) // 1024)

    async def on_transcript(self, who: str, text: str) -> None:
        line = f"[{who}] {text}"
        self.transcript_lines.append(line)
        print(f"  转写 {line}")

    async def on_interrupted(self) -> None:
        print("  ⚡ interrupted（打断生效）")

    async def on_state(self, state: str, detail: str) -> None:
        if state == "turn_complete":
            turn = json.loads(detail)
            self.turns.append(turn)
            print(f"  ✅ turn 完成: user={turn.get('user')!r} "
                  f"assistant={turn.get('assistant')!r}")
        else:
            logger.info("state=%s detail=%s", state, detail)

    async def on_tool_call(self, calls: list) -> None:
        print(f"  🔧 toolCall: {calls}")

    def save_wav(self, path: Path, sample_rate: int = 24000) -> int:
        data = b"".join(self.audio_chunks)
        if not data:
            return 0
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(data)
        return len(data)


async def mic_stream(client: LiveClient) -> None:
    """命令行麦克风模式：Windows 上用 sounddevice 采集 16k PCM16 实时上行。

    sounddevice 不在 requirements 里，--mic 时提示安装（pip install sounddevice）。
    """
    try:
        import sounddevice as sd
    except ImportError:
        print("麦克风模式需要 sounddevice: .venv\\Scripts\\pip install sounddevice")
        return

    import numpy as np

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    def callback(indata, frames, time_info, status):
        pcm = (indata[:, 0] * 32768).astype("<i2").tobytes()
        try:
            q.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # 丢包优于阻塞音频线程

    print("🎤 麦克风已开（说话即上行；Ctrl+C 挂断）")
    with sd.RawInputStream(
        samplerate=16000, channels=1, dtype="float32",
        blocksize=2048, callback=callback,
    ):
        while True:
            pcm = await q.get()
            await client.send_audio(pcm)


async def fake_video_stream(client: LiveClient, duration: float = 5.0) -> None:
    """视频计时实测：发 duration 秒 1FPS 纯色 JPEG 帧，观察会话是否被 2 分钟预算掐。

    Pillow 生成 320x240 三色循环帧，不依赖摄像头。
    """
    from PIL import Image

    print(f"📹 发送 {duration:.0f}s 视频帧（1FPS 纯色循环）...")
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    for i in range(int(duration)):
        img = Image.new("RGB", (320, 240), colors[i % 3])
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        await client.send_video_frame(buf.getvalue())
        print(f"  frame {i + 1}/{int(duration)} 已发")
        await asyncio.sleep(1.0)
    print("📹 视频帧发送完毕（继续观察会话存活与 turn 行为）")


async def run(args: argparse.Namespace) -> int:
    rtc_cfg = config_utils.load_config()
    api_key = config_utils.get_api_key(rtc_cfg)
    if not api_key:
        print("❌ 没有 API key：rtc/config.json 的 api_key 或全局 gemini provider 均为空")
        return 1

    sink = SmokeSink()
    cfg = LiveSessionConfig(
        model=rtc_cfg.get("model", "gemini-3.1-flash-live-preview"),
        api_key=api_key,
        system_instruction=(
            "你是 Nora，正在和你的主人进行语音通话。用简短口语化中文回答，"
            "每次回答控制在两三句话以内，像打电话一样自然。"
        ),
        voice_name=rtc_cfg.get("voice_name", "Kore"),
        proxy=(rtc_cfg.get("proxy") or "").strip() or None,
    )
    client = LiveClient(cfg, LiveCallbacks(
        on_audio=sink.on_audio,
        on_transcript=sink.on_transcript,
        on_interrupted=sink.on_interrupted,
        on_state=sink.on_state,
        on_tool_call=sink.on_tool_call,
    ))

    print(f"连接 Live API: model={cfg.model} voice={cfg.voice_name}")
    t0 = time.monotonic()
    try:
        await client.connect()
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        return 1
    print(f"✅ setupComplete（{time.monotonic() - t0:.1f}s）")

    rc = 0
    try:
        await client.send_text(args.text)
        if args.video:
            await fake_video_stream(client, args.video_seconds)
        if args.mic:
            await mic_stream(client)  # 阻塞到 Ctrl+C
        else:
            # 文本模式：等 turn 完成 + 收完音频（10s 静音兜底）
            deadline = time.monotonic() + args.wait
            last_audio = time.monotonic()
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if sink.audio_chunks:
                    last_audio = time.monotonic()
                elif time.monotonic() - last_audio > 10.0:
                    print("（10s 无新音频，认为本轮结束）")
                    break
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        await client.close()

    # 落盘结果
    wav_path = OUTPUT_DIR / "smoke_output.wav"
    nbytes = sink.save_wav(wav_path)
    if nbytes:
        print(f"🔊 下行音频落盘: {wav_path}（{nbytes / 1024:.0f} KB ≈ "
              f"{nbytes / 2 / 24000:.1f}s）")
    txt_path = OUTPUT_DIR / "smoke_transcript.txt"
    txt_path.write_text(
        "\n".join(sink.transcript_lines) + "\n", encoding="utf-8")
    print(f"📝 转写共 {len(sink.turns)} 个 turn，落盘: {txt_path}")
    print(f"📊 字节统计: 上行 {client.bytes_out / 1024:.0f} KB / "
          f"下行 {client.bytes_in / 1024:.0f} KB / 连接存活 "
          f"{client.uptime_seconds:.1f}s")
    if args.video:
        print("⏱️  视频计时观察: 上述连接存活时间内若会话被掐（goAway/ABORTED），"
              "则'开过视频即 2 分钟预算'假设成立")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description="RTC P0 冒烟")
    parser.add_argument("--text", default="你好，能听到我说话吗？简单回应一下。",
                        help="开场文本（触发模型开口）")
    parser.add_argument("--mic", action="store_true", help="本地麦克风实时对讲")
    parser.add_argument("--video", action="store_true", help="实测视频计时行为")
    parser.add_argument("--video-seconds", type=float, default=5.0,
                        help="视频帧发送时长（秒）")
    parser.add_argument("--wait", type=float, default=60.0,
                        help="文本模式最长等待（秒）")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
