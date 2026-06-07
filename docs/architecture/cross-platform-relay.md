# 跨平台接力与五层身份模型 (Cross-Platform Relay)

> **一句话**：把 N.O.R.A 从"多个平台各自一段上下文"改成"**同一个 Nora 在不同地方说话**"。
> 平台只是通信端点；Nora 的人格、记忆、上下文、媒体和任务状态属于同一个连续主体。
> 同时把"用户即主人"拆成**主人 (owner)** 与**说话人 (actor)**，以适配群聊里的多个用户。

---

## 1. 设计动机

旧模型下，消息历史 / RAG / 图片记忆都按 `(platform, chat_id)` 分区，**读取还按 `platform` 过滤**。
结果是：同一个人在 Telegram 私聊、网页、群里跟 Nora 说话，被切成互不相通的几段上下文——
Nora 在网页上不记得 Telegram 说过的事，像换了个人。

跨平台接力解决两件事：

1. **连续主体**：不论在哪个平台 / 私聊 / 群聊，底层归入同一个 `memory_scope_id`，上下文共享。
2. **主人 vs 访客**：群聊会出现多个说话人。`USER.md` 描述的是**主人**，当前说话人不一定是主人，
   需要显式区分，避免把主人的隐私 / 偏好安在陌生访客身上。

---

## 2. 五层身份模型

所有平台会话都解析成一个 `ConversationIdentity`（`core/conversation_identity.py`）：

| 层 | 字段 | 含义 | 默认值 |
|---|---|---|---|
| 自我 | `owner_id` | 主人是谁 | `owner:default` |
| 关系 | `relationship_id` | 你和 Nora 的主关系 | `relationship:owner:default` |
| **记忆作用域** | `memory_scope_id` | **共享上下文主键**（历史/RAG/媒体都按它检索） | = `relationship_id` |
| 地点 | `place_scope_id` | 话从哪来，可回查 | `{platform}:{platform_chat_id}` |
| 说话人 | `actor_display_name` + `is_owner` | 谁在说、是否主人 | 昵称 / 绑定判断 |

保留字段（运行时用，非上下文主键）：

- `runtime_key` (`{platform}:{platform_chat_id}`)：发送消息 / typing / 任务调度的目标。
- `platform_chat_id`：平台实际投递地址。
- `storage_id`：兼容期 fallback（私聊=actor_user_id，群聊=platform_chat_id），不再作最终共享上下文主键。

> 单主人个人实例：`memory_scope_id` 恒为 `relationship:owner:default`，所有平台 / 私聊 / 群聊全量合并进同一作用域。

---

## 3. 主人识别 (Owner Registry)

`core/owner_registry.py` + `data/owner_bindings.json` 提供**最小**主人识别（不是多租户系统）：

- 命中绑定 → 主人 (`is_owner=True`)。
- **私聊**未绑定 → 自动绑定该说话人为主人，并返回 True（每平台第一个私聊者即主人）。
- **群聊**未绑定 → 不是主人 (`is_owner=False`)，按访客对待。
- config 预声明：`config.yml → owner.identities: [{platform, user_id}]` 启动时注入绑定。

解析通过回调注入：`conversation_identity.set_owner_resolver(...)` 在 controller 启动时挂上
`owner_registry.resolve_is_owner`，让身份模块保持零业务依赖、可独立测试。

---

## 4. 共享上下文与数据迁移

- 消息历史三表（`messages`/`summaries`/`conversation_sessions`）+ 镜像库 `raw_messages` +
  压缩库 `context_segments` 都新增 `memory_scope_id` 列；读取优先按 `memory_scope_id`，
  旧 `(platform, chat_id)` 作 fallback。详见 [消息历史管理](./message_history.md)。
- **`close_session` 语义**：进 SEMI_ONLINE 时按 `memory_scope_id` 关闭当前**连续对话**，
  而非按单平台孤立关闭——否则 Telegram 收尾会切断 Web 的连续对话。
- RAG / 图片记忆 payload 新增同套 scope 字段，检索按 `memory_scope_id` 过滤，旧 payload 缺字段回退。
  `view_media` 默认注入当前 `memory_scope_id`，跨平台能找回别处发过的图；精确回查仍走
  `platform + platform_message_id + chat_id` 不串图。
- **自动迁移**：启动检测旧表缺列 → `ALTER TABLE`；旧行 `memory_scope_id` 补 `relationship:owner:default`、
  `place_scope_id` 由旧 `platform/chat_id` 生成；保留旧列与旧查询 fallback，非破坏性。

---

## 5. 主动消息与最近活跃端 (Delivery Target)

`core/delivery_target_store.py` 持久化 `data/delivery_target_state.json`：
`last_active_runtime_key` / `last_active_platform` / `last_active_platform_chat_id` / `last_active_at` / `memory_scope_id`。

- 每次用户**私聊**消息进入 → 更新最近活跃端。**群聊活跃不更新**（落实「仅私聊投递」，绝不自动发进群）。
- `ProactiveScheduler` / `TriggerManager` 通过注入 `resolve_delivery_callback` 解析投递目标：
  - **闹钟 (alarm)**：用户/AI 显式设定了目标，原样投递，**绝不重定向**。
  - **主动消息 (proactive) / trigger**：默认投到 `last_active_runtime_key`；不可用 → 回退 `default_chat_id`。
- `default_chat_id` 语义改为 **fallback 投递目标**，文件兼容保留，不再代表上下文归属。

---

## 6. 主人 vs 访客的 Prompt 注入

`brain/prompts.py` 的 `load_identity_context` / `get_system_prompt` 接收
`actor_display_name` / `is_owner` / `chat_type`，构造 `<current_speaker>` 注入块：

- **主人 + 私聊**：默认场景，不注入额外块，保持上下文纯净。
- **主人 + 群聊**：提示注意场合得体性——私密内容看场合，别在群里机械抖出来。
- **非主人（访客）**：提示这是访客，别把 `USER.md` 当对方；对访客保持礼貌、有边界；
  想记住 ta 就在 `data/memory/people/<昵称>.md` 建自主笔记（与主人档案 USER.md 互不混淆）。

system prompt 模板 `brain/templates/system.jinja`：
- §0.5「跨平台接力」准则：同一个 Nora / 平台是地点 / 记得别处 / 群聊得体自判 / 接力自然说明。
- §5 身份与记忆：主人 vs 访客边界 + `data/memory/people/` 自主人物记忆区。

---

## 7. 关联实现与文档

| 关注点 | 文件 / 文档 |
|---|---|
| 身份解析 | `core/conversation_identity.py` |
| 主人识别 | `core/owner_registry.py`、`data/owner_bindings.json` |
| 投递目标 | `core/delivery_target_store.py`、`data/delivery_target_state.json` |
| 共享历史 | `memory/message_history.py`、[消息历史管理](./message_history.md) |
| 镜像/压缩 | `memory/context_store.py` |
| RAG / 媒体 | `memory/rag.py`、`memory/image_store.py`、[图片记忆系统](./image-memory.md) |
| Prompt 注入 | `brain/prompts.py`、`brain/templates/system.jinja` |
| 身份文件边界 | [身份与记忆文件一览](./identity_files.md) |

---

## 8. 已知风险

- **群聊隐私**（最大赌注）：全量合并 + 仅 prompt 约束，私聊隐私可能被 Nora 在群里提起。
  按决策接受，靠 prompt 强约束 + 仅私聊投递降低暴露面。
- **改动面大**：`storage_id` 引用众多，全程保留 fallback，逐 Phase 验证，不一次性删旧键。
- **数据不可逆**：迁移保留旧列 + 旧查询 fallback，非破坏性；变更生产 SQLite 前先备份
  （`message_history.db` / `message_log.db` / `context_compression.db`）。
