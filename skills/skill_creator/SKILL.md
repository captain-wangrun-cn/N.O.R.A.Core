<skill>
  <name>skill_creator</name>
  <description>Create/Update agent skills. Use when user asks for new capabilities (e.g. 'learn to paint') or to modify existing ones.</description>
</skill>

# Skill Creator (技能创造者)

这个技能指导你如何为 N.O.R.A. Core 添加新的能力。

## 技能的结构

一个标准的技能包含在 `skills/<skill_name>/` 目录中，核心文件是 `SKILL.md`。

```text
skills/
  └── <skill_name>/
      ├── SKILL.md        (必须: 技能定义和操作指南)
      └── <script>.py     (可选: 如果技能需要执行复杂逻辑，可以使用 Python 脚本)
```

## SKILL.md 模板

创建新技能时，请严格遵守以下 XML 头部格式，以便加载器能识别它：

```markdown
<skill>
  <name>weather</name>
  <description>Get current weather forecast. Use when user asks about temperature or rain.</description>
</skill>

# Weather Skill

## 什么时候使用
描述这个技能的适用场景。

## 如何使用
详细的步骤说明。
例如：
1. 读取 `config.yml` 获取配置。
2. 运行 `python skills/my_new_skill/script.py --arg value`。
3. 读取输出结果并回答用户。

```

## 操作步骤

如果你被要求创建一个新技能（例如 "weather"）：

1.  **创建目录:** 使用 `mkdir -p skills/weather`。
2.  **编写文档:** 使用 `write` 工具创建 `skills/weather/SKILL.md`，填入 `<name>`, `<description>` 和详细指南。
3.  **编写脚本 (如需):** 如果需要调用 API 或处理数据，编写 `skills/weather/weather.py`。
4.  **安全审查 (Security Check):**
    - 在运行新生成的脚本前，**必须**先进行自我代码审查，确保无危险操作（如 `rm -rf`, `subprocess` 滥用）。
    - **询问用户:** "我已为您编写了脚本 `weather.py`。为了安全，您希望我先进行一次AI代码审计，还是直接运行？"
    - 如果用户选择审计，请仔细分析代码潜在风险后再执行。
5.  **注册:** 不需要手动注册。下次系统启动或重载时，Loader 会自动扫描到它。

## 注意事项

- **自包含:** 尽量让技能逻辑包含在自己的目录里，不要修改核心代码 (`core/`, `brain/`)。
- **Python 脚本:** 如果编写脚本，请确保它能独立运行（使用 `if __name__ == "__main__":`），并打印清晰的输出供你读取。
