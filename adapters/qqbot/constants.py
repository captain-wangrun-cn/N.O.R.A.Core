from __future__ import annotations

API_BASE = "https://api.bot.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
INTENTS = (1 << 25) | (1 << 26)
MESSAGE_EVENTS = {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
PASSIVE_EXPIRED_CODES = {304103, 40034005, 40034026, 40034128}
MENTION_ONLY_TEXT = "[群聊单独艾特: 用户只艾特了你，没有附加文字。请自然地回应。]"

DEFAULT_CONFIG = {
    "app_id": "",
    "app_secret": "",
    "self_id": "",
    "download_media": True,
    "text_chunk_size": 2000,
    "passive_ttl_seconds": 300,
    "request_timeout_seconds": 30,
    "max_media_bytes": 104857600,
    "cache_size": 4096,
    "event_queue_size": 1024,
}
