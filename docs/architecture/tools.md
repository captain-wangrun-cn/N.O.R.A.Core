# 内置工具系统 (Tools) 技术文档

> 文件位置：`brain/tools.py`  
> 主类：`ToolManager`

本文档说明 N.O.R.A. Core 当前全部内置工具的用途、参数、返回格式和使用建议。

> 2026-03-11 更新：
> - `brain/tools.py` 新增 `TOOL_INTROS`，为每个工具提供一句话简介，用于注入到前脑/后脑提示词。
> - 工具简介仅用于**前脑路由判断**是否需要后脑；前脑禁止直接调用工具，所有执行在后脑完成。

---

## 1. 总览

`ToolManager` 当前注册的工具：

1. `create_new_skill`
2. `execute_skill`
3. `execute_tool_plan`
4. `read_file`
5. `search`
6. `write_file`
7. `edit_file`
8. `list_dir`
9. `get_available_skills`
10. `exec_command`
11. `view_media`
12. `crop_image_for_llm`
13. `report_progress`
14. `set_alarm`
15. `list_alarms`
16. `cancel_alarm`
17. `read_secret_vault`
18. `write_secret_vault`

---

## 2. 工具详解

## `create_new_skill`

**作用**：创建一个技能脚手架（目录、`SKILL.md`、`main.py` 模板）。

**参数**：
- `skill_name` (string, required): 技能名
- `description` (string, required): 技能描述

**返回**：
- 成功：返回创建路径和后续实现提示
- 失败：`Error: ...`

**注意**：仅创建模板，不会生成可运行业务逻辑。

---

## `execute_skill`

**作用**：执行 `skills/<skill_name>/main.py`。

**参数**：
- `skill_name` (string, required): 技能目录名
- `args_json` (string, optional): JSON 字符串参数，默认 `{}`

**返回**：
- 成功：技能 stdout
- 失败：`Error: ...` 或 `REFUSED: ...`

**注意**：
- 会拒绝执行明显模板代码。
- 自动注入 `WORKSPACE_ROOT` / `DOWNLOADS_DIR` / `SKILLS_DIR` 环境变量。

---

## `execute_tool_plan`

**作用**：一次调用中串行执行多个工具步骤。

**参数**：
- `plan_json` (string, required): JSON 数组，元素格式 `{"name": "tool", "args": {...}}`
- `stop_on_error` (boolean, optional, default=true)
- `dedupe_successful` (boolean, optional, default=true)
- `max_steps` (integer, optional, default=20, max=50)

**返回**：
- JSON 字符串，含 summary/steps 明细

**注意**：
- 禁止嵌套 `execute_tool_plan`。
- 默认会跳过“已成功且入参相同”的重复调用。

---

## `read_file`

**作用**：读取文件全文或指定行。

**参数**：
- `path` (string, required)
- `start_line` (integer, optional)
- `end_line` (integer, optional)

**返回**：
- 文件内容；按行读取时附带行范围头信息
- 错误：`Error: ...`

**安全限制**：禁止读取敏感文件（如 `config.yml`、`.env`）。

---

## `search`

**作用**：在文件中做关键词/正则搜索。

**参数**：
- `query` (string, required)
- `path` (string, optional, default=".")
- `include_pattern` (string, optional, default="**/*")
- `is_regex` (boolean, optional, default=false)
- `case_sensitive` (boolean, optional, default=false)
- `max_results` (integer, optional, default=50, max=200)

**返回**：
- 匹配列表（`file:line: preview`）
- 无结果时返回扫描统计

---

## `write_file`

**作用**：覆写写入文件（不存在则创建）。对敏感文件有防护。

**参数**：
- `path` (string, required)
- `content` (string, required)

**返回**：
- 成功：`Successfully wrote to ...`
- 失败：`Error writing file: ...`

**安全限制**：拒绝写入敏感文件（`config.yml` / `.env` / `SECRET.md` / `CUSTOM.md` / `secret.key`）。SECRET/CUSTOM 需要用专用通道，见文末。

---

## `edit_file`

**作用**：按片段替换文件内容（先精确匹配，再尝试空白归一化匹配）。

**参数**：
- `path` (string, required)
- `old_code` (string, required)
- `new_code` (string, required)

**返回**：
- 成功：`Successfully edited ...`
- 失败：`Error: old_code not found ...`

**建议**：复杂修改失败后，改用 `write_file` 全量覆写。

---

## `list_dir`

**作用**：列目录内容（包含文件大小、目录标记）。

**参数**：
- `path` (string, optional, default=".")

**返回**：
- 目录内容文本
- 错误：`Error: ...`

---

## `get_available_skills`

**作用**：列出当前可用技能目录。

**参数**：无

**返回**：
- `Available skills:` 列表
- 无技能或错误提示

---

## `exec_command`

**作用**：执行 shell 命令（高风险兜底工具）。

**参数**：
- `command` (string, required)
- `timeout` (integer, optional, default=60, range 10-600)

**返回**：
- 含退出码、stdout/stderr 的结构化文本

**安全限制**：
- 阻断高危命令模式（如 `rm -rf /`）。
- 阻断通过 shell 读取敏感文件。
- 阻断直接 `python skills/...`，并引导改用 `execute_skill`。

**额外限制**：阻止借助 shell 读取 `SECRET.md` / `CUSTOM.md` / `secret.key`。SECRET 使用专用工具；CUSTOM 仅用户手动编辑。

---

## `view_media`

**作用**：仅从图片记忆系统回查**用户曾发送给 AI 的历史图片**（按 ID / 标签语义 / OCR 文本 / 时间 / 最近）。不用于搜索或生成新图。支持关键词与 OCR 文本联合检索。

**参数**：
- `image_id` (string, optional)
- `keyword` (string, optional)
- `text_query` (string, optional): OCR 文字模糊搜索（大小写不敏感、空格分词 AND 逻辑）
- `start_time` (string, optional, Unix 时间戳)
- `end_time` (string, optional, Unix 时间戳)
- `user_id` (string, optional)
- `limit` (integer, optional, default=10, max=50)
- `return_image` (boolean, optional, default=false)

**返回**：
- 元数据列表（含 OCR 文字预览，若存在）
- 当 `return_image=true` 时，额外返回 `MediaTag: [image: abs_path]`

OCR文本为模型回复，并非外置OCR

**联动**：控制器会解析 `MediaTag`，并在下一轮切到 image 模型分析；此时为“工具回查图片”，不再要求生成 `[IMAGE_TAGS]`。

---

## `read_secret_vault`

**作用**：解密读取 `SECRET.md`（AI 私人加密记事本）。

**参数**：无。

**返回**：
- 解密后的纯文本；空内容返回 `(secret vault is empty)`
- 失败：`Error reading SECRET.md: ...` 或解密错误提示

**实现要点**：
- 首次写入时生成密钥 `workspace/data/secret.key`，使用 Fernet 对称加密。
- 文件/密钥均列为敏感，通用文件/命令工具禁止访问。

**使用场景**：AI 自我反思/偏见/想法，不向用户展示。

---

## `write_secret_vault`

**作用**：写入/追加到 `SECRET.md`（加密存储）。

**参数**：
- `content` (string, required): 纯文本内容
- `append` (boolean, optional, default=true): 是否追加；false 时覆盖
- `add_timestamp` (boolean, optional, default=true): 是否自动加 UTC 时间戳前缀

**返回**：
- 成功：`Secret note saved (append=..., length=...)`
- 失败：`Error writing SECRET.md: ...`

**实现要点**：
- 自动生成并复用 `workspace/data/secret.key`。
- 旧内容解密失败时会重置为新内容并记录警告。

**安全约束**：
- SECRET 仅供 AI 内部记事，回复中不得泄露内容。
- 禁止用 `read_file`/`write_file`/`exec_command` 操作 SECRET/CUSTOM/secret.key。

---

## 关于 CUSTOM.md 与敏感文件

- `CUSTOM.md`：用户自定义全局指令，系统自动注入提示，AI 不得通过文件工具读取/修改。
- 敏感名单：`config.yml`、`.env`、`SECRET.md`、`CUSTOM.md`、`secret.key` 等，所有通用文件/命令工具会拒绝访问。

---

## `crop_image_for_llm`

**作用**：裁剪图片重点区域，便于 LLM 读取关键部分。

**参数**：
- `image_path` (string, required)
- `preset` (string, optional)
- `left` / `top` / `right` / `bottom` (integer, optional)
- `padding` (integer, optional, default=0)
- `output_path` (string, optional)
- `return_image` (boolean, optional, default=true)

**关键规则**：
- `preset` 与像素范围模式二选一。
- **若两者同时给出，优先使用 `preset`**。

**内置 preset**：
- `top_half`
- `bottom_half`
- `left_half`
- `right_half`
- `center`
- `top_center`
- `bottom_center`

**返回**：
- 裁剪信息（模式、源路径、输出路径、尺寸、裁剪框）
- `return_image=true` 时包含 `MediaTag: [image: ...]`

---

## `set_alarm`

**作用**：设置绝对时间或倒计时闹钟。

**参数**：
- `trigger_time` (string, optional): 触发时间（与 `countdown_minutes` 二选一）
- `reason` (string, optional): 闹钟缘由
- `countdown_minutes` (integer, optional): 倒计时分钟数
- `allow_conflict` (boolean, optional, default=false): 是否允许与已有闹钟时间在 5 分钟内冲突

**返回**：
- 成功：闹钟 ID 与触发时间
- 失败：提示存在时间冲突或参数错误

**注意**：
- 当 5 分钟内已有闹钟时，默认返回提示，需确认后再设置（`allow_conflict=true`）。

---

## `list_alarms`

**作用**：列出所有未触发闹钟。

---

## `cancel_alarm`

**作用**：取消指定闹钟。

## 3. 路径与安全说明

- 所有文件路径会通过 `_resolve_workspace_path` 标准化：支持绝对路径、`workspace/...`、相对路径。
- 文件读取受敏感文件白名单保护。
- 执行命令受高危关键字拦截。

---

## 4. 开发扩展建议

新增工具时建议：

1. 在 `ToolManager._register_tools()` 注册。
2. 编写清晰 docstring 和 `:param` 说明（schema 会读取）。
3. 参数类型标注尽量精确（`Optional[int]`、`bool`、`float` 等）。
4. 返回文本尽量结构化，便于 LLM 二次解析。
5. 有副作用工具要增加安全护栏。
