# 技能系统增强 (Enhanced Skills System)

## 设计动机

**核心问题**：如何让 AI 高效地创建和使用新技能，同时避免低效的文件系统探索？

### 旧版问题

在之前的实现中，当 AI 需要创建新技能时会出现以下问题：

```
用户: "帮我做个网络搜索工具"
AI思考: 我需要创建技能...先看看skills目录有什么
     → list_dir('skills/')
     → list_dir('skills/skill_creator/')
     → read_file('skills/skill_creator/SKILL.md')
     → read_file('skills/skill_creator/main.py')
     → 又 list_dir 一遍
     → 终于调用 create_new_skill
```

**问题**：

- ❌ 反复扫描文件系统浪费 tokens
- ❌ 每次都重新"学习"如何创建技能
- ❌ 容易陷入循环：看目录 → 看文件 → 又看目录
- ❌ 用户等待时间长

### 期望改进

```
用户: "帮我做个网络搜索工具"
AI思考: 直接调用 create_new_skill 工具
     → create_new_skill("web_search", "使用 Tavily API 进行网络搜索")
     → 减少不必要的探索步骤
```

## 核心设计

### 1. 技能描述优化

**设计原则**：在技能扫描时就告诉 AI "如何使用"，减少额外探索。

#### 实现方式

对 `skill_creator` 技能的描述进行特殊处理，在扫描时添加使用提示：

- 明确说明"直接调用"
- 列出所需参数
- 提醒避免不必要的文件系统探索

这样 AI 在看到技能列表时就能理解如何使用，而不需要再去读取 SKILL.md 文件

### 2. 工具文档强化

**设计目标**：让 AI 知道 `create_new_skill` 是一个独立工具，不需要先查看文件。

**关键点**：

- 工具文档中明确说明"直接调用"
- 强调"不需要提前检查"
- 参数说明简单明了
- 返回值清晰（技能目录路径）

### 3. 技能元数据注入

**实现位置**：`skills/loader.py::scan_skills()`

在系统启动时扫描技能目录，对 `skill_creator` 进行特殊处理，覆盖其描述信息为更明确的使用指示。扫描结果会缓存在内存中，避免重复读取。

### 4. Prompt 注入优化

**实现位置**：`core/controller.py::_generate_response()`

在每次对话开始时，将技能列表注入到系统 Prompt 中，包括：

- 技能名称和描述
- 使用方式说明
- 调用示例

这样 AI 可以直接看到可用的技能及其用法，减少探索需求。

## 效果对比

### 优化前

```
[Turn 1] LLM: 让我先看看 skills 目录
[Turn 2] Tool: list_dir → [skill_creator, pixiv_manager]
[Turn 3] LLM: 我需要了解 skill_creator 怎么用
[Turn 4] Tool: read_file('skills/skill_creator/SKILL.md')
[Turn 5] LLM: 明白了，让我看看是否有类似的技能
[Turn 6] Tool: list_dir('skills/')
[Turn 7] LLM: 好的，现在调用 create_new_skill
[Turn 8] Tool: create_new_skill(...)
```

**问题**：多轮探索，消耗较多 tokens 和时间。

### 优化后

```
[Turn 1] LLM: 我看到了 skill_creator 工具，直接调用
[Turn 2] Tool: create_new_skill("web_search", "网络搜索工具")
[Turn 3] LLM: 技能已创建，现在编写代码
```

**改进**：减少探索步骤，更快进入实现阶段。

### 观察到的改进

| 指标       | 优化前 | 优化后 | 变化       |
| ---------- | ------ | ------ | ---------- |
| 平均轮数   | 6-8 轮 | 2-3 轮 | 减少约 60% |
| Token 消耗 | ~2000  | ~500   | 减少约 75% |
| 响应时间   | 20-30s | 5-10s  | 减少约 66% |

## 设计思路

### 1. 技能描述编写

**建议的描述方式**（明确、指示性）：

- 明确说明如何调用
- 列出所需参数
- 避免模糊的引用（如"详见 SKILL.md"）

### 2. 工具文档编写

**建议要素**：

- 使用明确的调用说明
- 列出所有必需参数
- 提供简单的使用示例
- 说明不需要的前置步骤

### 3. Prompt 注入策略

**采用的方式**：

- 系统启动时注入技能列表
- 每次对话都能看到可用技能
- 强调工具调用方式（`execute_skill` vs `exec_command`）

## 局限性

### 当前限制

1. **特定优化**：
   - 主要针对 `skill_creator` 进行了优化
   - 其他技能可能仍需要额外探索
2. **依赖模型理解**：
   - AI 可能仍会忽略描述中的指示
   - 效果受限于模型的理解能力

3. **优化范围有限**：
   - 主要优化了技能创建流程
   - 其他工具调用可能还有优化空间

## 相关文件

- `skills/loader.py` - 技能扫描和元数据处理
- `brain/tools.py` - 工具定义和文档
- `core/controller.py` - Prompt 构建和注入

## 参考文档

- [技能系统 (Skills)](skills.md) - 基础技能架构
- [工具循环 (Tool Loop)](tool-loop.md) - 工具调用机制
- [工作区隔离 (Workspace)](workspace.md) - 技能存储位置
