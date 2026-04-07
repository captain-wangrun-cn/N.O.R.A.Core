# N.O.R.A.Core

N.O.R.A.Core 是一个主要 AI 助手框架，旨在提供个人陪伴和一点点效率提升。虽然不敢说完美，但在长期记忆和拟人化交互方面做了一些微小的尝试。

> [!CAUTION]
> 本项目为**VibeCoding**项目(氛围编程)(本人使用VSC+GithubCopilot)
> **99%的代码为AI生成**，本人仅提供架构设计与部分指导
> 如果阅读本项目的源码给你带来了包括*心跳加速*,*高血压*等不适，本人不负任何责任
> **本项目为个人项目，本来只是个人用，代码内可能包括但不限于各种离谱设计，史山代码等，但是请手下留情，我只是想要自己用，不希望带来更多麻烦**
> 个人的详细声明可以阅读[架构文档索引](docs/architecture/README.md)

## 架构设计

更多细节请查阅 [架构文档索引](docs/architecture/README.md)。

### 项目特点

- **双脑协同（前脑 + 后脑）**：前脑负责即时回复与路由判断，后脑负责深度执行（工具/技能/RAG），兼顾响应速度与任务完成度。就算正在工作，也可以陪用户对话/聊天
- **前后脑轮询审查**：后脑总结给前脑进行审查，不满意可以暂时告知用户再等一会并继续工作
- **后脑可打断与可切换任务**：支持用户在执行中随时要求停止或加入新任务，更接近真人对话中的“插话”与“改需求”。
- **长期记忆能力<font color="red">（RAG系统待完善）</font>**：RAG数据库，SOUL USER等markdown文档，按照消息段压缩上下文。能记住要点，重点内容
- **主动消息/闹钟**：根据SCHEDULE内容，自动生成主动消息触发，可以主动发消息问候用户。同时也可以定闹钟提醒用户
- **图片入库**：解决传统架构无法的图片仅存在当此请求的问题。收到的图片会生成标签，ocr文本等存入数据库。后续可以根据关键词等再次获取到图片，就像你让你朋友回去看之前发过的一张图片
- **图片裁剪分析<font color="red">（自主性调用待完善）</font>**：AI能主动裁剪图片并再次分析，能够更好的查看细节
- **技能<font color="red">（行业适配性待完善）</font>**：skills系统，可以从外部下载技能，或者让AI自己创作技能，自己学习成长
- **触发器**：可扩展的trigger系统，触发AI的不一定是用户或主动消息，trigger系统提供一个可自定义扩展的触发器用于触发AI，顺便可以让AI主动提醒/处理事务。例如邮件触发
- **适配器<font color="red">（扩展适配性待完善）</font>**：可扩展的adapter系统，实现跨平台对话
- **多模型架构**：分为四个模型，对应的模型做对应的事情，兼顾速度与质量，以及性价比

## ✅ 环境要求

- Python 3.12+
- 可访问的 LLM API（Gemini / OpenAI 或兼容 OpenAI 的网关）
- Telegram Bot Token（若使用 Telegram 适配）

> Windows 用户建议额外安装 `tzdata`，避免时区数据库缺失导致 `ZoneInfo` 报错。

## 快速开始
1. 克隆仓库:

   ```bash
   git clone https://github.com/captain-wangrun-cn/N.O.R.A.Core.git
   cd N.O.R.A.Core
   ```

2. 安装依赖:
   强烈建议使用虚拟环境
   ```bash
   python -m venv .venv
   ```

   Windows
   ```bash
   .venv\Scripts\activate
   ```
   Linux
   ```bash
   source .venv/bin/activate
   ```

   安装依赖
   ```bash
   pip install -r requirements.txt
   pip install tzdata
   ```

3. 配置 (使用 CLI 向导):
   ```bash
   python cli.py
   ```
   选择运行配置向导，根据提示填入
   
   - `fast`模型: 快速决策,判断是否追问,web_fetch总结等
   - `smart`模型: 平常对话,前脑主模型
   - `coder`模型: 专业决策,调用工具/技能,后脑主模型
   - `summary`模型: 总结,压缩等

4. 运行:
   ```bash
   # 带 TUI（默认）
   python main.py

   # 无 TUI / 纯命令行
   python main.py --no-tui
   ```

## 管理工具

N.O.R.A. Core 包含一个简易的 CLI 工具 (`cli.py`) 用于配置和维护：

```bash
# 交互式菜单
python cli.py
```

## 目录结构

```text
N.O.R.A.Core/
├─ main.py / cli.py / tui.py / view_costs.py
├─ config.py / config.example.yml / workspace_config.py
├─ brain/                 # 模型接入、提示词模板、工具定义
│  ├─ providers/          # Gemini / OpenAI 适配
│  └─ templates/          # Jinja 提示词模板
├─ core/                  # 控制器、前后脑、轮询、调度
├─ adapters/              # 平台适配（当前以 Telegram 为主）
├─ memory/                # 历史消息、RAG、向量、图片记忆
├─ skills/                # 仓库预装技能
├─ triggers/              # 外部触发系统（Email 等）
├─ docs/
│  ├─ architecture/       # 架构设计文档
│  └─ onboarding/         # 新会话接手文档
├─ tests/                 # 单元测试
└─ locales/               # 多语言文案
```

更详细目录与说明请查看：[`docs/CODE_STRUCTURE.md`](docs/CODE_STRUCTURE.md)

## 常见问题

- **`ZoneInfoNotFoundError`（Windows）**
   - 执行：`pip install tzdata`
- **启动提示找不到 `config.yml`**
   - 先复制 `config.example.yml` 为 `config.yml`，再运行 `python cli.py --configure`
- **想快速了解代码结构**
   - 查看 `docs/CODE_STRUCTURE.md`
- **如何让新AI接手此项目**
   Agent模式下，在会话开头第一句话前面加上：`阅读onboarding内的文档`
   如：
   ```
   阅读onboarding内的文档

   编写一个新的trigger，用于......
   ```

## 许可证

本项目采用 **BSD 3-Clause License**

```text
BSD 3-Clause License

Copyright (c) 2026, WangRun(王润)

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
