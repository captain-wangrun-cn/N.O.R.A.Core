# Nora 偏好系统 (Nora Preferences)

> 相关实现：`config.py`、`config.example.yml`、`brain/prompts.py`、`brain/templates/user_preferences.jinja`、`core/scheduler.py`、`core/scheduler_mixin.py`、`adapters/telegram/main.py`

---

## 1. 设计目标

Nora 偏好系统用于集中管理 **行为参数** 与 **回复风格参数**，让 Nora 的主动性、节奏、语气和聊天方式可以被持续调节，而不是零散地硬编码在 prompt 或调度逻辑中。

该系统主要解决四类问题：

1. **对话延续要不要真的追一句**
2. **自主主动消息要不要发**
3. **回复该短一点还是展开一点**
4. **语气该更温柔、更俏皮还是更有主张**

系统设计上明确区分：

- **代码逻辑层偏好**：直接影响状态机或发送决策
- **Prompt 风格层偏好**：影响模型输出风格，但不直接改状态流

---

## 2. 配置入口

配置位于：

- `config.yml` → `nora_preferences`
- 示例模板：`config.example.yml`
- 统一读取入口：`config.get_nora_preferences()`
- 统一保存入口：`config.set_nora_preferences()`

### 2.1 当前字段

```yaml
nora_preferences:
  nora_followup_probability: 1.0
  proactive_message_probability: 1.0
  split_reply_probability: 0.7
  short_reply_preference: 0.5
  verbosity_preference: 0.5
  pause_between_splits_seconds: 1.0
  warmth_level: 0.7
  playfulness_level: 0.4
  emotional_expressiveness: 0.6
  assertiveness_level: 0.5
  followup_skip_end_after: 3
```

---

## 3. 字段分层

### 3.1 行为层（代码逻辑直接使用）

#### `nora_followup_probability`
含义：

- 当 follow-up 检测结果为 `FOLLOWUP` 时，Nora 实际发送该 follow-up 的概率。

注意：

- 它**不控制是否进行 follow-up 检测**。
- 检测始终照常执行，避免破坏状态机。

#### `proactive_message_probability`
含义：

- 控制 **自主型主动消息**（`autonomous`）的实际发送概率。

注意：

- 它**不作用于**用户明确要求的固定提醒（`explicit`）
- 它**不作用于**闹钟（`alarm`）

#### `followup_skip_end_after`
含义：

- 当 follow-up 检测多次命中 `FOLLOWUP`，但又连续因概率未发送时，达到该次数后直接结束会话。

默认策略：

- 第一次没发：按 `WAIT` 处理
- 连续若干次没发：转 `END`

这是为了防止对话长期停在“理论上该追但又一直没追”的不稳定中间态。

---

### 3.2 风格层（Prompt 注入使用）

这些字段通过 `brain/templates/user_preferences.jinja` 注入到 prompt：

#### `split_reply_probability`
控制 Nora 是否更倾向使用分段聊天式输出。

#### `short_reply_preference`
控制 Nora 是否更偏好短句、轻量回复。

#### `verbosity_preference`
控制 Nora 是否更愿意展开解释、补充细节。

#### `pause_between_splits_seconds`
控制默认推荐的分段停顿秒数，用于引导模型更自然地输出 `[SPLIT:秒数]`。

#### `warmth_level`
控制温柔感、亲近感、安抚感强度。

#### `playfulness_level`
控制轻松感、调皮感、小玩笑倾向。

#### `emotional_expressiveness`
控制情绪表达外显程度。

#### `assertiveness_level`
控制是否更愿意表达判断、给建议、轻微主导对话方向。

---

## 4. Prompt 注入链路

### 4.1 模板拆分

为避免把偏好逻辑直接塞进 `system.jinja`，系统新增了独立模板：

- `brain/templates/user_preferences.jinja`

职责分工：

| 模板 | 职责 |
|------|------|
| `system.jinja` | 全局规则、协议、工具与行为边界 |
| `user_preferences.jinja` | 当前偏好参数的风格化注入 |
| `schedule.jinja` | 调度、follow-up、主动消息相关 prompt |

### 4.2 注入方式

在 `brain/prompts.py` 中：

1. 读取 `config.get_nora_preferences()`
2. 渲染 `user_preferences.jinja`
3. 将结果作为独立 instruction 注入总 prompt 组装链路

这样做的好处：

- 偏好层与系统规则层解耦
- 便于后续继续扩展偏好字段
- 更容易调试“是规则影响了输出，还是偏好影响了输出”

---

## 5. Follow-up 概率逻辑

实现位于：

- `core/scheduler_mixin.py`

### 5.1 基本流程

```text
FOLLOWUP 检测
  ↓
结果 = FOLLOWUP / WAIT / END
  ↓
如果结果是 FOLLOWUP
  ↓
按 nora_followup_probability 决定是否真正发送
```

### 5.2 为什么概率不放在“是否检测”

如果概率作用在“是否检测”这一层，会导致：

- 本该进入 follow-up 生命周期的对话根本不被检查
- 状态可能长期停在未结束的中间态
- 对话节奏会变得不可预测

因此系统采用更稳的策略：

- **始终检测**
- **检测后再决定是否发送**

### 5.3 概率未命中时的处理

当检测结果为 `FOLLOWUP`，但概率未命中：

- 不视为已发送
- 不继续维持强 follow-up 候选状态
- 先按 `WAIT` 处理
- 连续多次未命中后直接 `END`

这是为了让状态机保持闭环，而不是无限悬空。

---

## 6. Proactive 概率逻辑

### 6.1 计划事件分类

主动消息在**计划生成阶段**就区分为两类：

| `message_kind` | 含义 |
|---------------|------|
| `explicit` | 用户明确要求的固定时间提醒/督促/通知 |
| `autonomous` | Nora 自主决定的问候、关心、延续话题 |

此外还有：

| `event_type` | 含义 |
|-------------|------|
| `alarm` | 工具设置的闹钟/倒计时 |

### 6.2 为什么要在生成阶段区分

系统不在发送时靠文案临时猜“像不像提醒”，而是在 daily plan 生成时就要求 LLM 输出：

```json
{
  "time": "08:00",
  "message_kind": "explicit",
  "reason": "吃药提醒"
}
```

这样可以让后续逻辑稳定地区分：

- 哪些是用户明确授权的承诺型消息
- 哪些是 Nora 自主发挥的陪伴型消息

### 6.3 概率作用范围

`proactive_message_probability` 仅作用于：

- `message_kind == "autonomous"`

不会作用于：

- `explicit`
- `alarm`

也就是说：

> “提醒”是履约，不是随机人格行为。

---

## 7. `[SPLIT:秒数]` 与节奏偏好

系统本身已经支持：

- `[SPLIT]`
- `[SPLIT:秒数]`

但过去模型更容易只输出裸 `[SPLIT]`。因此当前方案分两层增强：

### 7.1 协议层增强

在 `brain/templates/system.jinja` 和 `brain/templates/schedule.jinja` 中明确要求：

- 优先使用显式 `[SPLIT:秒数]`
- 情绪停顿、语义转折、递进说明时要主动写 delay
- 不要所有 delay 都机械写成一样
- 不要该停顿时完全不写 delay

### 7.2 偏好层增强

在 `user_preferences.jinja` 中注入：

- `split_reply_probability`
- `pause_between_splits_seconds`

让模型理解：

- 当前用户更喜欢强聊天感还是一次说完
- 默认停顿建议大概是多少秒

---

## 8. Telegram 配置入口

Telegram 适配器新增：

- `/nora_prefs`

实现位于：

- `adapters/telegram/main.py`

### 8.1 交互方式

使用 inline keyboard 直接调整偏好参数。

特点：

- 点击按钮即可循环切换数值
- 修改立即保存到 `config.yml`
- 不需要手动编辑配置文件

### 8.2 当前可调字段

- `nora_followup_probability`
- `proactive_message_probability`
- `split_reply_probability`
- `short_reply_preference`
- `verbosity_preference`
- `pause_between_splits_seconds`
- `warmth_level`
- `playfulness_level`
- `emotional_expressiveness`
- `assertiveness_level`

### 8.3 配套展示

`/schedule_today` 现在会显示每条计划的类型：

- `明确提醒`
- `自主消息`

便于直接检查计划分类是否合理。

---

## 9. 当前设计原则总结

Nora 偏好系统当前遵循三层分工：

### 9.1 代码逻辑层
负责：

- `nora_followup_probability`
- `proactive_message_probability`
- `followup_skip_end_after`
- `explicit / autonomous / alarm` 的发送决策差异

### 9.2 Prompt 偏好层
负责：

- 分段聊天倾向
- 长短回复倾向
- 详细程度倾向
- 默认停顿倾向
- 温柔 / 俏皮 / 情绪 / 主张强度

### 9.3 协议规则层
负责：

- `[SPLIT:秒数]` 的使用规范
- 调度计划输出格式约束
- `message_kind` 的强制输出要求

---

## 10. 后续可扩展方向

未来如果继续扩展该系统，建议优先保持以下原则：

1. **不要把所有偏好都做成概率**
   - 有些适合概率
   - 有些适合强度
   - 有些适合阈值
   - 有些适合时间参数

2. **继续区分“行为参数”和“风格参数”**
   - 行为参数进代码
   - 风格参数进 prompt

3. **所有新增偏好都优先进入独立模板或统一配置入口**
   - 不要把偏好散落在多个 prompt 中硬编码

4. **Telegram 调整入口应尽量和配置结构同步**
   - 避免 UI 与配置字段脱节

---

## 11. 相关文件

### 配置与读取
- `config.py`
- `config.example.yml`

### Prompt 注入
- `brain/prompts.py`
- `brain/templates/user_preferences.jinja`
- `brain/templates/system.jinja`
- `brain/templates/schedule.jinja`

### 调度与状态机
- `core/scheduler.py`
- `core/scheduler_mixin.py`
- `core/message_handler.py`

### Telegram 入口
- `adapters/telegram/main.py`

### 测试
- `tests/test_nora_preferences.py`
- `tests/test_nora_prefs_telegram.py`
