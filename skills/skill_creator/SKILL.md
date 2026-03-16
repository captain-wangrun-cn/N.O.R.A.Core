---
name: skill_creator
description: Create/Update agent skills. Use when user asks for new capabilities (e.g. 'learn to paint') or to modify existing ones.
parameters:
  type: object
  properties:
    skill_name:
      type: string
      description: 目标技能名称
    action:
      type: string
      description: create 或 update
      default: create
    requirement:
      type: string
      description: 需求描述（功能、输入输出、边界）
  required: [skill_name, requirement]
---

# Skill Creator (技能创造者)

这个技能指导你如何为 N.O.R.A. Core 添加新的、符合 **Agent Skills 开放标准** 的能力。

## 什么时候使用

当主人要求你学习新能力，或者修改现有技能时使用。

## 如何调用（必须使用 `execute_skill`）

- `execute_skill("skill_creator", '{"skill_name": "new_skill", "action": "create", "requirement": "功能需求..."}')`
- 禁止使用 `exec_command` 或直接运行脚本。

## 标准操作流程 (SOP)

### 阶段 1: 文档先行 (Documentation First)
1.  **确认需求:** 与主人沟通，明确新技能的目标、输入参数和期望的输出格式。
2.  **创建目录:** 使用 `mkdir -p skills/<skill_name>`。
3.  **编写 SKILL.md:** 使用 `write_file` 创建 `skills/<skill_name>/SKILL.md`。**必须** 使用下面的 "SKILL.md 最佳实践模板"。
4.  <b>请求审核与自主推进:</b> 将 `SKILL.md` 的内容展示给主人。<b>如果主人的请求明确表示需要立即实现功能（例如：“帮我从Pixiv抓图”），则在展示后可直接进入代码实现阶段，无需等待明确的 “批准” 指令。否则，请等待批准后再进行代码实现。严禁</b> 在文档批准前编写任何代码。

### 阶段 2: 代码实现 (Code Implementation)
1.  <b>编写可工作脚本:</b> 在主人批准 `SKILL.md` 后，开始编写实现核心功能的 Python 脚本 (e.g., `skills/<skill_name>/script.py`)。脚本必须具备实际作用，而非仅仅是一个框架或示例。
2.  **遵循 I/O 契约:**
    *   **输入:** 使用 `argparse` 解析 `SKILL.md` 中 `parameters` 定义的参数。
    *   **输出:** 成功时，向 `stdout` 打印单行 JSON；失败时，向 `stderr` 打印错误信息。
3.  **安全审计:** 编写完成后，**必须** 先进行自我代码审查，然后向主人报告："脚本已完成，经AI初步审计无明显风险。是否批准运行？"
4.  **测试:** 在获得批准后，才能执行脚本进行测试。

- 运行入口：只能通过 `execute_skill("<skill_name>", '{"arg": "value"}')` 调用；不要用 `exec_command` 直接跑脚本。
- 路径使用框架注入的环境变量，禁止硬编码绝对路径：`WORKSPACE_ROOT`、`SKILLS_DIR`、`DOWNLOADS_DIR`、`CODE_ROOT`。
- 配置注入：若存在 `skills/<skill_name>/config.json`，会以环境变量形式注入；不要去读根目录的 `config.yml` / `.env`。
- 输出契约：成功写 `stdout`，错误/调试写 `stderr`，必要时退出码非 0。
- 敏感文件：不要读取/修改 `CUSTOM.md`、`SECRET.md` 等敏感文件；遵守系统提示的文件访问限制。

---

## SKILL.md 最佳实践模板

```markdown
---
name: your-skill-name
description: A clear, concise description of what this skill does and when to use it.
# [可选] 定义脚本的输入参数，LLM 会优先参考这里
parameters:
  type: object
  properties:
    arg_name_1:
      type: string
      description: Description of the first argument.
    arg_name_2:
      type: boolean
      description: Description of a boolean flag.
  required: [arg_name_1]
---

# Your Skill Name

## How to use
[对如何运行脚本或执行此技能的自然语言描述]
必须使用 `execute_skill("your-skill-name", '{"arg_name_1": "value"}')` 调用，禁止直接 `exec_command`。

## Examples
- **Example 1:** `execute_skill("your-skill-name", '{"arg_name_1": "value"}')`
  - [描述这个例子的作用和预期输出]
- **Example 2:** ...

## Guidelines
- [关于使用此技能的注意事项或最佳实践]
- 入口统一用 `execute_skill`；路径使用 `WORKSPACE_ROOT`/`SKILLS_DIR` 等注入环境变量；配置如有需要放 `skills/your-skill-name/config.json`。
```
