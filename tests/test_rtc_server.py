"""RTC 服务器协议测试（P1）：hello/welcome 握手 + 鉴权 + 控制消息 + 时长上限。

不起 Google Live：LiveClient 被桩替换（只验证桥接协议行为）。
HTTP/WS 用真实 AppRunner + 127.0.0.1 随机端口（不依赖 aiohttp_client fixture；
本仓 venv 无 pytest-asyncio，async 测试统一 anyio mark）。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from rtc.server import RTCServer


def _base_cfg(**over):
    cfg = {
        "enabled": True,
        "listen": {"host": "127.0.0.1", "port": 0},
        "api_key": "test-key",
        "model": "gemini-test",
        "voice_name": "Kore",
        "max_call_duration_seconds": 1800,
        "static_token": "sec-token",
        "proxy": "",
    }
    cfg.update(over)
    return cfg


def _make_server(**cfg_over) -> RTCServer:
    server = RTCServer(_base_cfg(**cfg_over))
    # 桩掉 LiveClient：connect 成功、close 幂等
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.close = AsyncMock()
    fake_client.send_audio = AsyncMock()
    fake_client.send_interrupt = AsyncMock()
    fake_client.bytes_in = 0
    fake_client.closed = False

    import rtc.server as srv

    orig_live = srv.LiveClient

    def _stub_live(cfg, cb):
        return fake_client

    srv.LiveClient = _stub_live
    server._stub_restore = lambda: setattr(srv, "LiveClient", orig_live)
    server._fake_client = fake_client
    return server


@pytest.fixture
def server_factory():
    """同步工厂：返回 (server, cleanup)。测试自起自停。"""
    made = []

    def _make(**cfg_over):
        s = _make_server(**cfg_over)
        made.append(s)
        return s

    yield _make
    for s in made:
        s._stub_restore()


def _hello(token: str = "sec-token", version: int = 1):
    return json.dumps({"type": "hello", "version": version,
                       "auth": {"token": token}})


async def _start_and_connect(server: RTCServer) -> tuple[aiohttp.ClientSession, str]:
    """起 server（随机端口）并连上 WS。返回 (session, ws_url)。"""
    await server.start()
    # TCPSite 用 port=0 时实际端口在 _site._server.sockets
    sockets = server._site._server.sockets
    port = sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    session = aiohttp.ClientSession()
    return session, base


class TestHandshake:
    @pytest.mark.anyio
    async def test_hello_welcome_roundtrip(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with session.ws_connect(f"{base}/ws") as ws:
                await ws.send_str(_hello())
                m1 = await ws.receive_json()
                assert m1["type"] == "state" and m1["state"] == "connecting"
                m2 = await ws.receive_json()
                assert m2["type"] == "welcome"
                assert m2["version"] == 1
                assert m2["max_seconds"] == 1800
                assert m2["voice"] == "Kore"
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_no_hello_rejected(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with session.ws_connect(f"{base}/ws") as ws:
                await ws.send_str(json.dumps({"type": "audio"}))
                msg = await ws.receive(timeout=3)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    assert data["type"] == "error" and data["code"] == "hello_expected"
                else:
                    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_future_version_rejected(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with session.ws_connect(f"{base}/ws") as ws:
                await ws.send_str(_hello(version=99))
                data = await ws.receive_json()
                assert data["type"] == "error" and data["code"] == "version"
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_bad_token_auth_failed(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with session.ws_connect(f"{base}/ws") as ws:
                await ws.send_str(_hello(token="wrong"))
                data = await ws.receive_json()
                assert data["type"] == "auth_failed"
        finally:
            await session.close()
            await server.stop()


class TestCallFlow:
    async def _open_call(self, session, base):
        ws = await session.ws_connect(f"{base}/ws")
        await ws.send_str(_hello())
        await ws.receive_json()  # state connecting
        await ws.receive_json()  # welcome
        return ws

    @pytest.mark.anyio
    async def test_audio_relay_and_mute(self, server_factory):
        server = server_factory()
        fake = server._fake_client
        session, base = await _start_and_connect(server)
        try:
            async with await self._open_call(session, base) as ws:
                pcm = b"\x01\x02\x03\x04"
                await ws.send_bytes(pcm)
                await asyncio.sleep(0.05)
                assert fake.send_audio.await_count == 1
                # 静音后不再上行
                await ws.send_str(json.dumps({"action": "mute"}))
                await asyncio.sleep(0.05)
                await ws.send_bytes(pcm)
                await asyncio.sleep(0.05)
                assert fake.send_audio.await_count == 1
                # 取消静音恢复
                await ws.send_str(json.dumps({"action": "unmute"}))
                await asyncio.sleep(0.05)
                await ws.send_bytes(pcm)
                await asyncio.sleep(0.05)
                assert fake.send_audio.await_count == 2
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_interrupt_forwarded(self, server_factory):
        server = server_factory()
        fake = server._fake_client
        session, base = await _start_and_connect(server)
        try:
            async with await self._open_call(session, base) as ws:
                await ws.send_str(json.dumps({"action": "interrupt"}))
                await asyncio.sleep(0.05)
                assert fake.send_interrupt.await_count == 1
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_hangup_sends_bye(self, server_factory):
        server = server_factory()
        fake = server._fake_client
        session, base = await _start_and_connect(server)
        try:
            async with await self._open_call(session, base) as ws:
                await ws.send_str(json.dumps({"action": "hangup"}))
                data = await ws.receive_json()
                assert data["type"] == "bye" and data["reason"] == "peer_hangup"
                assert fake.close.await_count == 1
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_second_call_rejected_while_busy(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with await self._open_call(session, base) as ws1:
                async with session.ws_connect(f"{base}/ws") as ws2:
                    await ws2.send_str(_hello())
                    data = await ws2.receive_json()
                    assert data["type"] == "error" and data["code"] == "busy"
        finally:
            await session.close()
            await server.stop()


class TestLimit:
    @pytest.mark.anyio
    async def test_time_limit_bye(self, server_factory):
        server = server_factory(max_call_duration_seconds=1)
        session, base = await _start_and_connect(server)
        try:
            async with session.ws_connect(f"{base}/ws") as ws:
                await ws.send_str(_hello())
                await ws.receive_json()
                await ws.receive_json()
                # 等上限到点（1s + 回调）
                data = await asyncio.wait_for(ws.receive_json(), timeout=5)
                assert data["type"] == "bye" and data["reason"] == "limit"
                assert server._fake_client.close.await_count == 1
        finally:
            await session.close()
            await server.stop()


class TestStatic:
    @pytest.mark.anyio
    async def test_healthz(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with session.get(f"{base}/healthz") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True and "protocol_version" in data
        finally:
            await session.close()
            await server.stop()

    @pytest.mark.anyio
    async def test_index_served(self, server_factory):
        server = server_factory()
        session, base = await _start_and_connect(server)
        try:
            async with session.get(f"{base}/") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Nora" in text
        finally:
            await session.close()
            await server.stop()
