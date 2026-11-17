from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

import config

from adapters.model_catalog import fetch_provider_models, provider_config, provider_names

_COMMAND = re.compile(r"(?m)^\s*/([a-z_]+)\b([^\n]*)")
_OWNER_COMMANDS = {"clear", "stop", "undo", "retry", "override", "reload_models", "debug", "custom_scope",
                   "set_stream", "regenerate_proactive", "end_segment", "model", "effort", "nora_prefs",
                   "model_pick", "model_list", "model_page", "model_set", "model_clear", "effort_pick"}

# 可清除的可选别名；与 telegram 的 /model 子菜单保持一致。
_CLEARABLE_ALIASES = ("video", "security", "fast-image", "draw", "draw_desc")
_PAGE_SIZE = 6
_LABEL_LIMIT = 30
_CATALOG_TTL = 180


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    user_id: str
    chat_id: str
    chat_type: str
    alias: str
    provider: str
    model: str = ""  # model_set 用的单项；分页目录项为空。
    models: tuple[str, ...] = ()  # 分页目录项的完整模型列表。
    offset: int = 0
    expires_at: float = 0.0


class CatalogStore:
    """/model 菜单的服务端目录缓存：模型列表（供翻页）与待选模型项（供 set）。

    仿 InteractionStore：token 索引、按归属校验、180 秒过期；条目可重复读
    （翻页按钮可连点），由 InteractionStore 的 generation 保证旧菜单按钮失效。
    """

    def __init__(self, *, clock: Any = time.monotonic, max_size: int = 256) -> None:
        self.clock = clock
        self.max_size = max_size
        self.entries: OrderedDict[str, CatalogEntry] = OrderedDict()

    def put(self, entry: CatalogEntry) -> str:
        token = secrets.token_urlsafe(16)
        self.entries[token] = entry
        while len(self.entries) > self.max_size:
            self.entries.popitem(last=False)
        return token

    def get(self, token: str, user_id: str, chat_id: str, chat_type: str) -> CatalogEntry | None:
        entry = self.entries.get(token)
        if not entry or (entry.user_id, entry.chat_id, entry.chat_type) != (user_id, chat_id, chat_type):
            return None
        if entry.expires_at <= self.clock():
            self.entries.pop(token, None)
            return None
        self.entries.move_to_end(token)
        return entry

    def set_offset(self, token: str, offset: int) -> CatalogEntry | None:
        entry = self.entries.get(token)
        if not entry:
            return None
        updated = replace(entry, offset=max(0, offset))
        self.entries[token] = updated
        self.entries.move_to_end(token)
        return updated


def _short(text: str, limit: int = _LABEL_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _llm_cfg() -> dict:
    cfg = config.get_config() or {}
    return cfg.get("llm", {}) or {}


async def _model_alias_menu(reply: Any, menu: Any) -> None:
    models = _llm_cfg().get("models", {}) or {}
    choices = []
    for alias in config.LLM_MODEL_ALIASES:
        current = models.get(alias, "")
        choices.append((f"{alias}: {_short(current) if current else '未设置'}", f"/model_pick {alias}"))
    choices.append(("取消", "/qqbot_cancel"))
    await reply("**🧠 模型切换**\n点击别名选择 Provider 与模型，保存后立即生效并重载。",
                keyboard=menu("model", choices, columns=3))


async def _model_provider_menu(reply: Any, menu: Any, alias: str) -> None:
    if alias not in config.LLM_MODEL_ALIASES:
        await reply("❌ 模型别名无效。别名：" + ", ".join(config.LLM_MODEL_ALIASES))
        return
    cfg = config.get_config() or {}
    current = (_llm_cfg().get("models", {}) or {}).get(alias, "")
    choices = [(name, f"/model_list {alias} {name}") for name in provider_names(cfg)]
    if alias in _CLEARABLE_ALIASES:
        choices.append(("🗑 清除（不使用）", f"/model_clear {alias}"))
    choices.append(("⬅ 返回", "/model"))
    choices.append(("取消", "/qqbot_cancel"))
    await reply(f"**📥 {alias} 的 Provider**\n当前模型：{current or '未设置'}\n选择 Provider 以拉取模型列表。",
                keyboard=menu("model", choices, columns=2))


def _model_page_render(store: CatalogStore, menu: Any, page_token: str) -> tuple[str, dict]:
    entry = store.entries[page_token]
    models = entry.models
    offset = min(entry.offset, max(0, len(models) - 1))
    options = []
    for name in models[offset:offset + _PAGE_SIZE]:
        option_token = store.put(replace(entry, model=name, models=()))
        options.append((_short(name), f"/model_set {entry.alias} {option_token}"))
    choices = list(options)
    if offset > 0:
        choices.append(("‹ 上一页", f"/model_page {page_token} {max(0, offset - _PAGE_SIZE)}"))
    if offset + _PAGE_SIZE < len(models):
        choices.append(("下一页 ›", f"/model_page {page_token} {offset + _PAGE_SIZE}"))
    choices.append(("⬅ 返回", f"/model_pick {entry.alias}"))
    choices.append(("取消", "/qqbot_cancel"))
    pages = max(1, -(-len(models) // _PAGE_SIZE))
    text = (f"**📥 {entry.alias} 模型列表**（{entry.provider}）\n"
            f"共 {len(models)} 个，第 {offset // _PAGE_SIZE + 1}/{pages} 页；点击切换并重载。")
    return text, menu("model", choices, columns=2 if len(options) > 3 else 1)


async def _model_list(adapter: Any, identity: Any, reply: Any, menu: Any, alias: str, provider: str) -> None:
    if alias not in config.LLM_MODEL_ALIASES:
        await reply("❌ 模型别名无效。")
        return
    cfg = config.get_config() or {}
    # SDK 的模型列表接口是同步调用，丢线程池避免阻塞事件分链。
    models = await asyncio.to_thread(fetch_provider_models, provider, provider_config(cfg, provider))
    if not models:
        await reply(f"⚠️ 未能从 {provider} 拉取到模型列表（未配置 API key 或接口失败）。\n"
                    f"也可直接发送 /model {alias} <model-name> 手动输入。",
                    keyboard=menu("model", [("⬅ 返回", f"/model_pick {alias}"), ("取消", "/qqbot_cancel")]))
        return
    store: CatalogStore = adapter.model_catalogs
    page_token = store.put(CatalogEntry(user_id=identity.actor_user_id, chat_id=identity.platform_chat_id,
                                        chat_type=identity.chat_type, alias=alias, provider=provider,
                                        models=tuple(models), expires_at=store.clock() + _CATALOG_TTL))
    text, keyboard = _model_page_render(store, menu, page_token)
    await reply(text, keyboard=keyboard)


async def _effort_level_menu(reply: Any, menu: Any, alias: str) -> None:
    if alias not in config.LLM_MODEL_ALIASES:
        await reply("❌ 模型别名无效。")
        return
    current = config.get_llm_effort(alias)
    choices = [(level, f"/effort {alias} {level}") for level in config.EFFORT_LEVELS]
    choices.append(("default（跟随全局）", f"/effort {alias} default"))
    choices.append(("⬅ 返回", "/effort"))
    await reply(f"**🎚 {alias} 推理强度**\n当前：{current or '未设置（跟随全局）'}\n选择后立即保存。",
                keyboard=menu("effort", choices, columns=2))


async def before_command(adapter: Any, context: dict[str, Any], identity: Any) -> bool:
    text = str(context.get("text") or "")
    matches = list(_COMMAND.finditer(text))
    if not matches:
        return False
    kwargs = {"chat_type": identity.chat_type, "delivery_context": context.get("delivery_context")}

    async def reply(content: str, *, keyboard: dict | None = None) -> None:
        await adapter.send_message(identity.platform_chat_id, content, keyboard=keyboard, **kwargs)

    if context.get("qqbot_has_nested_elements"):
        await reply("引用或聊天记录中的内容不会当作命令执行，请单独发送命令。")
        return True
    if any(match.group(1) in _OWNER_COMMANDS for match in matches) and not identity.is_owner:
        await reply("❌ 该命令仅限已绑定的主人操作。")
        return True
    # Core 按固定优先级查多行命令；QQ 禁止混合多个命令，避免确认了 A 却执行 B。
    if len(matches) != 1 or len(context.get("group_message_batch") or []) > 1:
        await reply("请单独发送一条命令。")
        return True
    match = matches[0]
    command, args = match.group(1), match.group(2).strip()
    grant = str(context.pop("_qqbot_command_grant", "") or "")
    confirmed = adapter.interactions.consume_grant(
        grant, identity.actor_user_id, identity.platform_chat_id, identity.chat_type, text,
    ) if grant else False

    def menu(name: str, choices: list[tuple[str, str]], columns: int = 1) -> dict:
        return adapter.interactions.menu(identity.actor_user_id, identity.platform_chat_id,
                                         identity.chat_type, name, choices, columns=columns)

    async def reload_models() -> None:
        controller = getattr(adapter._message_handler, "__self__", None)
        if controller:
            await controller._cmd_reload_models(identity.runtime_key)

    if command == "clear":
        if confirmed and text == "/clear":
            return False
        await reply("⚠️ 将永久清空跨平台共享的聊天记录，无法恢复。是否确认？", keyboard=menu("clear", [("确认永久清空", "/clear"), ("取消", "/qqbot_cancel")]))
        return True
    if command == "qqbot_cancel":
        await reply("已取消。")
        return True
    if command == "set_stream" and not args:
        await reply("选择当前会话输出模式。", keyboard=menu("stream", [("一次性输出", "/set_stream on"), ("流式输出", "/set_stream off")]))
        return True
    if command == "model":
        parts = args.split(maxsplit=1)
        if not parts:
            await _model_alias_menu(reply, menu)
            return True
        if len(parts) == 2:
            try:
                config.set_llm_model_by_alias(*parts)
                await reload_models()
            except ValueError:
                await reply("❌ 模型别名或名称无效。")
            return True
        await _model_provider_menu(reply, menu, parts[0])
        return True
    if command == "model_pick":
        await _model_provider_menu(reply, menu, args.split(maxsplit=1)[0] if args else "")
        return True
    if command == "model_list":
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            await reply("用法：/model_list <alias> <provider>（请从 /model 菜单进入。）")
            return True
        await _model_list(adapter, identity, reply, menu, parts[0], parts[1])
        return True
    if command == "model_page":
        parts = args.split()
        entry = (adapter.model_catalogs.get(parts[0], identity.actor_user_id, identity.platform_chat_id,
                                            identity.chat_type)
                 if len(parts) == 2 and parts[1].isdigit() else None)
        if not entry or not entry.models:
            await reply("⚠️ 目录已过期，请重新发送 /model。")
            return True
        adapter.model_catalogs.set_offset(parts[0], int(parts[1]))
        text_out, keyboard = _model_page_render(adapter.model_catalogs, menu, parts[0])
        await reply(text_out, keyboard=keyboard)
        return True
    if command == "model_set":
        parts = args.split(maxsplit=1)
        entry = (adapter.model_catalogs.get(parts[1], identity.actor_user_id, identity.platform_chat_id,
                                            identity.chat_type)
                 if len(parts) == 2 else None)
        if not entry or entry.alias != parts[0] or not entry.model:
            await reply("⚠️ 选项已过期，请重新发送 /model。")
            return True
        cfg = config.get_config() or {}
        llm_cfg = cfg.setdefault("llm", {})
        llm_cfg.setdefault("models", {})[entry.alias] = entry.model
        llm_cfg.setdefault("model_providers", {})[entry.alias] = entry.provider
        config.save_config(cfg)
        await reload_models()
        await reply(f"✅ 已更新 {entry.alias} 模型为: {entry.model}（provider: {entry.provider}）")
        return True
    if command == "model_clear":
        alias = args.split(maxsplit=1)[0] if args else ""
        if alias not in _CLEARABLE_ALIASES:
            await reply("❌ 该别名不支持清除配置。")
            return True
        cfg = config.get_config() or {}
        llm_cfg = cfg.setdefault("llm", {})
        llm_cfg.setdefault("models", {}).pop(alias, None)
        llm_cfg.setdefault("model_providers", {}).pop(alias, None)
        config.save_config(cfg)
        await reply(f"✅ 已清除 {alias} 模型配置。")
        return True
    if command == "effort":
        parts = args.split()
        if not parts:
            lines = ["**🎚 推理强度**", "点击别名选择档位；未设置的别名跟随全局 llm.effort。"]
            choices = []
            for alias in config.LLM_MODEL_ALIASES:
                current = config.get_llm_effort(alias)
                choices.append((f"{alias}: {_short(current) if current else '未设置'}", f"/effort_pick {alias}"))
            choices.append(("取消", "/qqbot_cancel"))
            await reply("\n".join(lines), keyboard=menu("effort", choices, columns=3))
            return True
        if len(parts) == 1:
            await _effort_level_menu(reply, menu, parts[0])
            return True
        if parts[0] not in config.LLM_MODEL_ALIASES or parts[1] not in {*config.EFFORT_LEVELS, "default"}:
            await reply("用法：/effort <alias> <level>\n档位：" + ", ".join(config.EFFORT_LEVELS) + ", default")
            return True
        config.set_llm_effort_by_alias(parts[0], None if parts[1] == "default" else parts[1])
        await reply("✅ 推理强度已保存。")
        return True
    if command == "effort_pick":
        await _effort_level_menu(reply, menu, args.split(maxsplit=1)[0] if args else "")
        return True
    if command == "nora_prefs":
        prefs = config.get_nora_preferences()
        parts = args.split()
        if not parts:
            await reply("Nora 偏好：\n" + "\n".join(f"{key}: {value}" for key, value in prefs.items()) + "\n用法：/nora_prefs <key> <value>", keyboard=menu("prefs", [("精简回复", "/nora_prefs verbosity_preference 0.2"), ("详细回复", "/nora_prefs verbosity_preference 0.8")]))
            return True
        try:
            if len(parts) != 2 or parts[0] not in prefs:
                raise ValueError
            value = float(parts[1])
            limit = 30 if parts[0] == "pause_between_splits_seconds" else 100 if parts[0] == "followup_skip_end_after" else 1
            if not 0 <= value <= limit:
                raise ValueError
            if parts[0] == "followup_skip_end_after":
                if not value.is_integer() or value < 1:
                    raise ValueError
                value = int(value)
            config.set_nora_preferences({parts[0]: value})
            await reply("✅ Nora 偏好已保存。")
        except ValueError:
            await reply("❌ 偏好名称或数值无效。")
        return True
    return False
