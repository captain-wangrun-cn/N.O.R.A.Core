# 🧭 Skills 开发手册（面向人类开发者）

> 编写/维护技能时的人类侧速查表，聚焦目录结构、运行约定与常见坑。

## 目录与入口

- 每个技能位于 `workspace/skills/<skill_name>/`，至少包含 `SKILL.md` 与 `main.py`。
- `SKILL.md` 需带 YAML Frontmatter，至少包含 `name`、`description`、`version`，可选 `parameters`（供运行时生成调用 schema）。
- 入口脚本统一放在 `workspace/skills/<skill_name>/main.py`，用 `python3` 直接运行，避免再加子目录。

## 运行时环境与路径

- 框架通过工具 `execute_skill` 调用脚本，并注入这些环境变量：
  - `WORKSPACE_ROOT`：工作区根路径
  - `SKILLS_DIR`：技能目录（`<workspace>/skills`）
  - `DOWNLOADS_DIR`：下载/临时文件目录
  - `CODE_ROOT`：源代码根路径
- 编码时请使用上述环境变量拼路径，避免硬编码绝对路径，确保跨平台。

## I/O 契约

- 参数解析：使用 `argparse`，参数名与 `SKILL.md` 的 `parameters` 对齐，采用 `--k v` 形式。
- 成功输出：写入 `stdout`（建议单行 JSON 便于上层解析）。
- 失败/日志：写入 `stderr`，必要时返回非 0 退出码。
- 入口保留 `if __name__ == "__main__":`，方便被 `execute_skill` 调用。

## 调用链说明

- 运行通道：框架只会经由 `execute_skill("<skill_name>", '{"arg": "value"}')` 执行。
- 直接用 `exec_command` 跑技能脚本会被拦截，请确保脚本兼容 `execute_skill` 的调用方式与环境变量。

## 配置与秘密

- 技能私有配置放在 `workspace/skills/<skill_name>/config.json`（JSON 对象）；运行时会自动注入为环境变量。
- 避免把密钥/令牌写进代码或提交到仓库，必要时使用环境变量或本地未纳入版本控制的文件。

## 数据读写

- 读写文件时以 `WORKSPACE_ROOT` 为基准，或写入 `DOWNLOADS_DIR` 作为输出/缓存目录。
- 避免写出工作区之外的绝对路径。

## 文档要求

- `SKILL.md` 需包含功能说明、参数、示例和注意事项，可参考 `workspace/skills/skill_creator/SKILL.md` 模板部分。
- Loader 仅扫描含 `SKILL.md` 的目录；缺少 `name`/`description` 会被跳过。

## 常见坑

- Windows 需 `pip install tzdata` 以修复 `ZoneInfoNotFoundError`。
- 模板代码会被拒绝执行：`execute_skill` 会拦截带占位标记的 `main.py`。
- 默认超时 120s，长耗时任务请做好切片/进度设计。

## 最小操作流程

1) 创建目录 `workspace/skills/<skill_name>/`，或使用工具 `create_new_skill` 生成骨架。
2) 填写 `SKILL.md` Frontmatter 与使用说明。
3) 编写 `main.py`：`argparse` 参数解析 + 环境变量路径 + stdout/stderr 契约。
4) 如有配置，编写 `config.json`（键值即注入的环境变量）。
5) 本地自测后，再通过 `execute_skill` 调用验证。
6) 更新文档/README/CHANGELOG（如适用）。
