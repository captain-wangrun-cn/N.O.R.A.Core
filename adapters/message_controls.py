from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


_CONTROL_MARKER_PATTERN = re.compile(
    r"\[\s*(reply|at|poke)\s*:\s*([^\]]*)\]",
    re.IGNORECASE,
)
_CONTROL_MARKER_LIKE_PATTERN = re.compile(
    r"\[\s*(?:reply|at|poke)(?:\s*:[^\]]*)?\s*\]",
    re.IGNORECASE,
)
_VALID_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class MessageControlToken:
    type: Literal["text", "reply", "mention", "poke"]
    value: str


@dataclass(frozen=True, slots=True)
class ParsedMessageControls:
    tokens: tuple[MessageControlToken, ...]
    reply_message_id: str = ""

    @property
    def mention_user_ids(self) -> list[str]:
        return [token.value for token in self.tokens if token.type == "mention"]

    @property
    def poke_user_ids(self) -> list[str]:
        return [token.value for token in self.tokens if token.type == "poke"]

    def visible_text(self) -> str:
        return "".join(token.value for token in self.tokens if token.type == "text")


def is_valid_control_id(value: str) -> bool:
    return bool(_VALID_CONTROL_ID_PATTERN.fullmatch(str(value or "").strip()))


def format_reply_marker(message_id: str) -> str:
    value = str(message_id or "").strip()
    return f"[reply:{value}]" if is_valid_control_id(value) else ""


def format_mention_marker(user_id: str) -> str:
    value = str(user_id or "").strip()
    return f"[at:{value}]" if is_valid_control_id(value) else ""


def format_poke_marker(user_id: str) -> str:
    value = str(user_id or "").strip()
    return f"[poke:{value}]" if is_valid_control_id(value) else ""


def parse_message_controls(text: str) -> ParsedMessageControls:
    raw_text = str(text or "")
    tokens: list[MessageControlToken] = []
    cursor = 0
    reply_message_id = ""

    for match in _CONTROL_MARKER_PATTERN.finditer(raw_text):
        if match.start() > cursor:
            tokens.append(MessageControlToken("text", raw_text[cursor:match.start()]))

        kind = str(match.group(1) or "").lower()
        value = str(match.group(2) or "").strip()
        if is_valid_control_id(value):
            if kind == "reply" and not reply_message_id:
                reply_message_id = value
                tokens.append(MessageControlToken("reply", value))
            elif kind == "at":
                tokens.append(MessageControlToken("mention", value))
            elif kind == "poke":
                tokens.append(MessageControlToken("poke", value))
        cursor = match.end()

    if cursor < len(raw_text):
        tokens.append(MessageControlToken("text", raw_text[cursor:]))

    return ParsedMessageControls(tuple(tokens), reply_message_id=reply_message_id)


def strip_message_control_markers(text: str) -> str:
    """Remove valid, malformed, and legacy-looking reply/mention controls."""
    return _CONTROL_MARKER_LIKE_PATTERN.sub("", str(text or ""))
