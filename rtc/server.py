"""RTC 正式服务器（P1）：前端 WS 协议 + 静态托管 + 会话编排。

设计文档 docs/architecture/rtc.md §4（桥接 WS 协议）、§5.1（server 职责）。

与 P0 的 scripts/rtc_p0_server.py 区别：
- hello → welcome 握手（协议版本校验 + 双轨鉴权）
- SessionRegistry 单并发 + 时长硬上限（到点 bye{limit}）
- LiveClient 用 rtc/prompt.py 组装的正式 systemInstruction
- mute 控制上行转发；interrupt / hangup 控制消息
- 全程显式 bye/error，不允许静默断链

两种运行方式：
1. 嵌主进程：`RTCServer(cfg).start()` 在主 asyncio loop 里 create_task
2. 独立进程：`python -m rtc.server`（调试用）
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import secrets
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # 直接 `python rtc/server.py` 跑时补包路径
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from rtc import config_utils
from rtc.auth import RtcAuthenticator
from rtc.live_client import LiveClient, LiveCallbacks, LiveSessionConfig
from rtc.prompt import (
    build_system_instruction, load_identity_for_call, load_recent_conversation,
)
from rtc.session import CallSession, CallState, SessionRegistry

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
PROTOCOL_VERSION = 1

# hello 之后等待下一条消息的超时（鉴权前置：不鉴权不转发任何东西）
HELLO_TIMEOUT_SECONDS = 15.0


def attach_nora_log(console: bool = True) -> None:
    """把 rtc.* 日志挂进 Nora 主日志（workspace/logs/nora.log）。

    独立进程模式（python -m rtc.server）下调用——主日志由
    brain.logging_config 的 TimedRotatingFileHandler 按天轮转，多进程
    追加同文件是安全的（行级原子写），rtc 侧用同格式独立建 handler。
    嵌主进程模式不用调（setup_logging 已接管 root logger）。
    """
    root = logging.getLogger()
    # 已有文件 handler（嵌主进程/重复调用）就不重复挂
    if any(isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root.handlers):
        return
    try:
        from workspace_config import get_workspace_manager

        log_path = str(Path(get_workspace_manager().logs_dir) / "nora.log")
    except Exception as e:  # noqa: BLE001 - 挂不上就回落纯 console
        logger.warning("rtc 日志接入 nora.log 失败，回落 console: %s", e)
        log_path = None

    root.setLevel(logging.INFO)

    # 时区与 Nora 主日志对齐（memory.message_history.timezone，默认 Asia/Shanghai）；
    # 不对齐的话两套日志时间戳差几个时区，排查时对不上
    display_tz = None
    try:
        from brain.logging_config import _resolve_display_timezone

        display_tz = _resolve_display_timezone()
    except Exception:  # noqa: BLE001 - 取不到就服务器本地时区
        pass

    class _TzFormatter(logging.Formatter):
        tz = display_tz

        def formatTime(self, record, datefmt=None):  # noqa: N802
            import datetime as _dt

            if self.tz is not None:
                ct = _dt.datetime.fromtimestamp(record.created, tz=self.tz)
            else:
                ct = _dt.datetime.fromtimestamp(record.created)
            return ct.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    fmt = _TzFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - [SYSTEM] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if log_path:
        fh = logging.handlers.TimedRotatingFileHandler(
            log_path, when="midnight", interval=1, backupCount=14,
            encoding="utf-8",
        )
        fh.suffix = "%Y-%m-%d"
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        root.addHandler(ch)


class RTCServer:
    """正式 RTC 桥接服务器。一个实例管一个 listen 端口。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config_utils.load_config()
        # static_token 为空 → 首次启动随机生成并写回 config.json（仓库/示例
        # 永远不带真实 token；已配置的沿用）
        config_utils.ensure_static_token(self.cfg)
        self.auth = RtcAuthenticator(static_token=str(self.cfg.get("static_token") or ""))
        self.registry = SessionRegistry(max_concurrent=1)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # identity_context 在 start() 时拉一次（通话开始时人设变了也不热更，
        # 一次通话一个快照足够）
        self._identity_context: str = ""
        # owner 的 telegram chat_id（静态 token 轨取当前消息段用），
        # 从 owner_bindings 解析；解析不到留空 = 不注入最近对话
        self._owner_chat_id: str = ""
        # 当前通话的 TTS 下行器（voice_source=tts 时设置；单并发所以一个实例位够）
        self._current_tts_downlink = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """在当前 loop 里启动监听。重复调用幂等。"""
        if self._runner is not None:
            return
        if not self.cfg.get("enabled"):
            logger.info("RTC 未启用（rtc/config.json enabled=false），跳过启动")
            return

        self._identity_context = load_identity_for_call()
        self._owner_chat_id = self._resolve_owner_chat_id()

        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/index.html", self._index)
        app.router.add_get("/audio-worklet.js", self._worklet)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/ws", self._ws_handler)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        host = (self.cfg.get("listen") or {}).get("host", "127.0.0.1")
        port = int((self.cfg.get("listen") or {}).get("port", 8085))
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        logger.info("RTC 服务器已启动: http://%s:%d/ (协议版本 %d)", host, port,
                    PROTOCOL_VERSION)

    async def stop(self) -> None:
        current = self.registry.current
        if current and current.state is not CallState.CLOSED:
            current.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("RTC 服务器已停止")

    # -------------------------------------------------------------- static

    @staticmethod
    def _resolve_owner_chat_id() -> str:
        """owner 的 telegram chat_id（owner_bindings 里 platform=telegram 那条）。"""
        try:
            from core.owner_registry import get_owner_registry

            for b in get_owner_registry().list_bindings():
                if str(b.get("platform") or "").lower() == "telegram":
                    return str(b.get("user_id") or "")
        except Exception as e:  # noqa: BLE001 - 解析不到就不注入最近对话
            logger.warning("owner chat_id 解析失败: %s", e)
        return ""

    NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}

    async def _index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers=self.NO_CACHE)

    async def _worklet(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "audio-worklet.js", headers=self.NO_CACHE)

    async def _healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
            "busy": self.registry.busy(),
        })

    # ------------------------------------------------------------------ ws

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        client_ip = request.remote or ""

        # ---- 1. 等 hello（鉴权前置，超时/格式错直接关）
        hello = await self._await_hello(ws)
        if hello is None:
            return ws

        # ---- 2. 版本校验
        if int(hello.get("version") or 0) > PROTOCOL_VERSION:
            await ws.send_json({"type": "error", "code": "version",
                                "message": f"unsupported version, server={PROTOCOL_VERSION}"})
            await ws.close()
            return ws

        # ---- 3. 双轨鉴权
        auth_cfg = hello.get("auth") or {}
        result = self.auth.authenticate(
            init_data=str(auth_cfg.get("initData") or ""),
            token=str(auth_cfg.get("token") or ""),
            client_ip=client_ip,
        )
        if not result.ok:
            await ws.send_json({"type": "auth_failed", "reason": result.reason})
            await ws.close()
            logger.info("RTC 鉴权失败: ip=%s platform=%s reason=%s",
                        client_ip, result.platform or "-", result.reason)
            return ws
        logger.info("RTC 鉴权通过: user=%s platform=%s", result.user_id,
                    result.platform)

        # ---- 4. 并发判定（单通话）
        session = CallSession(
            session_id=secrets.token_hex(6),
            user_id=result.user_id,
            platform=result.platform,
            display_name=result.display_name,
        )
        ok, holder = await self.registry.try_open(session)
        if not ok:
            await ws.send_json({"type": "error", "code": "busy",
                                "message": f"已有通话进行中（{holder}）"})
            await ws.close()
            return ws

        # ---- 5. 连 Live + welcome
        try:
            await self._run_call(ws, request, session)
        finally:
            session.close()
            self.registry.release(session)
        return ws

    async def _await_hello(self, ws: web.WebSocketResponse) -> dict | None:
        """等首条 hello JSON。任何异常路径都保证 ws 关闭。"""
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await ws.close()
            return None
        if msg.type != aiohttp.WSMsgType.TEXT:
            await ws.close()
            return None
        try:
            import json

            hello = json.loads(msg.data)
        except json.JSONDecodeError:
            await ws.close()
            return None
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await ws.send_json({"type": "error", "code": "hello_expected",
                                "message": "首条消息必须是 hello"})
            await ws.close()
            return None
        return hello

    async def _run_call(self, ws: web.WebSocketResponse, request: web.Request,
                        session: CallSession) -> None:
        """一通电话的完整过程：连 Live → welcome → 双向中继 → 收尾 bye。"""
        api_key = config_utils.get_api_key(self.cfg)
        if not api_key:
            await ws.send_json({"type": "error", "code": "no_api_key",
                                "message": "后端未配置 Google API key"})
            await ws.close()
            return

        voice = str(self.cfg.get("voice_name") or "Kore")
        # 当前消息段按通话发起人取（TG 入口传其 chat_id；静态 token 轨是 owner
        # 的 telegram 私聊——chat_id 兜底用 auth 给的 user_id）
        seg_platform = session.platform if session.platform in ("telegram", "onebotv11") else "telegram"
        seg_chat = session.user_id if session.platform != "static" else self._owner_chat_id
        recent = load_recent_conversation(seg_platform, seg_chat)
        # 语音来源分叉：live（模型内置音色，现状）/ tts（provider 流式合成）。
        # 提前读好传给 prompt 组装——tts 模式要注入 provider 的语音标记指南
        voice_source = str(self.cfg.get("voice_source") or "live").strip().lower()
        if voice_source != "tts":
            voice_source = "live"
        system_instruction = build_system_instruction(
            self._identity_context, recent_conversation=recent,
            voice_source=voice_source,
        )
        # 上下文注入构成（可观测：一次通话注入了什么、多大量）
        logger.info(
            "RTC 上下文注入: 身份=%d字 最近对话=%d字 总systemInstruction=%d字"
            "（滑窗压缩=%dtoken, 时长上限=%ds）",
            len(self._identity_context), len(recent), len(system_instruction),
            int(self.cfg.get("compression_sliding_window") or 8000),
            config_utils.get_max_call_seconds(self.cfg),
        )
        # 语音来源分叉：live 时 TTS 下行器为 None，Live 原生音频照常下发
        tts_downlink = None
        if voice_source == "tts":
            from rtc.tts_downlink import TtsDownlink

            tts_downlink = TtsDownlink(lambda b: self._send(ws, "bin", b))
            # 单并发前提下的 per-call 引用：turn_complete 回调（_on_live_state）
            # 在 LiveCallbacks 闭包外，经实例属性拿到当前通话的下行器
            self._current_tts_downlink = tts_downlink
            logger.info("RTC 语音来源: TTS provider（流式；不可用时回落 Live 音频）")

        async def _on_live_audio(b: bytes) -> None:
            # tts 模式：Live 原生音频不下发（台词走 TTS）——但 TTS 会话
            # 建立不了（未实现/无 provider）时照常下发，通话不能哑
            if tts_downlink is not None and not tts_downlink.unavailable:
                return
            await self._send(ws, "bin", b)

        client = LiveClient(
            LiveSessionConfig(
                model=str(self.cfg.get("model") or "gemini-3.1-flash-live-preview"),
                api_key=api_key,
                system_instruction=system_instruction,
                voice_name=voice,
                proxy=(str(self.cfg.get("proxy") or "").strip() or None),
            ),
            LiveCallbacks(
                on_audio=_on_live_audio,
                on_transcript=lambda who, text: self._on_transcript(
                    ws, tts_downlink, who, text),
                on_interrupted=lambda: self._send(ws, "json", {"type": "interrupted"}),
                on_state=lambda st, detail: self._on_live_state(ws, client, session, st, detail),
            ),
        )

        await self._send(ws, "json", {"type": "state", "state": "connecting"})
        try:
            await client.connect()
        except Exception as e:  # noqa: BLE001
            logger.error("Live 连接失败: %s", e)
            await ws.send_json({"type": "error", "code": "live_connect",
                                "message": f"Live 连接失败: {e}"})
            await ws.close()
            return

        session.activate()
        max_seconds = config_utils.get_max_call_seconds(self.cfg)
        session.arm_limit_timer(
            max_seconds,
            on_limit=lambda s: self._on_limit(ws, client, s),
        )
        await self._send(ws, "json", {
            "type": "welcome",
            "version": PROTOCOL_VERSION,
            "display_name": session.display_name or session.user_id,
            "voice": voice,
            "max_seconds": max_seconds,
        })
        logger.info("RTC 通话开始: %s", session.snapshot())

        # 视频段录制（旁路消费者：video_on/off 之间缓存帧，收尾时总结）
        from rtc.recorder import VideoRecorder

        recorder = VideoRecorder()
        call_started_at = time.monotonic()

        # 通话心跳：每 60s 打一条流量/时长快照（通话中唯一周期性观测点）
        heartbeat = asyncio.get_running_loop().create_task(
            self._heartbeat_loop(client, session)
        )

        # ---- 上行循环（下行由 LiveClient 回调推送）
        peer_hung_up = False
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if session.muted:
                        continue  # 静音 = 停止上行转发；下行照常
                    session.bytes_up += len(msg.data)
                    await client.send_audio(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    import base64 as _b64
                    import json

                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    act = data.get("action") or ""
                    if act == "mute":
                        session.muted = True
                        logger.info("RTC 静音: session=%s", session.session_id)
                    elif act == "unmute":
                        session.muted = False
                        logger.info("RTC 取消静音: session=%s", session.session_id)
                    elif act == "interrupt":
                        await client.send_interrupt()
                    elif act == "hangup":
                        peer_hung_up = True
                        break
                    elif data.get("type") == "video_frame" and data.get("data"):
                        # 摄像头帧：base64 JPEG → Live 离散视频输入（1FPS 由前端控制）
                        jpeg = _b64.b64decode(data["data"])
                        session.bytes_up += len(jpeg)
                        recorder.add_frame(jpeg)  # 旁路录制（第二消费者）
                        await client.send_video_frame(jpeg)
                    elif data.get("type") == "video_on":
                        recorder.start_segment()
                    elif data.get("type") == "video_off":
                        recorder.end_segment()
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
        finally:
            heartbeat.cancel()
            reason = "peer_hangup" if peer_hung_up else "peer_closed"
            await client.close()
            if tts_downlink is not None:
                await tts_downlink.close()
                self._current_tts_downlink = None
            session.bytes_down = client.bytes_in
            if not ws.closed:
                try:
                    await ws.send_json({"type": "bye", "reason": reason})
                    await ws.close()
                except Exception:  # noqa: BLE001 - 收尾路径不抛
                    pass
            logger.info("RTC 通话结束: %s up=%dKB down=%dKB 挂断方式=%s",
                        session.snapshot(), session.bytes_up // 1024,
                        session.bytes_down // 1024, reason)
            # ---- 通话总结回写（P2）：转写 → fast 总结 → message_history + 镜像库
            # 后台 task 执行：ws 已关不阻塞收尾；主进程模式下 loop 一直活着
            transcript = client.full_transcript
            video_segments = await recorder.close()
            if transcript or video_segments:
                asyncio.get_running_loop().create_task(
                    self._finalize_call_history(transcript, session, reason,
                                                video_segments, call_started_at)
                )
            else:
                logger.info("RTC 通话无转写内容，跳过总结回写")

    async def _finalize_call_history(self, transcript: list,
                                     session: CallSession, reason: str,
                                     video_segments: list,
                                     call_started_at: float) -> None:
        """通话收尾后的总结回写（独立 task，异常只记日志）。

        视频段先总结（每段关键帧每 5 张一批送 image 模型），
        段描述与聊天转写一起进 fast 总总结（rtc.md §5.4 时序）。
        """
        try:
            from rtc.history_writer import write_call_to_history
            from rtc.recorder import summarize_all_segments

            # 写入目标 = 通话发起人的对话 scope（TG 入口即其私聊；
            # 静态 token 轨是 owner 的 telegram 私聊）
            platform = (session.platform
                        if session.platform in ("telegram", "onebotv11")
                        else "telegram")
            chat_id = (session.user_id if session.platform != "static"
                       else self._owner_chat_id)
            if not chat_id:
                logger.warning("RTC 总结回写跳过：无法确定写入的 chat_id")
                return
            logger.info("RTC 开始通话总结: %d turns, %d 视频段（后台进行，稍后入库）",
                        len(transcript), len(video_segments))
            # 视频段描述（先于总总结，其输出是总总结的输入之一）
            video_desc = ""
            if video_segments:
                try:
                    video_desc = await summarize_all_segments(
                        video_segments, call_started_at)
                    if video_desc:
                        logger.info("RTC 视频段描述完成: %d字", len(video_desc))
                except Exception as e:  # noqa: BLE001 - 视频总结失败不阻断文字总结
                    logger.error("RTC 视频段总结失败（跳过画面部分）: %s", e)
            ok = await write_call_to_history(
                transcript,
                platform=platform,
                chat_id=chat_id,
                duration_seconds=session.active_seconds,
                bytes_up=session.bytes_up,
                bytes_down=session.bytes_down,
                video_summary=video_desc,
            )
            if not ok:
                logger.info("RTC 通话总结未产出（内容不足或模型失败）")
        except Exception as e:  # noqa: BLE001 - 回写失败不影响通话收尾
            logger.error("RTC 通话总结回写异常: %s", e)

    async def _on_transcript(self, ws: web.WebSocketResponse, tts_downlink,
                             who: str, text: str) -> None:
        """转写转发：前端字幕 + tts 模式喂下行合成器（assistant 侧）。"""
        # tts 模式：Nora 的台词增量进 TTS 管线（流式会话边收边合成）
        if tts_downlink is not None and who == "assistant":
            await tts_downlink.feed_transcript(text)
        await self._send(ws, "json", {"type": "transcript", "who": who, "text": text})

    async def _on_live_state(self, ws: web.WebSocketResponse, client: LiveClient,
                             session: CallSession, state: str, detail: str) -> None:
        """LiveClient on_state 的桥接：转发给前端 + 服务器侧日志。

        turn_complete 的 detail 是 {"user":..., "assistant":...} JSON——
        通话内容可观测性全靠这里（谁说了什么，每轮一条）。
        """
        # 服务器侧日志
        if state == "turn_complete":
            try:
                import json as _json

                turn = _json.loads(detail) if detail else {}
                user_text = str(turn.get("user") or "").strip()
                assistant_text = str(turn.get("assistant") or "").strip()
                logger.info(
                    "RTC turn: session=%s\n  主人: %s\n  Nora: %s",
                    session.session_id,
                    user_text[:200] if user_text else "（无）",
                    assistant_text[:200] if assistant_text else "（无）",
                )
                # tts 模式：turn 收口 → 下行合成器收尾（流式 flush / 回落整句合成）
                if self._current_tts_downlink is not None:
                    await self._current_tts_downlink.end_turn()
            except (ValueError, TypeError):
                logger.info("RTC turn: session=%s detail=%s", session.session_id,
                            detail[:200])
        elif state == "goaway":
            logger.warning("RTC Google goAway 预告掐链: session=%s timeLeft=%s",
                           session.session_id, detail)
        elif state in ("rotating", "resuming"):
            logger.info("RTC 连接轮换: session=%s state=%s", session.session_id, state)
        elif state == "closed":
            logger.info("RTC Live 连接断开: session=%s", session.session_id)

        # 转发前端
        await self._send(ws, "json", {"type": "state", "state": state, "detail": detail})

    async def _heartbeat_loop(self, client: LiveClient, session: CallSession) -> None:
        """通话中每 60s 一条流量/时长快照日志。通话结束自动退出。"""
        try:
            while session.state in (CallState.CONNECTING, CallState.ACTIVE):
                await asyncio.sleep(60)
                if session.state not in (CallState.CONNECTING, CallState.ACTIVE):
                    break
                logger.info(
                    "RTC 通话心跳: session=%s %ds up=%dKB down=%dKB",
                    session.session_id, int(session.active_seconds),
                    (session.bytes_up + client.bytes_out) // 1024,
                    client.bytes_in // 1024,
                )
        except asyncio.CancelledError:
            return

    async def _on_limit(self, ws: web.WebSocketResponse, client: LiveClient,
                        session: CallSession) -> None:
        """时长上限到点：发 bye{limit} 并关 WS。

        只关 ws 不直接关 client——ws 关闭会让 _run_call 的上行循环退出，
        其 finally 统一收链（避免双 close 竞态）。
        """
        if not session.begin_ending():
            return
        try:
            await ws.send_json({"type": "bye", "reason": "limit"})
        except Exception:  # noqa: BLE001
            pass
        if not ws.closed:
            await ws.close()

    # 回调侧发送封装：LiveClient 回调在 recv_loop 里同步 await，
    # ws.send_* 返回的 Future 这里直接 await 保证顺序与背压
    async def _send(self, ws: web.WebSocketResponse, kind: str, payload) -> None:
        try:
            if kind == "bin":
                await ws.send_bytes(payload)
            else:
                await ws.send_json(payload)
        except ConnectionResetError:
            pass


async def _standalone_main() -> None:  # pragma: no cover - 独立进程入口
    # 日志接入 Nora 主日志（workspace/logs/nora.log，按天轮转）+ console
    attach_nora_log(console=True)
    cfg = config_utils.load_config()
    cfg["enabled"] = True  # 独立跑时无视 enabled 开关
    server = RTCServer(cfg)
    await server.start()
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_standalone_main())
