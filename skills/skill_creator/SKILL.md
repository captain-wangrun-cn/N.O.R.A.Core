---
name: skill_creator
description: Create/Update agent skills. Use when user asks for new capabilities (e.g. 'learn to paint') or to modify existing ones.
---

# Skill Creator (技能创造者)

这个技能指导你如何为 N.O.R.A. Core 添加新的、符合 **Agent Skills 开放标准** 的能力。

## 什么时候使用
当主人要求你学习新能力，或者修改现有技能时使用。

## 标准操作流程 (SOP)

### 阶段 1: 文档先行 (Documentation First)
1.  **确认需求:** 与主人沟通，明确新技能的目标、输入参数和期望的输出格式。
2.  **创建目录:** 使用 `mkdir -p skills/<skill_name>`。
3.  **编写 SKILL.md:** 使用 `write_file` 创建 `skills/<skill_name>/SKILL.md`。**必须** 使用下面的 "SKILL.md 最佳实践模板"。
4.  **请求审核:** 将 `SKILL.md` 的内容展示给主人，请求审核。**严禁** 在文档批准前编写任何代码。

### 阶段 2: 代码实现 (Code Implementation)
1.  **编写脚本:** 在主人批准 `SKILL.md` 后，开始编写对应的 Python 脚本 (e.g., `skills/<skill_name>/script.py`)。
2.  **遵循 I/O 契约:**
    *   **输入:** 使用 `argparse` 解析 `SKILL.md` 中 `parameters` 定义的参数。
    *   **输出:** 成功时，向 `stdout` 打印单行 JSON；失败时，向 `stderr` 打印错误信息。
3.  **安全审计:** 编写完成后，**必须** 先进行自我代码审查，然后向主人报告："脚本已完成，经AI初步审计无明显风险。是否批准运行？"
4.  **测试:** 在获得批准后，才能执行脚本进行测试。

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
e.g., Run `script.py` with the arguments defined in `parameters`.

## Examples
- **Example 1:** `python3 skills/your-skill-name/script.py --arg_name_1 "value"`
  - [描述这个例子的作用和预期输出]
- **Example 2:** ...

## Guidelines
- [关于使用此技能的注意事项或最佳实践]
- e.g., The API key for this skill is stored in `config.yml` under the `your_skill_api_key` key.
```
