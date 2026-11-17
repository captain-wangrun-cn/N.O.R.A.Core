"""预设请求组装；不修改调用方的历史，不改变数据库或共享模型状态。"""

from core.preset_manager import PresetData


def prepend_preset_head(system_prompt: str, preset: PresetData | None) -> str:
    if not preset or not preset.head_prompt:
        return system_prompt
    prefix = preset.head_prompt + "\n\n---\n"
    return system_prompt if system_prompt.startswith(prefix) else prefix + system_prompt


def apply_preset_to_request(kwargs: dict, preset: PresetData) -> dict:
    request = dict(kwargs)
    request["system_prompt"] = prepend_preset_head(request.get("system_prompt") or "", preset)
    request.update(preset.parameters)
    injection = preset.depth_injection
    if injection is None or not injection.content:
        return request
    message = {"role": injection.role, "content": injection.content}
    if injection.depth == 0:
        request["request_tail"] = [*(request.get("request_tail") or []), message]
        return request
    history = list(request.get("history") or [])
    # 当前 user 也计入深度；工具输出虽有 user 的协议角色，不算对话 user。
    user_indexes = [i for i, item in enumerate(history)
                    if item.get("role") == "user" and item.get("type") not in ("tool_call", "tool_response")
                    and not str(item.get("content", "")).startswith("【Tool Output for ")]
    if request.get("user_prompt"):
        user_indexes.append(len(history))
    index = user_indexes[-injection.depth] if len(user_indexes) >= injection.depth else 0
    history.insert(index, message)
    request["history"] = history
    return request
