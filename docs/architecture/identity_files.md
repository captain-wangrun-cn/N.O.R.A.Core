# 身份与记忆文件一览（openclaw 启发）

> 本项目的身份/记忆文件设计受到 openclaw 启发，在此集中列出各文件的定位、写入原则与权限边界，便于维护。

## 文件总览

| 文件 | 作用 | 写入/读取规则 | 备注 |
| --- | --- | --- | --- |
| `SOUL.md` | AI 的人设、语气、边界 | 仅当用户明确要求调整人设/语气时更新，并告知用户；前脑发现变更需求需 `[NEED_BACKEND]` 交后脑写入 | 禁止写入用户信息 |
| `USER.md` | **主人**画像：偏好、背景、联系方式、习惯 | 任何对话中听到的**主人**信息可主动更新（无需用户说“记一下”）；前脑标记 `[NEED_BACKEND]` 后脑写入 | 禁止写 AI 人设/日程；**只写主人，不写访客** |
| `SCHEDULE.md` | 作息与日程安排，驱动主动消息调度 | 用户提到作息/日程变化或与记录不符时提醒并更新；前脑标记 `[NEED_BACKEND]` | 仅写作息/日程，禁止其他内容 |
| `data/memory/MEMORY.md` | 长期记忆：决策、约定、持久事实、经验 | 重要信息随时记录；前脑标记 `[NEED_BACKEND]` | 保持精炼，定期整理 |
| `data/memory/YYYY-MM-DD.md` | 每日记忆（不自动注入） | 需要时显式读取/写入，用于当天事件日志 | 可用于整理后再汇总入 MEMORY |
| `data/memory/people/<昵称>.md` | **其他人**（非主人/访客）的自主人物笔记 | Nora 想记住某位访客/熟人时按昵称建文件，记显示名、平台 id、印象等 | 她的自主记忆区；与 USER.md 互不混淆 |
| `CUSTOM.md` | 用户维护的全局自定义指令 | **仅用户**编辑；系统自动注入提示；AI 禁止用文件/命令工具读取或修改 | AI 只需遵守内容 |
| `SECRET.md` | AI 私人加密记事本（想法/偏见/反思） | 仅用专用工具 `read_secret_vault` / `write_secret_vault`；首次写入生成 `workspace/data/secret.key`（Fernet）；前脑需标记 `[NEED_BACKEND]` 路由后脑写入 | 回复中不得泄露内容；通用读写/命令工具均被阻止 |

## 使用与边界
- 前脑不得直接读写文件；只做路由，需写入时在回复末尾标记 `[NEED_BACKEND]` 交由后脑执行。
- 敏感文件（`SECRET.md`/`secret.key`/`CUSTOM.md`/`.env`/`config.yml` 等）被通用读写/命令工具阻止，SECRET/CUSTOM 仅走专用或人工路径。
- CUSTOM 由用户自定义，系统自动注入全局提示；AI 不得修改。
- SECRET 用于 AI 自我记录，不对用户暴露；写入默认追加且可附时间戳。

## 主人 vs 访客（跨平台接力后）
- `USER.md` 描述的是**主人**，当前说话人不一定是主人。每轮上下文注入 `<current_speaker>` 块
  （`actor_display_name` + `is_owner`），由 `brain/prompts.py::_build_current_speaker_block` 构造。
- **主人 + 私聊**：默认场景，不注入额外块。**主人 + 群聊**：提示注意场合得体性。
  **访客（非主人）**：提示别把 USER.md 当对方，对访客有边界。
- 访客笔记写 `data/memory/people/<昵称>.md`，不写进 USER.md。详见
  [跨平台接力与五层身份模型](./cross-platform-relay.md)。

## 关联实现
- Prompt 注入：`brain/prompts.py` 加载 SOUL/USER/MEMORY/SCHEDULE 和 CUSTOM（只读）；前脑模板支持 SCHEDULE/CUSTOM 块；路由规则在 `brain/templates/front_brain.jinja`。
- 专用工具：`read_secret_vault` / `write_secret_vault`（`brain/tools.py`），加密存储与密钥管理。
- 敏感防护：`brain/tools.py` 在 `_is_path_safe` / `write_file` / `exec_command` 层阻断对敏感文件的通用访问。

## 借鉴
- 部分文件的设计思路受到 openclaw 启发，并结合本项目的双脑架构与安全隔离需求进行调整。
