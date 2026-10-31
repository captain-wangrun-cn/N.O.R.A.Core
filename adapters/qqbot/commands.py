from __future__ import annotations

import re
from typing import Any

import config

_COMMAND = re.compile(r"(?m)^\s*/([a-z_]+)\b([^\n]*)")
_OWNER_COMMANDS = {"clear", "stop", "undo", "override", "reload_models", "debug", "custom_scope",
                   "set_stream", "regenerate_proactive", "end_segment", "model", "effort", "nora_prefs"}


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

    def menu(name: str, choices: list[tuple[str, str]]) -> dict:
        return adapter.interactions.menu(identity.actor_user_id, identity.platform_chat_id, identity.chat_type, name, choices)

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
        if len(parts) != 2:
            await reply("用法：/model <alias> <model-name>\n别名：" + ", ".join(config.LLM_MODEL_ALIASES))
            return True
        try:
            config.set_llm_model_by_alias(*parts)
            controller = getattr(adapter._message_handler, "__self__", None)
            if controller:
                await controller._cmd_reload_models(identity.runtime_key)
            else:
                await reply("✅ 模型设置已保存。")
        except ValueError:
            await reply("❌ 模型别名或名称无效。")
        return True
    if command == "effort":
        parts = args.split()
        if len(parts) != 2 or parts[0] not in config.LLM_MODEL_ALIASES or parts[1] not in {*config.EFFORT_LEVELS, "default"}:
            await reply("用法：/effort <alias> <level>\n档位：" + ", ".join(config.EFFORT_LEVELS) + ", default")
            return True
        config.set_llm_effort_by_alias(parts[0], None if parts[1] == "default" else parts[1])
        await reply("✅ 推理强度已保存。")
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
