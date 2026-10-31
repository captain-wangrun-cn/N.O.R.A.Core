from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from .client import QQBotAPIError
from .message import attachment_type


class PublicResolver(aiohttp.abc.AbstractResolver):
    """下载连接只解析公网地址，连接使用同次解析结果，避免 DNS 重绑定。"""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        resolved = []
        for af, _, proto, _, address in records:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global:
                raise ValueError("Private media address rejected")
            resolved.append({"hostname": host, "host": address[0], "port": port,
                             "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST})
        if not resolved:
            raise ValueError("Media host has no public address")
        return resolved

    async def close(self) -> None:
        return None


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in {None, 443}:
        raise ValueError("Media URL must be public HTTPS")
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ValueError("Private media address rejected")
    if parts.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Loopback media host rejected")
    return url


def scene_endpoint(chat_type: str, chat_id: str) -> str:
    from urllib.parse import quote
    if chat_type not in {"private", "group"} or not chat_id:
        raise ValueError("QQ send requires an explicit private/group scene")
    return f"/v2/{'groups' if chat_type == 'group' else 'users'}/{quote(chat_id, safe='')}"


class QQBotMedia:
    def __init__(self, client: Any, workspace_root: Path, *, max_bytes: int = 104857600, session: Any = None) -> None:
        self.client = client
        self.root = workspace_root.resolve()
        self.max_bytes = max_bytes
        self.session = session
        self._owns_session = session is None
        self.cache: OrderedDict[tuple, tuple[str, float]] = OrderedDict()
        self._upload_locks: dict[tuple, asyncio.Lock] = {}
        self._upload_users: dict[tuple, int] = {}

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False),
                timeout=aiohttp.ClientTimeout(total=60), trust_env=False,
            )

    async def close(self) -> None:
        if self._owns_session and self.session is not None:
            await self.session.close()

    async def download(self, attachment: dict[str, Any], chat_type: str, chat_id: str) -> str:
        scene_endpoint(chat_type, chat_id)
        await self.start()
        media_type = attachment_type(attachment)
        url = str(attachment.get("voice_wav_url") or attachment.get("url") or "")
        validate_url(url)
        declared_size = int(attachment.get("size") or 0)
        if declared_size > self.max_bytes:
            raise ValueError("Media exceeds size limit")
        suffix = Path(urlsplit(url).path).suffix.lower()
        if attachment.get("voice_wav_url"):
            suffix = ".wav"
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = {"image": ".jpg", "video": ".mp4", "audio": ".wav"}.get(media_type, ".bin")
        # 不用远端 filename 或 opaque ID 拼未净化路径。
        scene_name = hashlib.sha256(chat_id.encode()).hexdigest()[:24]
        target_dir = self.root / "data" / "media" / "qqbot" / chat_type / scene_name / media_type
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / (uuid.uuid4().hex + suffix)
        try:
            for _ in range(4):
                async with self.session.get(validate_url(url), allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        validate_url(url)
                        continue
                    if response.status != 200:
                        raise QQBotAPIError(response.status)
                    if response.content_length and response.content_length > self.max_bytes:
                        raise ValueError("Media exceeds size limit")
                    size = 0
                    with path.open("xb") as output:
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise ValueError("Media exceeds size limit")
                            await asyncio.to_thread(output.write, chunk)
                    return path.relative_to(self.root).as_posix()
            raise ValueError("Too many media redirects")
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def local_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        path = (self.root / path if not path.is_absolute() else path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Outgoing media must be a workspace file")
        relative = path.relative_to(self.root)
        names = {part.lower() for part in relative.parts}
        if names & {"secret.md", "custom.md", "secret.key", "config.yml", "config.yaml", ".env", ".env.local"} or (
            "appearance" in names and "refs" in names
        ):
            raise ValueError("Protected file cannot be sent as generic media")
        if path.stat().st_size > self.max_bytes:
            raise ValueError("Media exceeds size limit")
        return path

    @staticmethod
    def _hashes(path: Path) -> dict[str, str]:
        md5 = hashlib.md5()
        sha1 = hashlib.sha1()
        prefix = hashlib.md5()
        remaining = 10002432
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                md5.update(chunk)
                sha1.update(chunk)
                prefix.update(chunk[:remaining])
                remaining = max(0, remaining - len(chunk))
                size += len(chunk)
        return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": prefix.hexdigest(), "file_size": str(size)}

    async def upload(self, value: str, kind: str, chat_type: str, chat_id: str) -> str:
        endpoint = scene_endpoint(chat_type, chat_id)
        file_type = {"image": 1, "video": 2, "voice": 3, "audio": 3, "file": 4, "doc": 4}[kind]
        if value.startswith("https://"):
            validate_url(value)
            result = await self.client.request("POST", endpoint + "/files", json={
                "file_type": file_type, "url": value, "srv_send_msg": False,
            })
            if not result.get("file_info"):
                raise ValueError("QQ upload returned no file_info")
            return str(result["file_info"])
        path = self.local_path(value)
        stat = path.stat()
        key = (chat_type, chat_id, file_type, str(path), stat.st_mtime_ns, stat.st_size)
        lock = self._upload_locks.setdefault(key, asyncio.Lock())
        self._upload_users[key] = self._upload_users.get(key, 0) + 1
        try:
            async with lock:
                cached = self.cache.get(key)
                if cached and time.monotonic() < cached[1]:
                    return cached[0]
                hashes = await asyncio.to_thread(self._hashes, path)
                if int(hashes["file_size"]) != stat.st_size:
                    raise ValueError("Media changed during hashing")
                file_name = path.name
                # .oga 与 .ogg 同容器；仅更正上传文件名，不伪装转码或降级成文件。
                if file_type == 3 and path.suffix.lower() == ".oga":
                    file_name = path.stem + ".ogg"
                prepared = await self.client.request("POST", endpoint + "/upload_prepare", json={
                    "file_type": file_type, "file_name": file_name, **hashes,
                })
                upload_id = str(prepared.get("upload_id") or "")
                parts = prepared.get("parts") or []
                if not upload_id or not isinstance(parts, list) or not parts:
                    raise ValueError("QQ prepare returned no upload parts")
                # 文档示例从 0 起，实际服务可从 1 起；先校验全部编号，确认沿用平台原值。
                first_index = int(parts[0].get("index", -1))
                if first_index not in {0, 1} or any(
                    int(part.get("index", -1)) != first_index + position
                    or not 0 < int(part.get("block_size") or prepared.get("block_size") or 0) <= self.max_bytes
                    for position, part in enumerate(parts)
                ):
                    raise ValueError("Invalid upload part layout")
                await self.start()
                offset = 0
                with path.open("rb") as source:
                    for part in parts:
                        part_index = int(part["index"])
                        block_size = int(part.get("block_size") or prepared.get("block_size") or 0)
                        chunk = await asyncio.to_thread(source.read, block_size)
                        if not chunk:
                            raise ValueError("Upload parts exceed file length")
                        url = validate_url(str(part.get("presigned_url") or ""))
                        # PUT 同分片幂等；有限重试，绝不向预签名主机传 QQ Authorization。
                        for attempt in range(3):
                            try:
                                async with self.session.put(url, data=chunk, allow_redirects=False) as response:
                                    if not 200 <= response.status < 300:
                                        raise QQBotAPIError(response.status)
                                break
                            except (aiohttp.ClientError, asyncio.TimeoutError, QQBotAPIError):
                                if attempt == 2:
                                    raise
                                delay = min(5, max(0, float((prepared.get("upload_config") or {}).get("retry_delay", 1))))
                                await asyncio.sleep(delay)
                        await self.client.request("POST", endpoint + "/upload_part_finish", json={
                            "upload_id": upload_id, "part_index": part_index,
                            "block_size": str(len(chunk)), "md5": hashlib.md5(chunk).hexdigest(),
                        })
                        offset += len(chunk)
                if offset != stat.st_size or path.stat().st_mtime_ns != stat.st_mtime_ns:
                    raise ValueError("Upload size mismatch or file changed")
                result = await self.client.request("POST", endpoint + "/files", json={
                    "file_type": file_type, "file_name": file_name,
                    "upload_id": upload_id, "srv_send_msg": False,
                })
                info = str(result.get("file_info") or "")
                if not info:
                    raise ValueError("QQ upload returned no file_info")
                ttl = int(result.get("ttl", 300))
                self.cache[key] = (info, float("inf") if ttl == 0 else time.monotonic() + max(0, ttl - 10))
                self.cache.move_to_end(key)
                while len(self.cache) > 128:
                    self.cache.popitem(last=False)
                return info
        finally:
            self._upload_users[key] -= 1
            if self._upload_users[key] == 0:
                self._upload_users.pop(key)
                self._upload_locks.pop(key, None)
