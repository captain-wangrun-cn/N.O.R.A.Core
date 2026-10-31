from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from adapters.delivery import DeliveryCancelled, DeliveryContext, DeliveryError, DeliveryReceipt
from adapters.message_controls import parse_message_controls

from .client import QQBotAPIError
from .constants import PASSIVE_EXPIRED_CODES
from .media import scene_endpoint

logger = logging.getLogger(__name__)
_MEDIA = re.compile(r"\[(image|video|voice|audio|file|doc):\s*([^\]]+)\]")


def text_chunks(text: str, limit: int) -> list[str]:
    limit = max(64, limit)
    chunks = []
    while len(text) > limit:
        boundary = text.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(text[:boundary])
        text = text[boundary:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


class QQBotSender:
    def __init__(self, client: Any, state: Any, media: Any, *, chunk_size: int = 2000) -> None:
        self.client = client
        self.state = state
        self.media = media
        self.chunk_size = max(64, chunk_size)

    async def _post(self, endpoint: str, payload: dict[str, Any], credential: Any = None) -> dict[str, Any]:
        async def perform() -> dict[str, Any]:
            for attempt in range(3):
                try:
                    return await self.client.request("POST", endpoint + "/messages", json=payload)
                except QQBotAPIError as exc:
                    if exc.status == 429 and attempt < 2:
                        await asyncio.sleep(min(10, max(0.2, exc.retry_after or 1)))
                        continue
                    raise
            raise DeliveryError("QQ send retry exhausted")

        if credential is None:
            return await perform()
        async with credential.lock:
            if credential.remaining <= 0 or self.state.clock() >= credential.expires_at:
                return await perform()
            credential.seq += 1
            credential.remaining -= 1
            payload[credential.kind] = credential.value
            payload["msg_seq"] = credential.seq
            try:
                return await perform()
            except QQBotAPIError as exc:
                if exc.code not in PASSIVE_EXPIRED_CODES:
                    raise
                credential.remaining = 0
                payload.pop(credential.kind, None)
                payload.pop("msg_seq", None)
                return await perform()

    async def send(
        self, chat_id: str, text: str, *, chat_type: str,
        delivery_context: DeliveryContext | None = None,
        reply_to_message_id: str = "", keyboard: dict | None = None,
        markdown: bool = True, parse_media: bool = True,
    ) -> list[str]:
        endpoint = scene_endpoint(chat_type, chat_id)
        snapshot = (delivery_context or DeliveryContext.proactive("qqbot", chat_id, chat_type)).for_target("qqbot", chat_id, chat_type)
        controls = parse_message_controls(text)
        if controls.mention_user_ids:
            logger.info("[qqbot] outgoing member @ unsupported; control removed")
        visible = controls.visible_text()
        reply_id = controls.reply_message_id or reply_to_message_id
        reference = self.state.reference_for(chat_type, chat_id, reply_id) if reply_id else None
        items: list[tuple[str, str]] = []
        cursor = 0
        if parse_media:
            for match in _MEDIA.finditer(visible):
                items.extend(("text", chunk) for chunk in text_chunks(visible[cursor:match.start()], self.chunk_size))
                items.append((match.group(1), match.group(2).strip()))
                cursor = match.end()
        items.extend(("text", chunk) for chunk in text_chunks(visible[cursor:], self.chunk_size))
        sent_ids: list[str] = []
        sent_texts: list[str] = []
        stage, kind = "prepare", "text"
        try:
            for kind, content in items:
                stage = "prepare"
                payload: dict[str, Any]
                if kind == "text":
                    payload = {"msg_type": 2, "markdown": {"content": content}} if markdown else {"msg_type": 0, "content": content}
                    if keyboard and not sent_ids:
                        payload["keyboard"] = keyboard
                else:
                    stage = "upload"
                    file_info = await self.media.upload(content, kind, chat_type, chat_id)
                    payload = {"msg_type": 7, "media": {"file_info": file_info}}
                if reference and reference.index and not sent_ids:
                    payload["message_reference"] = {"message_id": reference.index}
                credential = self.state.credential_for(chat_type, chat_id, snapshot.source_message_ids) if snapshot.mode == "response" else None
                stage = "message"
                result = await self._post(endpoint, payload, credential)
                message_id = str(result.get("id") or "")
                if not message_id:
                    raise DeliveryError("QQ send returned no message ID", uncertain=True)
                sent_ids.append(message_id)
                sent_texts.append(content if kind == "text" else f"[{kind}: {content}]")
                index = str((result.get("ext_info") or {}).get("ref_idx") or "")
                self.state.remember_reference(chat_type, chat_id, message_id, index, is_bot=True)
            return DeliveryReceipt(sent_ids, "\n".join(sent_texts))
        except asyncio.CancelledError:
            raise DeliveryCancelled(sent_ids=sent_ids, sent_texts=sent_texts) from None
        except Exception as exc:
            uncertain = (isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))
                         or (isinstance(exc, QQBotAPIError) and (exc.status >= 500 or 200 <= exc.status < 300))
                         or (isinstance(exc, DeliveryError) and exc.uncertain))
            logger.warning("[qqbot] send failed: stage=%s kind=%s sent=%d uncertain=%s error=%s http=%s code=%s", stage, kind, len(sent_ids), uncertain, type(exc).__name__, exc.status if isinstance(exc, QQBotAPIError) else "-", exc.code if isinstance(exc, QQBotAPIError) else "-")
            raise DeliveryError(
                str(exc) if isinstance(exc, (QQBotAPIError, DeliveryError, ValueError)) else "QQ send failed",
                sent_ids=sent_ids, sent_texts=sent_texts, uncertain=uncertain,
            ) from None

    async def delete(self, chat_id: str, message_id: str, *, chat_type: str) -> bool:
        ref = self.state.reference_for(chat_type, chat_id, message_id)
        if not ref or not ref.is_bot or self.state.clock() - ref.timestamp >= 120:
            return False
        from urllib.parse import quote
        try:
            await self.client.request("DELETE", scene_endpoint(chat_type, chat_id) + "/messages/" + quote(message_id, safe=""))
            return True
        except (QQBotAPIError, aiohttp.ClientError, asyncio.TimeoutError):
            return False
