from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from adapters.delivery import DeliveryContext


@dataclass(frozen=True, slots=True)
class ButtonAction:
    user_id: str
    chat_id: str
    chat_type: str
    command: str
    menu: str
    generation: str
    expires_at: float


class InteractionStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_size: int = 1024) -> None:
        self.clock = clock
        self.max_size = max_size
        self.actions: OrderedDict[str, ButtonAction] = OrderedDict()
        self.generations: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
        self.acknowledged: OrderedDict[str, None] = OrderedDict()
        self.grants: OrderedDict[str, ButtonAction] = OrderedDict()

    def _trim(self, cache: OrderedDict) -> None:
        while len(cache) > self.max_size:
            cache.popitem(last=False)

    def menu(self, user_id: str, chat_id: str, chat_type: str, menu: str, choices: list[tuple[str, str]]) -> dict:
        key = (user_id, chat_id, chat_type, menu)
        generation = secrets.token_urlsafe(12)
        self.generations[key] = generation
        self.generations.move_to_end(key)
        self._trim(self.generations)
        rows = []
        for label, command in choices:
            token = secrets.token_urlsafe(24)
            self.actions[token] = ButtonAction(user_id, chat_id, chat_type, command, menu, generation, self.clock() + 180)
            rows.append({"buttons": [{
                "id": token,
                "render_data": {"label": label, "visited_label": "已选择", "style": 1},
                "action": {"type": 1, "permission": {"type": 0, "specify_user_ids": [user_id]},
                           "data": token, "click_limit": 1},
            }]})
        self._trim(self.actions)
        return {"content": {"rows": rows}}

    def consume(self, token: str, user_id: str, chat_id: str, chat_type: str) -> ButtonAction | None:
        action = self.actions.get(token)
        if not action or (action.user_id, action.chat_id, action.chat_type) != (user_id, chat_id, chat_type):
            return None
        key = (user_id, chat_id, chat_type, action.menu)
        if action.expires_at <= self.clock() or self.generations.get(key) != action.generation:
            self.actions.pop(token, None)
            return None
        self.actions.pop(token, None)
        self.generations.pop(key, None)  # 同一个菜单的其它按钮一起失效。
        return action

    def grant(self, action: ButtonAction) -> str:
        token = secrets.token_urlsafe(24)
        self.grants[token] = action
        self._trim(self.grants)
        return token

    def consume_grant(self, token: str, user_id: str, chat_id: str, chat_type: str, command: str) -> bool:
        action = self.grants.pop(token, None)
        return bool(action and action.expires_at > self.clock()
                    and (action.user_id, action.chat_id, action.chat_type, action.command)
                    == (user_id, chat_id, chat_type, command))

    def reserve_ack(self, interaction_id: str) -> bool:
        if interaction_id in self.acknowledged:
            return False
        self.acknowledged[interaction_id] = None
        self._trim(self.acknowledged)
        return True


async def handle_interaction(adapter: Any, packet: dict[str, Any]) -> None:
    data = packet.get("d") or {}
    if data.get("type") not in {11, 12}:
        return
    interaction_id = str(data.get("id") or "").removeprefix("INTERACTION_CREATE:")
    if not interaction_id or not adapter.interactions.reserve_ack(interaction_id):
        return
    scene = str(data.get("scene") or "")
    chat_type = "private" if scene == "c2c" else "group" if scene == "group" else ""
    user_id = str(data.get("user_openid") or "") if scene == "c2c" else str(data.get("group_member_openid") or "")
    chat_id = user_id if chat_type == "private" else str(data.get("group_openid") or "")
    resolved = (data.get("data") or {}).get("resolved") or {}
    token = str(resolved.get("button_data") or "")
    controller = getattr(adapter._message_handler, "__self__", None)
    context = {"platform": "qqbot", "chat_id": chat_id, "chat_type": chat_type,
               "user_id": user_id, "user_name": "未知用户", "text": ""}
    identity = controller._identity(context) if chat_type and user_id and chat_id and controller else None
    action = adapter.interactions.consume(token, user_id, chat_id, chat_type) if identity and identity.is_owner else None
    # ACK 使用 d.id；event_id 仅用于后续消息被动凭据，绝不能交换。
    await adapter.client.request("PUT", "/interactions/" + quote(interaction_id, safe=""), json={"code": 0 if action else 4})
    if not action:
        return
    outer_id = str(packet.get("id") or "")
    if outer_id:
        adapter.state.remember_credential(chat_type, chat_id, interaction_id, kind="event_id", value=outer_id, ttl=180)
    context.update({"text": action.command, "platform_message_id": interaction_id,
                    "platform_message_ids": [interaction_id],
                    "_qqbot_command_grant": adapter.interactions.grant(action),
                    "delivery_context": DeliveryContext("qqbot", chat_id, chat_type, (interaction_id,))})
    await adapter.dispatch_message(context)
