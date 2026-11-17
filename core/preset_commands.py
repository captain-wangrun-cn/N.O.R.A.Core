"""聊天与 CLI 共用预设操作，状态只由 PresetManager 管理。"""

from core.preset_manager import PresetManager


def run_preset_command(manager: PresetManager, arguments: str = "") -> str:
    parts = arguments.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "list"
    name = parts[1].strip() if len(parts) > 1 else ""
    if action == "list" and not name:
        presets = manager.list_presets()
        lines = ["🎭 全局预设管理：", f"当前激活: {manager.active_name}",
                 f"📁 预设目录: {manager.directory}"]
        lines.extend(f"• {p.filename}{'（已激活）' if p.active else ''} - {p.name}: {p.description}" for p in presets)
        if not presets:
            lines.append("暂无可用预设，请自行创建 .yml / .yaml 文件后 reload。")
        lines.append("切换: /preset use <文件名>\n停用: /preset off\n重载: /preset reload")
        return "\n".join(lines)
    if action == "use" and name:
        if manager.set_active_preset(name):
            return f"✅ 全局预设已切换为 {manager.active_name}，后续所有平台的匹配作用域请求生效。"
        return "❌ 预设不存在、无效或状态保存失败，请检查文件并 /preset reload。"
    if action in ("off", "clear") and not name:
        if manager.clear_active_preset():
            return "ℹ️ 已停用所有预设，恢复默认纯净模式。"
        return "❌ 预设停用失败，状态未保存。"
    if action == "reload" and not name:
        manager.reload_presets()
        return f"✅ 已重载 {len(manager.list_presets())} 个预设，当前激活: {manager.active_name}。"
    return "用法: /preset [list | use <文件名> | off | clear | reload]"
