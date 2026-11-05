# QQ 官方机器人 adapter

## 定位与接入

`qqbot` 直接连接 QQ 官方 API v2，不是 OneBot/NapCat；与 Telegram、OneBot v11 可同进程并发。官方平台已支持主动消息和全群接收，adapter 不采用旧的 @-only/禁主动模型。Core 的群半在线/全在线与主动安全路由保持不变。

1. 在 QQ 开放平台创建机器人，取得 AppID / AppSecret，并开通测试所需消息权限。
2. 将 `adapters/qqbot/config.example.json` 复制为该目录的 `config.json`，填 `app_id` / `app_secret`。该私有文件已被忽略，不要提交。没有 Node/扫码 SDK 依赖。
3. **公开接入前预绑定主人的 QQ 官方开放 ID**：全局 `owner.identities` 的 `platform` 为 `qqbot`，`user_id` 为可靠的 `user_openid` / `member_openid`。现有 OwnerRegistry 的首次私聊自动绑定策略未修改；不预绑定可能让首位陌生私聊者占位。开放 ID 不是 QQ 号；同一个人在 C2C/群中的 ID 是否一致须实测，不能自动绑定 OneBot 的 QQ 号。
4. 启动 `python main.py --no-tui --adapter qqbot`，或 `--adapter telegram,onebotv11,qqbot`。`auto` 检测所有已配置平台；旧 `qq` 别名仍指向 `onebotv11`。

只创建示例配置；实施和 mock 测试不需要真实凭据。不要把 AppSecret、token、附件 rkey 或预签名 URL 输出到日志。

## 模块

| 文件 | 职责 |
|---|---|
| `main.py` | 生命周期、场景分链事件分发、群门控、聚合、标准事件分发 |
| `client.py` | token 提前刷新/401 竞争、REST、WS Identify/Resume/心跳 ACK |
| `message.py` | opaque ID、mentions、引用关系、有界嵌套记录 |
| `state.py` | 场景隔离的有界去重、被动凭据、引用索引缓存 |
| `sender.py` | 文本/Markdown/媒体、被动预算和序号、主动回退、自身撤回 |
| `media.py` | 公网 HTTPS 下载校验、工作区文件、分片上传与场景缓存 |
| `commands.py` | 核心命令前置闸门及设置入口 |
| `interactions.py` | keyboard、一次性绑定 token、独立 ACK 和命令授权 |
| `adapters/delivery.py` | 平台无密钥投递快照、兼容 list 的可见回执与部分失败/取消 |

## 连接与事件

- token 请求 `POST https://api.bot.qq.com/app/getAppAccessToken`；REST 和 WS 使用 `QQBot {AccessToken}`。
- 使用 `/gateway/bot` 返回的 WSS；Hello 后 Identify 或 Resume，心跳带最新已入队序号，漏 ACK 关闭并重连。消息消费与网络心跳分离，队列只做背压（满则不推进 seq、重连后重放）；出队后按场景分链——同群/同私聊串行、跨场景并发、控制事件独立链，生成轮与媒体下载不再阻塞其它聊天的入站管线。互动 ACK 不排在下载/普通消息后面。关闭取消 worker、全部场景链、互动任务、聚合 timer 和心跳，关闭自有会话。
- 订阅 `(1<<25) | (1<<26)`：C2C、全群、群 @、互动及关系变更。拒收/机器人移除事件阻止对应场景发送，恢复事件解锁；未知事件忽略。
- 全群与 AT 双事件、Resume 重放按场景 + `msg_idx` 去重，缺失回退 `d.id`，不是正文去重。SEMI 忽略的非定向全量版本不吞掉后到的 AT 版本。
- ONLINE 消息进入已有 group listener，SEMI 仅 @自己/引用已知自己的消息进入聚合；**不新增 SEMI 非@消息入库**。@其它 bot 不是 @自己。全量模式 @ 也可能只推 `GROUP_MESSAGE_CREATE`：认平台 `mentions[].is_you` 严格布尔 `true` 及可靠 User ID 别名，不能只依赖 AT 事件或 READY ID，不能把任意 `bot=true` 当自己。
- 接收诊断日志记录 gateway 事件、解析结果、忽略原因、群门控、聚合/Core 路由；场景 ID 哈希化，不记正文/密钥/附件 URL。`gateway dispatch` 没有群事件与 `group gate action=ignore_non_directed` 是不同故障层。
- 群不同发送者分开聚合。未知名称/人数保持未知。嵌套 `msg_elements` 限深度 4、节点 60、文字 8000，内部媒体只显示占位。

## 三类消息 ID 与发送快照

1. `d.id`：标准入站 ID，消息被动发送时用 `msg_id`。
2. 外层 `id`：互动等事件的被动 `event_id`，不与 `msg_id` 同时传；互动 ACK 使用 **`d.id`**，去掉 `INTERACTION_CREATE:` 前缀。
3. 入站 `message_scene.ext.msg_idx` / 出站 `ext_info.ref_idx`：原生引用索引，转换后写 `message_reference.message_id`，不是普通 `d.id`。

聚合/群提升后构造不可变 `DeliveryContext`，只带平台、场景、本轮来源 ID 和发送模式。前脑、后脑、polling、DRAW/VOICE 显式透传；合并重启重建来源集合。adapter 不保存“最近一条入站消息”作为隐式发送目标。显式 `[reply:ID]` 只影响引用，不提供被动资格。跨平台/场景发送把来源清空，主动通知/followup 显式主动，绝不借用目标场景恰好缓存的新消息。

私聊每凭据 4 次、群 5 次，锁和 `msg_seq` 跨多次 send 共用，覆盖长文本/SPLIT/媒体。凭据用尽或明确过期转主动；429 有限等待并复用同一序号；权限失败不重试，超时结果不确定**不转主动重复发**。200 响应缺少消息 ID 也视为结果不确定，不冒充成功。

`text_chunk_size=2000` 是可调的软切块值，**不是官方长度上限**。被动时效暂用保守 `passive_ttl_seconds=300`，私聊文档概述 60 分钟与字段表 5 分钟冲突，等待真机契约验证。若平台拒绝软长度，保持部分回执并报告失败，不无限重试。

出站文本被 40034006（消息内容违规）或 40034127（无 markdown 模板权限）拒绝时，自动换 `msg_type=0` 纯文本重试一次（凭据 msg_seq 继续递增）；40034127 之后本进程直接改发纯文本，不再每条白耗一次 400。同一内容纯文本仍被拒则干净失败——不重试、不转主动重复发。响应头 `X-Tps-Trace-Id` 随 `QQBotAPIError.trace_id` 进发送失败日志（`trace=` 字段），供联系开放平台排查。投递彻底失败（结果确定且无任何已送达内容）时，Core 按平台开关 `notify_delivery_failure`（当前仅 qqbot 为 True）向场景补发一条固定系统提示（`core.message_handler.DELIVERY_FAILURE_NOTICE`）；提示自身的发送失败只记日志，绝不递归补发。

## 富媒体与控制

- 入站下载只允许公网 HTTPS，校验每次重定向；自定义 DNS resolver 在实际连接解析时拒绝私网/回环，避免“先查公网、连接又解析”的重绑定。流式累计体积限制，失败清理自身临时文件；远端 filename 不用于路径。
- 本地出站文件限定 workspace 内，拒绝敏感文件及 `appearance/refs/`。预上传携带整文件 MD5/SHA1、前 **10002432** 字节 MD5，按服务端分片 PUT，再 `upload_part_finish`，最后 `/files` 携带 `upload_id` 合并。分片 `index` 文档示例从 0 起，2026-10-31 实测从 1 起：接受连续 0/1 基编号，确认传原编号，乱序/缺片拒绝。明确 `srv_send_msg=false`，避免上传阶段意外消耗主动发送预算。预签名主机不收到 QQ Authorization。
- `file_info` 按场景/文件版本缓存并遵守 ttl，`ttl=0` 表示长期。只用普通富媒体 `file_info`，不把它当 Markdown 内嵌图片的 `qqbot-media://` 素材地址。内嵌素材管理尚未接入。
- 语音优先下载 `voice_wav_url`，保留 ASR 参考文本，用已有 `[file:]` 输入链路。出站 `.oga` 为 Ogg 容器，仅更正上传名为 `.ogg`，**不表示已转码或已获平台验证**；不支持时明确失败，不伪装成语音成功。必要转码留待实际格式结果决定。
- `[reply:ID]` 只引用当前场景已知索引，未知/跨场景引用安全移除。入站 @ 可解析；指定成员出站 @ 未获得可靠契约，移除 `[at:]` 并记降级，不声称已提醒。`[poke:]` 不支持。
- 自身撤回仅允许已知自己发出的消息，缓存中年龄小于 120 秒；重启后未缓存旧消息不会试撤他人。`/undo` 取实际目标 adapter 能力，多条撤回有一条失败即不声称全部界面已删除。

## 命令和交互

Core 命令复用现有 dispatcher；QQ 前置闸门在 `handle_new_message` 最终分发前执行，覆盖多行、聚合及群提升。修改配置、清库、撤销等命令仅限主人；混合多个命令或多人监听批次拒绝，避免确认 A 却执行 B 或借用末位主人的身份。嵌套记录中的命令不执行，聚合/群提升保持该安全标志。

- `/clear`：明确警告永久清空跨平台共享历史，按钮二次确认后才准许清库。
- `/set_stream`：二选一按钮，或 `/set_stream on|off`。
- `/model <alias> <name>`：共用 `config.set_llm_model_by_alias()`，立即保存并刷新；不做 TG 完整 provider 分页枚举。
- `/effort <alias> <level>`：复用 effort helper，`default` 清除单别名设置。
- `/nora_prefs`：显示偏好及简短菜单，`/nora_prefs <key> <value>` 校验后持久化。
- `/status`、`/context` 等由 Core 处理；没有危险 `/start` 或全库 `/debug_cleanup` 菜单。

keyboard 按腾讯官方 botgo `dto/keyboard/keyboard.go` 的 callback type=1 构造。token 为随机、不含裸命令，绑定用户/场景/动作/参数、180 秒过期、单次消费、旧菜单和同菜单其它按钮失效。客户端 permission 之外仍服务端验权。ACK 与业务分离，同 interaction ID 仅尝试一次 ACK，耗时业务通过新消息反馈，不编辑旧消息。

## 送达与历史

成功 contract 仍为 `list[str]`（`DeliveryReceipt` 子类附可见文本）；`DeliveryError` 承载确认已发 IDs/片段，`DeliveryCancelled` 保持 asyncio 取消语义并携带部分回执。SPLIT 累积之前成功的段，不重发已发段。

前脑、后脑、polling 转述、主动/followup 只落已确认可见部分；完全失败不落伪发送，DRAW/VOICE 无回执不打成功日志。后脑 sent 标志在成功后置位。**polling / no_reply 的合法内部产物仍保留**，不会按“无平台 ID 就扔掉”清空工作上下文。QQ 普通 session 保留旧历史已发内容，不把本轮未确认草稿当作已见回复。多分区归档仍合为单条 system，但每项标平台/场景来源，不迁移或重写存量摘要。

## 验证与尚未完成的端到端项

新增 `tests/test_qqbot_{client,adapter,sender,media,interactions,delivery}.py`，mock HTTP/WS 和临时工作区；现有重点回归另跑。全量测试必须用仓库 `.venv`，异步 `anyio`。

2026-10-30 临时 HEAD 快照 + 合成内存配置基线：938 passed / 13 failed / 1 skipped；同配置当前全量：1020 passed / 同 13 failed / 1 skipped。全部新增 QQ 测试 82 passed；重点回归另跑 293 passed。13 个既有失败涉及 main 模块导入污染 2、旧价格数据 4、cost tracker 2、图片语义 fixture 2、旧 asyncio mark 2、timezone 上下文数量 1；不在本次改造中顺带修复。

2026-10-31 提交前回归：全部 QQ adapter 测试与 adapter selection 共 **100 passed**。经主人授权已在深圳实测私聊、群 @/ONLINE 普通消息，以及 `SOUL.md` 仅上传链路；群自身提及和一基分片编号问题已修复。最终聊天文件送达尚未确认，不能把上传成功当作消息送达。

仍待核验私聊时效、指定成员 @、Ogg 语音、长度/频控、主动接收开关、同人 C2C/群 ID。Webhook、Node 扫码、频道、QQ token 级流式、完整 TG 模型分页、RTC、群管 tools、Markdown 素材管理均未实现。本期不重构 ToolManager 的 primary 工具注册，不新增 QQ 专属后脑工具。

## 官方参考

- [接入指南](https://bot.q.qq.com/wiki/agent-qqbot/)
- [WS 事件协议](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- [全群事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)
- [富媒体](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)
- [预上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_id_upload_prepare.post.html)
- [分片确认](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_id_upload_part_finish.post.html)
- [合并/文件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_files.post.html)
- [互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)
- [腾讯官方 keyboard 类型](https://github.com/tencent-connect/botgo/blob/master/dto/keyboard/keyboard.go)
