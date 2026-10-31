from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable


@dataclass(slots=True)
class PassiveCredential:
    kind: str
    value: str
    expires_at: float
    remaining: int
    seq: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class MessageReference:
    index: str
    message_id: str
    user_id: str = ""
    user_name: str = ""
    is_bot: bool = False
    timestamp: float = 0


class QQBotState:
    """场景隔离、有界凭据/引用/重放去重缓存，不保存 latest-inbound。"""

    def __init__(self, max_size: int = 4096, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_size = max(1, max_size)
        self.clock = clock
        self.credentials: OrderedDict[tuple[str, str, str], PassiveCredential] = OrderedDict()
        self.references: OrderedDict[tuple[str, str, str], MessageReference] = OrderedDict()
        self.seen: OrderedDict[tuple[str, str, str], float] = OrderedDict()

    def _put(self, cache: OrderedDict, key: tuple, value: object) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.max_size:
            cache.popitem(last=False)

    def is_duplicate(self, chat_type: str, chat_id: str, identifier: str) -> bool:
        if not identifier:
            return False
        key = (chat_type, chat_id, identifier)
        if key in self.seen and self.clock() - self.seen[key] < 7200:
            return True
        self._put(self.seen, key, self.clock())
        return False

    def forget_duplicate(self, chat_type: str, chat_id: str, identifier: str) -> None:
        self.seen.pop((chat_type, chat_id, identifier), None)

    def remember_credential(
        self, chat_type: str, chat_id: str, message_id: str, *,
        kind: str = "msg_id", value: str = "", ttl: float = 300,
    ) -> None:
        key = (chat_type, chat_id, message_id)
        if not message_id or key in self.credentials:
            return
        self._put(self.credentials, key, PassiveCredential(
            kind, value or message_id, self.clock() + ttl, 5 if chat_type == "group" else 4,
        ))

    def credential_for(self, chat_type: str, chat_id: str, source_ids: tuple[str, ...]) -> PassiveCredential | None:
        for message_id in reversed(source_ids):
            credential = self.credentials.get((chat_type, chat_id, message_id))
            if credential and credential.remaining > 0 and self.clock() < credential.expires_at:
                return credential
        return None

    def remember_reference(
        self, chat_type: str, chat_id: str, message_id: str, index: str, *,
        user_id: str = "", user_name: str = "", is_bot: bool = False,
    ) -> None:
        if not message_id:
            return
        ref = MessageReference(index, message_id, user_id, user_name, is_bot, self.clock())
        self._put(self.references, (chat_type, chat_id, message_id), ref)
        if index:
            self._put(self.references, (chat_type, chat_id, index), ref)

    def reference_for(self, chat_type: str, chat_id: str, message_id: str) -> MessageReference | None:
        return self.references.get((chat_type, chat_id, message_id))
