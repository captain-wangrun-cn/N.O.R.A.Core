# 实时通话子系统（RTC）设计稿

> 状态：**P2 已实现（2026-08-29），真机验收进行中**。P0 冒烟全通；P1 的
> server/auth/session/前端正式 UI/邀请/时长上限/单并发全部落地；P2 的转写回写/
> 视频段录制总结/字幕按 turn 聚合/TTS 音色复用（voice_source=tts）已落地。
> 自动化测试 92 个 RTC 测试全过（HK 服务器实测验证），部署于 HK（rtc.nora.wris.me
> 前端 + hk.wris.me 桥接）。P3（工具桥）/P4（Nora 主动邀约）未做。
> 调研完成于 2026-08-27（Gemini Live API 官方文档 + 定价 + 官方浏览器样板）。
> 分期计划见文末，实施时逐期落地并更新本文档状态。
>
> 命名说明：叫 **rtc**（Real-Time Communication）不叫 voice——一是模态不止语音（用户可短时开视频），
> 二是 `voice` 已被 TTS 系统占用（`[VOICE]` 标记 / `voice_text` 字段），避免概念缠车。

---

## 1. 目标与非目标

**目标**：在 Telegram 里和 Nora 进行实时双向通话——音频为主，用户可短时开摄像头让 Nora
看到画面。Nora 带人设、带记忆、能调工具，通话结束后对话内容（含用户展示的画面）回流到
文本侧记忆体系。通话可由用户主动发起（`/rtc` 命令），也可由 Nora 主动邀约（`[RTC:]` 前脑标记）。

**非目标**：
- 不做持续视频通话（Live 音视频会话上限仅 2 分钟，见 §3；形态定位为"通话中途短时给 Nora 看"）
- 不做 Nora 的视频形象输出（模型只出音频；"看见 Nora" 归形象生图系统管）
- 不做自定义音色克隆（Live 仅支持 30 个预置音色；自定义声线需 half-cascade，暂不做）
- 不替换文本管线：实时通话是**并行模态**，不经过 `core/controller.py` 的双脑文本流程

---

## 2. 选型结论（已核实，勿凭记忆推翻）

### 用 Gemini Live API，不用 Interactions API

- Interactions API（2026-06 GA）是 generateContent 的继任者：纯 HTTP/SSE 逐轮接口，
  **无 WebSocket、无 VAD、无打断、无连续音频流**，模型清单里没有任何 Live 模型。
- Live API 未弃用：Google 在 Interactions GA 之后（2026-03）仍向 Live API 发布新语音模型
  `gemini-3.1-flash-live-preview`。文档页横幅推荐 Interactions 是面向通用功能的总指引，
  非弃用预告。
- **风险**：Live API 仍是 Preview（旧命名 `gemini-live-*` 已于 2025-12 退役）。因此代码上
  Google 会话层必须单独封装（见 §5.2），前端协议与 Nora 主系统不直接感知 Google 消息格式。

### 架构：服务端桥接，客户端永远不直连 Google

```
浏览器（TG Mini App / 桌面 Chrome）
   │  WSS（PCM16 二进制帧 + JSON 控制消息 + 视频帧）
   ▼
N.O.R.A. RTC bridge（aiohttp，主进程 asyncio loop 内）
   │  WSS（Google Live 协议，JSON + base64 PCM/JPEG）
   ▼
wss://generativelanguage.googleapis.com/...BidiGenerateContent
```

理由：
- Google API key 不出服务器；人设/记忆/工具/成本跟踪全部在场——这是「和 Nora 通话」
  而不是「和一个没人设的 Gemini 通话」的唯一路径
- 官方 ephemeral token 路线是为**客户端直连**设计的；我们的桥接架构走 API key 即可
- 用户网络环境的差链路（国内→境外）由「客户端→自己的 HK/SG 服务器」承担，
  服务器→Google 走干净线路
- 音频码率很低（16k PCM16 单声道 ≈ 32 KB/s），后端中继开销可忽略

### 前端定位：无状态通用瘦客户端 + 同源托管为默认

- 前端纯静态、零秘密、零实例耦合，放进仓库 `rtc/web/`，由各家后端**同源托管**
  （self-hoster 克隆即得 `https://自己的域名/rtc/`，什么都不用填）
- 中央托管是可选镜像：`?backend=https://...` 指定后端地址，桌面浏览器调试用
- Telegram 入口的按钮 URL 由各 bot 自己配置，可携带 `?backend=` 参数实现零输入
- **为什么不默认中央托管**：中央前端要兼容全网所有旧后端（版本漂移成本），
  且麦克风权限授给中央托管的 JS 有信任面问题。同源 = 前后端同仓库同版本，原子演进。

---

## 3. Live API 关键事实速查（2026-08 核实）

> 模型 ID 与价格会变，实施前用官方文档复核本表；架构性结论（PCM16、时长上限、
> 转写机制）短期内稳定。

| 项 | 值 |
|---|---|
| 端点 | `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<API_KEY>` |
| 首条消息 | `setup`（含 model / systemInstruction / tools / voice 等），**必须等 `setupComplete` 后才能发后续消息** |
| 模型 | `gemini-3.1-flash-live-preview`（推荐，native audio，输入 128k 上下文）；备选 `gemini-2.5-flash-native-audio-preview-12-2025`。**setup.model 必须带 `models/` 前缀**（裸名 CLOSE 1008 not found，2026-08 实测） |
| 下行帧 | **全部以 WS 二进制帧到达**（内容仍是 JSON，含 setupComplete 和音频块），接收侧必须同时处理 TEXT/BINARY |
| 会话上限 | 纯音频会话 15 分钟；**音视频会话 2 分钟**；**单条连接约 10 分钟**（服务端先发 `goAway{timeLeft}` 再 ABORTED 掐断） |
| 视频计时 | **P0 实测（2026-08-27）**：发过 8 秒 1FPS 视频帧后连接存活 158.5s（>120s 预算线），无 goAway/ABORTED——"开过视频即全程 2 分钟预算"的保守假设**不成立**，2 分钟限制大概率只约束持续视频流；实现上仍保守显示倒计时，但超限由服务端 goAway 兜底 |
| 视频输入 | 离散图片帧（JPEG ≤1 FPS）。**上行格式必须是平铺 `realtimeInput.video{data,mimeType}`**（`mediaChunks` 数组已被 3.1 系弃用，CLOSE 1007，2026-08 实测） |
| 续命 | `sessionResumption.handle`（令牌 2 小时有效；断线后尽快重连，保守按 10 分钟窗口设计）+ `contextWindowCompression.slidingWindow`（自动压缩，可无限延长会话） |
| 音频输入 | raw PCM16 little-endian，原生 16kHz 单声道（`audio/pcm;rate=16000`）；任意采样率可发、服务端重采样；**只收 PCM，不收 opus/webm 容器**。**上行格式必须是平铺 `realtimeInput.audio{data,mimeType}`**（同上，mediaChunks 已弃用） |
| 音频输出 | raw PCM16，**24kHz**，base64 内联（`serverContent.modelTurn` 内 inlineData） |
| **文本输出** | **native audio 模型只支持 `responseModalities:["AUDIO"]`，不支持 `["AUDIO","TEXT"]` 混合**（官方明文）。模型"说了什么"的文本**只能靠 `outputAudioTranscription` 转写**；转写事件与音频流**无严格时序保证**（官方明文），字幕要做缓冲/去抖 |
| 转写 | `inputAudioTranscription:{}` / `outputAudioTranscription:{}`（空对象即启用）→ `serverContent.inputTranscription/outputTranscription`；**没有"会话结束后取文本"的接口，必须在通话中累积**；转写按文本输出价额外收费 |
| VAD/打断 | 服务端 VAD（`realtimeInputConfig.automaticActivityDetection`，静音阈值默认 ~800ms，推荐 500-800ms）；插话时生成被取消并发 `serverContent.interrupted:true`，**挂起的 function calls 被取消**（`toolCallCancellation{ids}`）。**注意：`startOfSpeechSensitivity`/`endOfSpeechSensitivity` 字段在 3.1 系上会 CLOSE 1007 被拒（2026-08 实测），只能配 `prefixPaddingMs`/`silenceDurationMs`** |
| 工具 | `setup.tools[].functionDeclarations`；无自动执行，收 `toolCall` 后手动执行并回 `toolResponse`（id 必须对齐）；3.1 Flash Live 仅同步函数调用 |
| 音色 | 30 个预置（Kore / Puck / Zephyr / Charon / Fenrir / Leda / Aoede 等），`speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName` |
| 定价 | 输入音频 $3/1M token（25 token/秒 ≈ **$0.005/分钟**）；输出音频 $12/1M ≈ **$0.018/分钟**；文本输入 $0.75 / 输出 $4.50。免费层可用但配额严 + 数据用于改进产品 |
| 成本特性 | **每轮重算全上下文**：会话越长每轮越贵 → 必须配 contextWindowCompression（如 25k 触发 / 滑窗 8k） |
| 并发 | 官方无明确数字（社区：早期 3 并发/项目，疑似已放宽）。按单实例单并发（owner-only）设计即可 |

**P0 冒烟实测出的协议坑（全部已修进 `rtc/live_client.py`，勿再踩）：**
- aiohttp 的 `ws_connect(max_msg_size=0)` **不是"无限制"而是零容忍**——任何消息都判超限直接静默关连接；必须给真实数值（我们用 16MB）
- aiohttp 的 `async for ws` 迭代会**吞掉 CLOSE 帧**——服务端拒绝（1007/1008）被伪装成静默断链；必须用显式 `ws.receive()` 循环并记录关闭码
- setup 消息发送后**必须等 setupComplete**；上一步被拒时要在 connect() 里显式抛错（我们曾被"伪装成功"骗过：recv_loop finally 无条件 set setup_complete 事件）
- 文本输入用 `clientContent.turns` + `turnComplete:true`（不要塞 `clientRoles` 这种不存在字段）

**浏览器侧实战坑（官方样板 + 社区 issue 核实）：**
- 重采样最省事做法：`new AudioContext({sampleRate:16000})` 让浏览器重采样，
  AudioWorklet 里 Float32→Int16（×32768 截断），攒 2048 样本（128ms）一包；
  发送节奏 20-100ms 小块，**不要攒 1 秒**
- 播放不必用 AudioWorklet：`AudioBufferSourceNode` 队列（24kHz 按 7680 样本/320ms 切块，
  look-ahead 0.2s + 首块 100ms 预缓冲）；收到 `interrupted:true` **立刻清空播放队列**
- **Android Telegram WebView：同一次 Mini App 会话内每次 getUserMedia 都重新弹权限框**
  → 麦克风流要长活，静音用「发送开关」实现而不是 stop/start；跨域 iframe 场景宿主需
  `allow="microphone"`
- **iOS Safari 有已知录音 bug**（官方样板 issue #21：无报错但音量恒 0）→ 真机冒烟测试前置
- 回声靠浏览器 AEC：`getUserMedia({audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true}})`；外放场景不保证，UI 引导戴耳机

---

## 4. 桥接 WS 协议（前端 ↔ 后端）

原则：**协议自描述、版本握手、面最小**。客户端→后端音频用**二进制帧**（PCM16 原始字节，
省 base64 开销），其余全部 JSON 文本帧。视频帧率低（≤1 FPS）走 JSON base64 即可。

### 客户端 → 后端

| 消息 | 格式 | 说明 |
|---|---|---|
| `hello` | JSON | 首条。`{version, auth:{initData?|token?}, opts:{voice?, video?}}`。initData 与 token 双轨，见 §6 |
| 音频 | **binary** | PCM16 16kHz mono 原始字节，直接中继给 Google |
| `video_frame` | JSON | `{data:"<base64 JPEG>"}`，≤1 FPS；后端转 `realtimeInput.video`，同时旁路进 recorder 缓存 |
| `control` | JSON | `{action:"mute"\|"unmute"\|"video_on"\|"video_off"\|"interrupt"\|"hangup"}`。mute=后端停止上行转发（音频仍可收）；video_on/off=开/关摄像头（后端启停录制段）；interrupt=转发打断 |
| `tool_result_ack` | JSON | 可选，工具结果展示确认（P3 再定） |

### 后端 → 客户端

| 消息 | 格式 | 说明 |
|---|---|---|
| `welcome` | JSON | 握手成功：`{version, display_name, avatar?, voice, max_seconds, video_budget_seconds?}` |
| `auth_failed` | JSON | 鉴权失败（前端提示并退出；后端对失败连接限速） |
| 音频 | **binary** | PCM16 24kHz 原始字节 |
| `transcript` | JSON | `{who:"user"\|"assistant", text, turn_done?}`——来自 Live 转写；前端字幕**缓冲去抖**（转写与音频无时序保证） |
| `state` | JSON | `{state:"connecting"\|"resuming"\|"rotating"\|"active"\|"degraded"\|"ending", detail?}`——连接轮换/重连对前端的状态可见性 |
| `tool` | JSON | `{name, status:"running"\|"done"\|"cancelled", summary?}`——通话中工具执行提示（可选展示） |
| `bye` | JSON | 服务端结束：`{reason:"limit"\|"error"\|"peer_hangup", resumed_session?}` |
| `error` | JSON | `{code, message}` |

约束：
- 协议版本从 `1` 起；`hello.version` 高于后端支持 → 返回 `error{code:"version"}` 并附支持的版本
- 鉴权在 `hello` 完成前不转发任何音频/视频/控制消息
- 后端异常必须显式 `bye`/`error`，不允许静默断链（前端把异常断链当可重连处理：带 session
  标识自动重试一次）
- 开过视频的会话，`welcome.video_budget_seconds` 按 2 分钟预算下发，前端 UI 明示

---

## 5. 模块划分

```
rtc/                          ← 新子系统（与 adapters/ 平级：不是平台适配器，是模态子系统）
├── __init__.py
├── server.py                 # aiohttp web server：WS 端点 + 静态托管 rtc/web/ + 生命周期（P1 ✅）
├── auth.py                   # initData HMAC 校验（bot_token 为 key）+ 静态 token + → 身份解析（P1 ✅）
├── session.py                # CallSession：一通电话的完整生命周期（状态机）+ 时长上限 + 单并发（P1 ✅）
├── live_client.py            # Google Live WS 客户端封装：setup/消息翻译/resumption 轮换/重连（P0 ✅）
├── prompt.py                 # 通话 systemInstruction 组装：身份上下文+通话指令+rules.md+TTS 标记指南（P1 ✅）
├── invitation.py             # 邀请管理：TTL/去重冷却/URL 构建（P1 ✅，[RTC] 前脑标记 P4）
├── rules.md                  # owner 私有通话规则追加块（至高法则，gitignore 不忽略但内容私有）
├── tools_bridge.py           # ToolManager 精选子集 → functionDeclarations；toolCall→execute→toolResponse；取消竞态（P3）
├── history_writer.py         # 通话结束：转写+视频段摘要 → fast 总结 → message_history 写入（P2 ✅）
├── recorder.py               # 视频段录制（video_on/off 封段，关键帧序列送 image 模型）（P2 ✅）
├── tts_downlink.py           # voice_source=tts 的下行链路：转写增量→provider 流式会话→PCM 下发（P2 ✅）
├── config_utils.py           # rtc 配置读写（static_token 首启随机生成等）（P0 ✅）
└── web/                      # 前端源码（原生 JS + AudioWorklet，无构建）
                              # index.html 正式通话页（底部固定控制条）；test.html P0 调试页（保留）
```

### 5.1 server.py

- 复用主进程 asyncio loop（`aiohttp` 已在 requirements.txt），随主进程启动/停止
- 路由：`GET /` 静态（rtc/web/）、`GET /audio-worklet.js`、`GET /healthz`、`GET /ws`（升级）
  （独立进程模式 `python -m rtc.server` 亦可，日志接 Nora 主日志）
- 并发上限：同时 1 个通话（owner-only MVP；后续可配）
- 与 `core/controller.py` 解耦：不进消息处理流水线，只通过注入的回调/服务对象交互

### 5.2 live_client.py（Google 抽象层）

- 手写 WebSocket 客户端（`aiohttp.ClientSession.ws_connect`），不用 google-genai SDK
  —— 音频字节中继不需要 SDK，纯 WS 更可控；官方手写参考：`gemini-live-ephemeral-tokens-websocket`
  示例的 `frontend/geminilive.js`（解析逻辑完整，可移植）
- 职责：
  1. `setup` 组装（model / systemInstruction / voice / tools / 双转写配置 /
     `sessionResumption:{}` / `contextWindowCompression` / VAD 配置）
  2. 双向中继：上游二进制帧 → base64 `realtimeInput.audio`；`video_frame` → `realtimeInput.video`；
     下行 inlineData base64 → 二进制帧
  3. **连接轮换**：监听 `goAway`（或连接存活 ~8 分钟主动）→ 保存 `SessionResumptionUpdate.newHandle`
     → 静默开新连接（`setup.sessionResumption.handle`）→ 换轨对前端只发一条 `state{rotating}`
  4. 断线重连：指数退避，带 handle 重试；连续失败 N 次向上抛
  5. `sessionResumption` 在 setup 里必须显式带 `{}`（不带会被静默忽略——官方论坛确认的坑）
  6. 转写累积：`inputTranscription` / `outputTranscription` 文本按 turn 聚合，供字幕与回写
- 上游事件分发：`serverContent`（modelTurn/interrupted/turnComplete/transcription）、
  `toolCall`、`toolCallCancellation`、`goAway`

### 5.3 tools_bridge.py

- **白名单精选**：通话场景只暴露快、无副作用、短输出的工具（如 `search`、`set_alarm`、
  `view_media`(仅元数据)、词库查询类）。排除文件编辑、exec_command、技能执行、生图
  ——长耗时工具会卡住对话流
- 工具执行设**硬超时**（如 8s）；超时回「工具执行超时」的 toolResponse 让模型口头兜底
- **取消竞态**：收到 `toolCallCancellation{ids}` 时取消对应 in-flight 任务
  （asyncio.Task.cancel），已完成的直接丢弃结果不回传
- 工具结果文本截断（Live 上下文按 token 计费，长结果直接烧钱）

### 5.4 history_writer.py（通话结束的总结回写链）

通话结束后（`hangup` / 超限 / 异常终止都要走），**严格按以下时序**（顺序错了，
挂断后到摘要落库前抵达的入站消息会先于通话摘要处理，上下文衔接断裂）：

1. **视频段先总结**（recorder.py 产出的每段视频）：逐段送 `video` 模型生成画面描述
   （"用户展示了什么"），得到带时间锚点的段描述列表
2. **fast 模型做总总结**：输入 = 双侧转写文本 + 视频段描述，输出 = 一段通顺的通话摘要
   （含聊了什么、氛围/情绪、看到了什么）；转写无标点/无说话人边界的问题在这一步顺手清洗
3. **摘要写入 message_history**：按 owner 的 telegram 私聊 scope，content 带 `[RTC通话]`
   前缀（开放问题 ①）
4. **原始转写全文落 `message_log.db` 镜像库**（只追溯不进上下文）——摘要丢了细节时查底账
5. **之后才恢复正常的入站消息处理**（见 §5.5；通话期间的消息已被自动回复并丢弃，
   不存在排队积压——只有"挂断后、摘要落库前"窗口内抵达的消息需要等摘要先完成）

**时序图**（视频段按发生顺序排列，聊天与画面交错还原）：

```
通话中:  [聊A] [开摄像头展示X] [聊B] [关] [聊C] ...
recorder:      ├─ 视频段1 ─┤            （video_on → video_off 一段）
总结输入: 聊天转写(带时间) + "视频段1(14:03-14:06): 用户展示了X"
```

**recorder.py 实现注记**：帧到达后端时本来就要转发给 Google（`realtimeInput.video`），
录制 = 同一条链路加第二消费者，零额外采集成本。缓存 `<timestamp, JPEG>` 序列，
`video_off` 时封段：优先直接把关键帧序列作为多图输入送 `image` 模型（省依赖）；
若 `video` 模型链路要求真实视频文件，再用 ffmpeg 编码（引入 ffmpeg 时同步更新
requirements/文档）。帧率 ≤1 FPS、单段 ≤2 分钟，体量很小。

### 5.5 通话期间的消息独占

通话期间**全局独占**（`AIPresence` 置忙 + rtc 入口判定）：

- 任何平台任何人发来消息 → **不排队、不处理**，直接自动回复一条系统提示
  「Nora 正在和 <某人> 通话中」；发消息的人由此知道发生了什么，也知道消息不会被回应
- fast_llm 忙碌路径**整体绕过**（那条路径会回"排队中"文字，通话中收到文字回复很分裂）
- proactive / followup 对所有用户抑制；通话结束恢复
- 通话中发起第二个 RTC 连接 → 拒绝并提示（单并发）

---

## 6. 认证（双轨，owner-only）

`hello.auth` 二选一，后端按序尝试：

1. **initData**（TG 入口，零输入）：Telegram webview 注入，后端用
   `adapters/telegram/config.json` 的 `bot_token` 做 HMAC-SHA256 校验（secret =
   `HMAC(bot_token, "WebAppData")`，校验 `hash` 字段 + auth_date 时效，如 ±5 分钟）。
   解析出 user_id 后查 `core/owner_registry.py` 判定是否 owner，非 owner 拒绝。
2. **静态 token**（桌面浏览器 / 非 TG 入口）：`rtc/config.json` 里 owner 手工配置的
   长随机串。校验失败对源 IP 限速（如 5 次失败锁 10 分钟）。

- 认证只决定「是谁在打」→ 映射到记忆 scope；**绝不能信任客户端自报的 user id**
- initData 校验依赖 telegram adapter 已配置；未配置时该轨自动禁用并打日志
- MVP 阶段两种轨都只放 owner（防止 Live 音频持续烧钱被滥用）

---

## 7. 配置

遵循 adapter 私有配置约定（不进全局 config.yml），放 `rtc/config.json`：

```jsonc
{
  "enabled": false,            // 总开关，默认关
  "listen": {"host": "0.0.0.0", "port": 8085},
  "public_url": "",            // 对外 https 地址（生成邀请按钮/菜单链接用）
  "api_key": "",               // Google API key（留空则回落 llm.api_keys.gemini）
  "model": "gemini-3.1-flash-live-preview",
  "voice_name": "Kore",
  "voice_source": "live",     // live=模型内置音色 | tts=Nora 台词走 TTS provider
                              //（流式合成；provider 的语音标记指南会注入通话提示词）
  "max_call_duration_seconds": 1800,  // 最大通话时长（秒），语音/视频共用（30 分钟）
  "static_token": "",          // 桌面浏览器调试 token（留空 = 首次启动随机生成并写回）
  "invitation_ttl_seconds": 300, // [RTC] 邀请有效期
  "tools_whitelist": ["search", "set_alarm", "list_alarms", "cancel_alarm"]
}
```

HTTPS：server.py 只监听 HTTP；对外 TLS 由现有反代（nginx/caddy）终结，同源路径
`/rtc/`（静态）与 `/rtc/ws`。成本跟踪：`cost_tracker.custom_prices` 补
`gemini-3.1-flash-live-preview` 条目；用量按音频时长 × 25 token/s 估算经
`log_usage()` 入账（转写文本量小，并入文本 token 估算）。

---

## 8. 与现有系统的缝合点（改动面控制）

| 系统 | 缝合方式 | 注意 |
|---|---|---|
| 身份/人设 | `brain/prompts.py` 的 `load_identity_context()` 拼装 systemInstruction（SOUL/APPEARANCE/USER/MEMORY） | Live 的 systemInstruction 单块文本；`[SPLIT]`/`[reply:]` 等文本标记协议**不注入**，另写 rtc 专用指令块（口语化、短句、无 markdown） |
| 记忆连续性 | 开场：近期 message_history 上下文压缩成开场注入（用 summary 模型，或直接取最近段落摘要）；结束：§5.4 回写 | Live 每轮重算全上下文，注入越多每轮越贵；建议 ≤2k token 摘要式注入 |
| 工具 | `brain/tools.py` ToolManager 实例复用 | 只读白名单；工具结果走 toolResponse 不进 temp_history |
| 在线状态 | 通话期间 `AIPresence` 置忙：所有平台入站消息不处理、自动回复「正在通话中」系统提示（§5.5）；proactive/followup 全局抑制 | 通话结束恢复；绕过 fast_llm 忙碌路径，避免通话中收到文字回复 |
| 成本 | `core/cost_tracker.py` `log_usage()` | 音频 token 按 25/s 换算估算即可满足监控粒度 |
| Telegram | adapter 启动时 `setChatMenuButton` 配 `/rtc` 入口；`/rtc` 命令与 `[RTC]` 标记共用 invitation.py 出按钮 | 按钮链接拼 `public_url` + initData 由 webview 自动注入，前端无需处理 |
| 前脑标记 | `[RTC:理由]` 新标记（见 §10） | 遵守"新增前脑标记"全套约定：routing.py 两份解析器都改、sanitize 兜底剥离、front_brain.jinja 说明、tests/test_routing.py 补测试；**三层透传坑**（routing → front_brain return dict → 调用方），漏一层静默失效 |

**开放问题（实施 P1 前拍板）：**
① 通话记录入库 platform/chat_id——本文按「复用 telegram 私聊 scope + `[RTC通话]` 前缀」设计，
让文本侧把电话视作同一对话的延续；实施时与 `core/conversation_identity.py` 对齐确认。
② 开场注入的上下文量——建议 ≤2k token 摘要式注入（Live 每轮重算全上下文）。
③ 桌面 Chrome 调试直连 `http://localhost` 可跳过 initData/token（loopback 特权），方便
开发——仅限 `127.0.0.1`。
④ ~~视频计时实测~~ **已关闭（P0 实测）**：开过视频后连接存活 158.5s > 120s 预算线，
"开过即全程 2 分钟"不成立；实现上仍显示倒计时引导短时使用，超限由 goAway 兜底。

---

## 9. 前端（rtc/web/，无构建直发）

技术栈：原生 ES modules + AudioWorklet（无框架依赖，无构建步骤，`rtc/web/` 即产物）。

核心模块（参照官方 `google-gemini/live-api-web-console` 样板，其音频管线可直接借鉴）：
- **采集**：`getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}})`
  → `AudioContext({sampleRate:16000})` → AudioWorklet Float32→Int16 → 128ms/包 → WS binary。
  麦克风流**长活**，静音 = 前端停止发送（发 control:mute 同步状态），**绝不 stop track**
  （Android WebView 每次重启都弹权限框）
- **视频**：独立的 `getUserMedia({video:...})` 按需开关；canvas 抓帧 → JPEG（质量适中，
  如 0.6）→ base64 JSON ≤1 FPS。视频开着时 UI 显示 2 分钟预算倒计时，引导"看完就关"
- **播放**：AudioBufferSourceNode 队列，24kHz，320ms/块，100ms 预缓冲 + 0.2s look-ahead；
  `interrupted`/`state:rotating` 时清队列
- **字幕**：`transcript` 消息缓冲去抖后渲染（转写与音频无时序保证，不做逐字对齐）
- **UI**：连接状态（connecting/active/rotating/ending）、实时字幕、静音/视频/挂断按钮、
  时长与剩余提示（开视频切 2 分钟预算提示）、麦克风权限失败引导页
- **入口参数**：`?backend=`（默认同源）+ `?dev=1`（跳过鉴权，仅配合 loopback）
- **TG 集成**：`window.Telegram.WebApp` 存在时用其主题色/全屏；`expand()` + 关闭前确认

---

## 10. 发起通话（双端发起，汇合到 invitation.py）

**现实约束：Nora 无法真正"拨打"**——bot 主动只能发消息。所以「Nora 发起」的真实语义是
**发一条带 WebApp 按钮的邀请消息**，通话在你点下按钮那一刻开始。

### 10.1 用户发起

- `/rtc` 命令（或 bot 菜单按钮）→ adapter 调 invitation.py 生成邀请消息：
  正文一句话 + InlineKeyboard `web_app` 按钮（URL = `public_url`，可带 `?backend=`）
- 等价入口：菜单按钮（`setChatMenuButton` 启动时配置）

### 10.2 Nora 发起（`[RTC:理由]` 前脑标记）

- 前脑在主对话轮输出 `[RTC:理由]`（模式对齐 `[DRAW:]`：即时意愿表达类旁路标记）
- 生效条件：私聊 + owner + rtc 子系统已启用 + 当前无通话；群聊/审查轮/主动消息轮
  **只剥离不生效**（与 `[DRAW]` 的"仅主对话轮"同构）
- 触发后：文字照常发送（剥离标记）+ **追加**邀请按钮消息（复用 invitation.py 同一函数）
- **邀请管理**（invitation.py）：同一用户同时只挂一个有效邀请；TTL 过期（默认 5 分钟，
  超时按钮点了也提示已过期）；Nora 侧去重冷却（如 10 分钟内不重复邀约），防止反复邀约变骚扰
- 未来延伸（P4+）：ProactiveScheduler 产出类型加"通话邀请"——Nora"想给你打电话"，
  本质是主动消息多一种按钮形态

### 10.3 标记实现清单（照 CONVENTIONS §6「添加新前脑标记」全套）

1. `core/routing.py` 定义 `[RTC:...]` 正则常量
2. **两个解析器都改**：`parse_front_brain_response()` 与 `parse_front_brain_review()`
3. `sanitize_adapter_output_text()` 兜底剥离
4. `brain/templates/front_brain.jinja` 写用法说明（`{% if rtc_enabled %}` 包住）
5. `tests/test_routing.py` 补测试：正常解析 + 标记缺失 + 审查轮剥离 + 与既有标记共存
6. 三层透传：routing → front_brain return dict（`rtc_invite` 字段）→ 调用方

---

## 11. 分期实施

| 期 | 内容 | 验收 |
|---|---|---|
| **P0 骨架验证** | ✅ **已完成（2026-08-27）**：live_client.py 跑通 setup/双向音频/双侧转写/视频帧（`scripts/rtc_smoke.py`）；测试页 + 最小桥（`scripts/rtc_p0_server.py` + `rtc/web/test.html`）本机联调通过；视频计时实测完成（开放问题④ 已关闭，见 §3）；踩出 4 个协议坑已回写 §3 | 后端冒烟全绿；真机麦克风验收留给 P1 验收一起做 |
| **P1 MVP** | ✅ **已实现（2026-08-27），真机验收中**：server.py（hello/welcome 握手+版本校验+静态托管+healthz）、auth.py（initData HMAC + 静态 token 双轨 + 失败限速）、session.py（状态机+时长硬上限+单并发）、rtc/web/index.html 正式前端（TG WebApp 主题/静音/时长显示/播放队列清空）、prompt.py（身份上下文+通话指令+rules.md）、invitation.py + `/rtc` 命令 + 菜单按钮 + main.py 随主进程启动；自动化测试 51 个全过。**注意**：验收用的纯音测试不触发 VAD turn 结束（连续音无静音段），必须真人说话 | 真机（Android TG + iOS TG + 桌面 Chrome）各通一通 3 分钟电话 |
| **P2 记忆闭环** | ✅ **已实现（2026-08-29）**：双侧转写按 turn 聚合 + 视频段录制/关键帧总结（每 5 张一批送 image 模型）+ fast 总总结回写 message_history + 原始转写落镜像库 + 字幕整句显示 + TTS 音色复用（voice_source=tts，fish_audio 流式会话）。**未做**：通话独占的系统提示拦截（history_writer 注记）；AIPresence 联动；cost_tracker 入账 | 通完电话后文本侧继续聊，Nora 知道电话内容和用户展示过的画面 |
| **P3 工具** | tools_bridge 白名单 + 取消竞态 + 工具执行状态推送 | 通话中「帮我定个闹钟」能执行并口头确认 |
| **P4 打磨与扩展** | 连接轮换/重连健壮性（弱网模拟）、成本日上限、`[RTC]` Nora 主动邀约（含 proactive 调度延伸）、视频输入（前端视频 UI + 2 分钟预算） | 弱网 10 分钟+通话不断档；Nora 能主动邀约；短时视频可见 |
| **P5 可选** | 多用户闸门（token 签发/吊销）、half-cascade 自定义音色评估 | — |

P0 前置依赖：`rtc/config.json`（enabled/api_key/public_url）、服务器反代 TLS 配好 `/rtc/` 路径。

---

## 12. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| Live API Preview：模型 ID 再退役（2.0/2.5 系已退役过一轮） | 中 | 模型 ID 收敛在 config + live_client 单处；抽象层隔离协议 |
| iOS Safari/Telegram 录音 bug（官方样板也有） | 中 | P0 首项即真机冒烟；不行则 iOS 引导用文字/TTS 条，不硬扛 |
| 成本失控（音频持续烧 + 每轮重算上下文） | 中 | 单通硬上限 + 日预算 + contextWindowCompression 必开 |
| 视频计时未知（可能开过即全程 2 分钟） | 低 | 最保守假设设计 + P0 实测；UI 引导短时使用 |
| 每次连接轮换丢失上文（resumption 窗口错过） | 低 | goAway 主动轮换（不等 ABORTED）；轮换失败带 handle 重试 |
| Android WebView 重复弹权限 | 低 | 麦克风流长活 + 静音开关设计（§9） |
| 弱网音频卡顿 | 低 | 24k PCM 播放队列缓冲；必要时后端攒包平滑发送 |
| `[RTC]` 邀约骚扰化 | 低 | TTL + 去重冷却 + 仅主对话轮生效 |
