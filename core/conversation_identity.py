from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


PRIVATE_CHAT_TYPE = "private"


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Canonical identity for one platform conversation."""

    platform: str
    platform_chat_id: str
    chat_type: str
    actor_user_id: str
    storage_id: str
    runtime_key: str


def normalize_chat_type(chat_type: Optional[str]) -> str:
    value = str(chat_type or PRIVATE_CHAT_TYPE).strip().lower()
    return value or PRIVATE_CHAT_TYPE


def resolve_platform(context: Optional[Mapping[str, Any]] = None, adapter: Any = None) -> str:
    if context:
        platform = context.get("platform")
        if platform:
            return str(platform)
    platform = getattr(adapter, "platform_name", None)
    if platform:
        return str(platform)
    return "telegram"


def build_conversation_identity(
    context: Mapping[str, Any],
    adapter: Any = None,
) -> ConversationIdentity:
    platform = resolve_platform(context, adapter)
    platform_chat_id = str(context.get("chat_id", ""))
    actor_user_id = str(context.get("user_id") or platform_chat_id)
    chat_type = normalize_chat_type(context.get("chat_type"))
    storage_id = platform_chat_id if chat_type != PRIVATE_CHAT_TYPE else actor_user_id
    return ConversationIdentity(
        platform=platform,
        platform_chat_id=platform_chat_id,
        chat_type=chat_type,
        actor_user_id=actor_user_id,
        storage_id=storage_id,
        runtime_key=f"{platform}:{platform_chat_id}",
    )


def build_identity_from_parts(
    *,
    chat_id: str,
    user_id: Optional[str] = None,
    chat_type: str = PRIVATE_CHAT_TYPE,
    platform: str = "telegram",
) -> ConversationIdentity:
    context = {
        "platform": platform,
        "chat_id": chat_id,
        "user_id": user_id or chat_id,
        "chat_type": chat_type,
    }
    return build_conversation_identity(context)


def build_identity_from_target(
    target: Mapping[str, Any],
    adapter: Any = None,
) -> ConversationIdentity:
    """Build an identity from a persisted/default conversation target."""
    runtime_key = str(target.get("runtime_key") or target.get("default_chat_id") or "").strip()
    platform = str(target.get("platform") or "").strip()
    platform_chat_id = str(target.get("platform_chat_id") or target.get("chat_id") or "").strip()

    if runtime_key and ":" in runtime_key:
        runtime_platform, runtime_chat_id = runtime_key.split(":", 1)
        platform = platform or runtime_platform
        platform_chat_id = platform_chat_id or runtime_chat_id

    platform = platform or resolve_platform(adapter=adapter)
    platform_chat_id = platform_chat_id or runtime_key
    chat_type = normalize_chat_type(target.get("chat_type"))
    storage_id = str(target.get("storage_id") or "").strip()
    actor_user_id = str(
        target.get("actor_user_id")
        or target.get("user_id")
        or (storage_id if chat_type == PRIVATE_CHAT_TYPE else "")
        or platform_chat_id
    ).strip()
    storage_id = storage_id or (platform_chat_id if chat_type != PRIVATE_CHAT_TYPE else actor_user_id)

    return ConversationIdentity(
        platform=platform,
        platform_chat_id=platform_chat_id,
        chat_type=chat_type,
        actor_user_id=actor_user_id,
        storage_id=storage_id,
        runtime_key=f"{platform}:{platform_chat_id}",
    )


def conversation_target_dict(identity: ConversationIdentity) -> dict[str, str]:
    """Serialize an identity for default-target persistence and trigger handoff."""
    return {
        "platform": identity.platform,
        "platform_chat_id": identity.platform_chat_id,
        "chat_id": identity.platform_chat_id,
        "chat_type": identity.chat_type,
        "actor_user_id": identity.actor_user_id,
        "user_id": identity.actor_user_id,
        "storage_id": identity.storage_id,
        "runtime_key": identity.runtime_key,
    }


def runtime_key_for(platform: str, chat_id: str) -> str:
    return f"{platform}:{chat_id}"
