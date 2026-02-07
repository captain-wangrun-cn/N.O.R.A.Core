# 工作区隔离 (Workspace Isolation)

## 设计动机

**核心问题**：如何防止 AI "乱翻家里的东西"？

真人助手的行为：

- 知道自己的工作范围（厨房、客厅，但不能进卧室）
- 知道哪些东西不能碰（保险箱、私人日记）
- 需要做事时会问："XX 在哪？"

AI 的危险行为（如果不加限制）：

- `read_file('config.yml')` → 泄露 API 密钥
- `exec_command('cat .env')` → 暴露所有秘密
- `exec_command('ls ../../..')` → 探索整个文件系统
- `exec_command('rm -rf /')` → 灾难性破坏

## 核心设计

### 工作区结构

```
N.O.R.A.Core/  ← WORKSPACE_ROOT（工作区根目录）
  ├── brain/          # 核心逻辑（AI 可读）
  ├── skills/         # 技能目录（AI 可读写）
  ├── downloads/      # 下载文件（AI 可读写）
  ├── logs/           # 日志文件（AI 不可读）
  ├── config.yml      # 配置文件（禁止读取）
  └── .env            # 环境变量（禁止读取）
```

### 隔离层次

```mermaid
graph TD
    A[LLM 请求操作] --> B{操作类型?}
    B -->|读文件| C[read_file 检查]
    B -->|写文件| D[write_file 检查]
    B -->|执行命令| E[exec_command 检查]
    B -->|列目录| F[list_dir 检查]

    C --> G{路径安全?}
    D --> G
    E --> H{命令安全?}
    F --> I[自动过滤敏感目录]

    G -->|敏感文件| J[拒绝: 含密钥]
    G -->|安全路径| K[允许执行]
    H -->|危险命令| L[拒绝: 安全原因]
    H -->|读敏感文件| M[拒绝: 含密钥]
    H -->|技能脚本| N[重定向到 execute_skill]
    H -->|安全命令| K
```

## 安全机制

### 1. 敏感文件黑名单

**定义**：

- `config.yml`, `config.yaml` - 包含 API 密钥
- `.env`, `.env.local` - 环境变量文件

**检查逻辑**：

```mermaid
graph LR
    A[路径请求] --> B[提取文件名]
    B --> C{在黑名单?}
    C -->|是| D[返回: Access denied<br/>该文件含敏感数据]
    C -->|否| E[继续执行]
```

**应用场景**：

- `read_file('config.yml')` → 拒绝
- `exec_command('cat .env')` → 拒绝
- `exec_command('grep API config.yml')` → 拒绝

### 2. 危险命令拦截

**黑名单**：

- `rm -rf /` - 删除根目录
- `mkfs` - 格式化磁盘
- `dd if=` - 磁盘镜像
- `> /dev/` - 写入设备
- `shutdown`, `reboot` - 关机重启
- `passwd` - 修改密码

**检查逻辑**：

```mermaid
graph TD
    A[exec_command 调用] --> B{命令包含黑名单关键词?}
    B -->|是| C[返回: 安全原因拒绝]
    B -->|否| D{读取敏感文件?}
    D -->|是| E[返回: 该文件含密钥]
    D -->|否| F{运行技能脚本?}
    F -->|是| G[重定向到 execute_skill<br/>附带使用说明]
    F -->|否| H[允许执行]
```

### 3. 技能脚本重定向

**问题**：LLM 倾向用 `exec_command` 跑技能脚本

**解决方案**：

```mermaid
graph LR
    A[exec_command检测到<br/>python3 skills/] --> B[提取技能名]
    B --> C[返回重定向提示]
    C --> D["Error: 不要用 exec_command<br/>请用 execute_skill<br/>示例: execute_skill('xxx', args)"]
    D --> E[LLM 调整策略]
```

### 4. 目录列表过滤

`list_dir` 自动过滤：

- 隐藏文件（`.git`, `.env`）
- 缓存目录（`__pycache__`）
- 系统文件（`.DS_Store`）

**好处**：

- 简化输出，减少 LLM 困惑
- 隐式保护敏感文件

## 提示层引导

### 工作区信息注入

每次请求都注入工作区上下文：

```
【工作区信息】
当前工作目录: /root/nora.core/N.O.R.A.Core
技能目录: /root/nora.core/N.O.R.A.Core/skills
下载目录: /root/nora.core/N.O.R.A.Core/downloads/
⚠️ 禁止读取: config.yml, .env (含 API 密钥)
💡 使用 list_dir 查看目录内容，不要用 exec_command ls
```

### 工作区规则

系统提示中的明确规则：

1. **不要盲目探索** - 项目结构已知，不需要 `ls -R`
2. **禁止读取配置文件** - 明确列出禁止文件
3. **技能文件位置** - 告知 `skills/技能名/` 结构
4. **下载文件位置** - 技能输出到 `downloads/`
5. **路径使用规范** - 工具用相对路径，回复用绝对路径

## 多层防护

```mermaid
graph TD
    A[LLM 请求] --> B[提示层引导]
    B --> C{LLM 遵守规则?}
    C -->|是| D[正常执行]
    C -->|否| E[工具层拦截]
    E --> F{违规类型?}
    F -->|读敏感文件| G[拒绝+说明]
    F -->|危险命令| H[拒绝+警告]
    F -->|技能脚本| I[重定向+示例]
    G --> J[LLM 收到反馈]
    H --> J
    I --> J
    J --> K[调整策略]
```

## 真实案例

### 案例 1：防止密钥泄露

**攻击尝试**（LLM 好奇）：

```
LLM: read_file('config.yml')  # 想看看配置长什么样
```

**系统响应**：

```
Access denied: 'config.yml' contains sensitive data (API keys, tokens).
You should not read this file.
```

**效果**：API 密钥未泄露，LLM 知道不该读

### 案例 2：防止文件系统探索

**攻击尝试**：

```
LLM: exec_command('ls ../..')  # 想看看上层目录
```

**系统响应**（配合提示引导）：

```
（系统提示已告知项目结构，LLM 不应该这么做）
如果仍然执行 → 返回父目录文件列表（无害，但浪费 turn）
```

**优化方向**：拦截 `ls` 命令，提示用 `list_dir`

### 案例 3：防止技能脚本误用

**错误用法**（按旧提示）：

```
LLM: exec_command("python3 skills/某技能/main.py --arg value")
```

**系统响应**：

```
Error: Do NOT use exec_command to run skill scripts.
Use the 'execute_skill' tool instead.
Example: execute_skill("技能名", '{"arg": "value"}')
The execute_skill tool handles argument parsing, timeout, and error reporting automatically.
```

**效果**：LLM 学会正确用法，不再绕过安全机制

## 与技能系统协同

技能运行在 **subprocess** 中，进一步隔离：

- 超时限制：120 秒
- 独立进程空间
- 标准输入/输出/错误流隔离

**好处**：

- 即使技能代码有问题（死循环、内存泄漏），也不影响主进程
- 可以强制终止失控的技能

## 设计权衡

| 方面           | 优势             | 劣势          |
| -------------- | ---------------- | ------------- |
| **严格黑名单** | 明确保护敏感文件 | 需要维护列表  |
| **提示引导**   | 减少违规尝试     | 依赖 LLM 理解 |
| **多层防护**   | 纵深防御         | 增加复杂度    |
| **重定向机制** | 引导到正确用法   | 错误信息较长  |
