from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


PRIVATE_CHAT_TYPE = "private"

# 单主人实例的恒定默认作用域。所有平台、私聊/群聊默认归入同一记忆作用域，
# 让 Nora 在任何地点都是同一个连续主体。
DEFAULT_OWNER_ID = "owner:default"
DEFAULT_RELATIONSHIP_ID = "relationship:owner:default"

# 可选的主人解析回调： (platform, actor_user_id, chat_type) -> bool。
# 由 controller 在启动时通过 set_owner_resolver 注入真实实现（core/owner_registry），
# 让本模块保持零业务依赖、可独立测试。
OwnerResolver = Callable[[str, str, str], bool]
_owner_resolver: Optional[OwnerResolver] = None


def set_owner_resolver(resolver: Optional[OwnerResolver]) -> None:
    """注入主人识别回调。传 None 可清除（测试用）。"""
    global _owner_resolver
    _owner_resolver = resolver


def _resolve_is_owner(platform: str, actor_user_id: str, chat_type: str) -> bool:
    """调用已注入的解析器判断说话人是否主人；未注入时默认 False。"""
    resolver = _owner_resolver
    if resolver is None:
        return False
    try:
        return bool(resolver(platform, actor_user_id, chat_type))
    except Exception:
        return False


def make_place_scope_id(platform: str, platform_chat_id: str) -> str:
    """来源地点标识：哪个平台的哪个会话窗口。"""
    return f"{platform}:{platform_chat_id}"


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Canonical identity for one platform conversation.

    五层身份模型（见 plans/nora-cross-platform-relay.md）：
      - owner_id            : 主人是谁（自我层）
      - relationship_id     : 你和 Nora 的主关系（关系层）
      - memory_scope_id     : 共享上下文主键，历史/RAG/媒体都按它检索（记忆作用域层）
      - place_scope_id      : 话从哪来，可回查（地点层）
      - actor_display_name  : 谁在说话（说话人层）
      - is_owner            : 当前说话人是否为主人

    保留字段：runtime_key（发送/typing/任务）、platform_chat_id（实际投递）、
    storage_id（兼容期 fallback，不再作为最终共享上下文主键）。
    """

    platform: str
    platform_chat_id: str
    chat_type: str
    actor_user_id: str
    storage_id: str
    runtime_key: str
    owner_id: str = DEFAULT_OWNER_ID
    relationship_id: str = DEFAULT_RELATIONSHIP_ID
    memory_scope_id: str = DEFAULT_RELATIONSHIP_ID
    place_scope_id: str = ""
    actor_display_name: str = ""
    is_owner: bool = False


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
    actor_display_name = str(context.get("user_name") or "").strip()
    is_owner = _resolve_is_owner(platform, actor_user_id, chat_type)
    return ConversationIdentity(
        platform=platform,
        platform_chat_id=platform_chat_id,
        chat_type=chat_type,
        actor_user_id=actor_user_id,
        storage_id=storage_id,
        runtime_key=f"{platform}:{platform_chat_id}",
        owner_id=DEFAULT_OWNER_ID,
        relationship_id=DEFAULT_RELATIONSHIP_ID,
        memory_scope_id=DEFAULT_RELATIONSHIP_ID,
        place_scope_id=make_place_scope_id(platform, platform_chat_id),
        actor_display_name=actor_display_name,
        is_owner=is_owner,
    )


def build_identity_from_parts(
    *,
    chat_id: str,
    user_id: Optional[str] = None,
    chat_type: str = PRIVATE_CHAT_TYPE,
    platform: str = "telegram",
    user_name: Optional[str] = None,
) -> ConversationIdentity:
    context = {
        "platform": platform,
        "chat_id": chat_id,
        "user_id": user_id or chat_id,
        "chat_type": chat_type,
    }
    if user_name:
        context["user_name"] = user_name
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

    # 作用域字段：优先用持久化值，缺失则补默认（兼容旧 target）。
    owner_id = str(target.get("owner_id") or DEFAULT_OWNER_ID)
    relationship_id = str(target.get("relationship_id") or DEFAULT_RELATIONSHIP_ID)
    memory_scope_id = str(target.get("memory_scope_id") or relationship_id)
    place_scope_id = str(target.get("place_scope_id") or "").strip() or make_place_scope_id(
        platform, platform_chat_id
    )
    actor_display_name = str(
        target.get("actor_display_name") or target.get("user_name") or ""
    ).strip()
    # is_owner：持久化里有就用持久化（避免恢复 target 时触发自动绑定副作用），否则解析。
    if "is_owner" in target:
        is_owner = bool(target.get("is_owner"))
    else:
        is_owner = _resolve_is_owner(platform, actor_user_id, chat_type)

    return ConversationIdentity(
        platform=platform,
        platform_chat_id=platform_chat_id,
        chat_type=chat_type,
        actor_user_id=actor_user_id,
        storage_id=storage_id,
        runtime_key=f"{platform}:{platform_chat_id}",
        owner_id=owner_id,
        relationship_id=relationship_id,
        memory_scope_id=memory_scope_id,
        place_scope_id=place_scope_id,
        actor_display_name=actor_display_name,
        is_owner=is_owner,
    )


def conversation_target_dict(identity: ConversationIdentity) -> dict[str, Any]:
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
        "owner_id": identity.owner_id,
        "relationship_id": identity.relationship_id,
        "memory_scope_id": identity.memory_scope_id,
        "place_scope_id": identity.place_scope_id,
        "actor_display_name": identity.actor_display_name,
        "is_owner": identity.is_owner,
    }


def runtime_key_for(platform: str, chat_id: str) -> str:
    return f"{platform}:{chat_id}"
