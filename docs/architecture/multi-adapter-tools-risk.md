# 多适配器下的 Tools 平台隔离风险（Phase G 记录）

> 状态：**已知风险，未修复**。多平台并发（Phase A–F）已落地，但工具层仍按单适配器绑定。此文档记录问题与后续修复方向，不阻塞路由/活跃场景功能。

## 现状

- `core/controller.py:294`：`ToolManager(adapter, ...)` 只接收 **primary 适配器**（`self.adapter` = 适配器列表首个）。
- `brain/tools.py:189 _register_adapter_tools()`：只调用 `self.adapter.get_adapter_tools()`，即**只注册 primary 平台**导出的适配器工具。
- 所有适配器工具 spec 的 `callable` 都绑定 primary 适配器实例。

## 后果

进程内同时运行 telegram + onebotv11 时：

1. **非 primary 平台的适配器工具完全缺失** —— 若 onebotv11 是 primary，Telegram 专有的适配器工具（如 TG 特有的媒体/群管能力）不会被注册，Nora 在 TG 场景里无法调用。
2. **跨平台会话调用工具会打到错误适配器** —— 即使工具名相同，`callable` 绑定的是 primary 适配器，在另一平台的会话中执行会发到错平台/错 chat。
3. **同名工具冲突未按平台隔离** —— `brain/tools.py:201` 遇到重名直接跳过；多适配器各有同名工具时，只有 primary 的那个能注册。

> 注意：**路由/发送不受影响**。出站消息经 `controller._adapter_for_key(runtime_key)` 按平台前缀选适配器（Phase B），已正确多平台分发。受影响的仅是**适配器专有工具**的注册与执行绑定。

## 修复方向（后续独立任务）

1. `ToolManager` 接收 `adapters: Dict[str, BaseAdapter]`（与 controller 注册表一致），而非单个 adapter。
2. `_register_adapter_tools` 遍历所有适配器，注册各自工具；工具 spec 记录归属 `platform`。
3. 每个适配器工具的 `callable` 绑定**其所属适配器实例**（而非 primary）。
4. **按会话平台过滤暴露**：`get_tool_schemas()` / `get_tool_intros()` 依当前会话平台，只暴露该平台（+平台无关）的工具；避免把 TG 工具暴露给 QQ 会话。
5. **同名冲突**：按平台命名空间隔离（如内部 key 用 `platform:tool_name`），或暴露时按会话平台择一。

## 关联

- Phase A–F 已完成：多适配器并发、controller 适配器注册表、活跃平台/场景、路由标记解析、送达接线、prompt/场景可见性。
- 本项为 Phase G，按计划**仅记录**，作为后续独立工作。
