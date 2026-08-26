# TTS 语音合成子系统

> 前脑 `[VOICE]...[/VOICE]` 标记 → tts provider 合成 → 落盘 → `[voice: path]` 追发。
> 文字先发、语音后到，与 `[DRAW:]` 旁路生图完全同构，不进后脑工具循环。

## 链路总览

```text
前脑回复带 [VOICE]要念的话[/VOICE]
  ↓ core/routing.py  parse_front_brain_response()
提取 voice_text + 从 user_reply 剥离标记（两个解析器都剥；sanitize 兜底）
  ↓ core/message_handler.py  3.10 块（send_front_reply 之后、presence_markers 之前）
start_voice_task(runtime_key, voice_text)
  ↓ core/speech_gen.py  SpeechGenMixin（后台 Task，per-key 只保留最新一条）
build_tts_provider() → provider.synthesize(text)
  ↓ 落盘 workspace/data/voice/YYYY-MM-DD/voice_<8hex>.oga
_send_platform_message(key, "[voice: abs_path]")
  ↓ adapters
Telegram: FILE_PATTERN 解析 [voice: path] → .oga → send_voice（语音条）
OneBot v11: _MEDIA_TAG_PATTERN 已含 voice → record 段（语音条）
```

## 标记语法：为什么是块格式

`[VOICE]...[/VOICE]`（仿 `[TASK_INSTRUCTION]`），而不是 `[VOICE:...]` 短格式：
provider 的情绪标记本身就是方括号（fish_audio 的 `[happy]` `[break]`），
短格式在第一个 `]` 截断会把它们切碎。短格式仅作兜底提取（模型习惯性输出时不吞内容）。

- 主解析器提取 `voice_text`（块优先、短格式兜底、只取第一个、空标记忽略）
- 审查轮（`parse_front_brain_review`）只剥离不触发
- `sanitize_adapter_output_text()` 剥块/短/裸三种形态（覆盖未闭合残 token）
- 与 `[DRAW:]` / `[NEED_BACKEND]` 完全正交，可共存

语音文本进 Mixin 后会剥离系统标记（SPLIT/NEED_BACKEND/reply 等，逐个点名——
**不能**用"剥所有方括号"的宽逻辑，会误杀 provider 情绪标记），超 500 字符截断。

## Provider 体系：tts/ 既是子系统也是容器

与 `adapters/` 同构：内置的 `fish_audio` 和用户自写的 provider 平级放在 `tts/<name>/`。
这是仓库第一个 **importlib 动态加载用户代码**的先例（skills 是子进程隔离，不算）。

```text
tts/
├─ base.py            # BaseTTSProvider：synthesize / get_text_guidance / health_check
├─ registry.py        # 发现 + importlib 加载 + 工厂 + tts_available + get_active_tts_guidance
├─ config_utils.py    # provider 目录 config.json 读取（仿 adapters/config_utils.py）
├─ TTS_GUIDE.md       # provider 编写文档（仿 ADAPTER_GUIDE.md）
├─ fish_audio/        # 内置 provider
│  ├─ provider.py     #   Provider(BaseTTSProvider)
│  ├─ config.json     #   用户编辑（gitignore）
│  └─ config.example.json
└─ _template/         # 复制即用的模板（下划线开头，registry 跳过）
```

关键设计：

- **一个 provider 一个文件夹**，`provider.py` 里定义 `Provider` 子类（类名 `Provider`
  优先；其它名取第一个）。文件夹名 = provider 名。
- **配置跟随 provider 目录**：`config.json`（gitignore）+ `config.example.json`；
  加载顺序 config.json > config.example.json > defaults。全局 `config.yml` 的
  `tts:` 块只有 `provider: "<名>"`。`tts/*/config.json` 已进 .gitignore。
- **失败 raise 而非返回错误文本**——TTS 产物是二进制，调用方是任务 Mixin，
  异常即失败（区别于 LLM provider 的历史约定）。
- **get_text_guidance() 是 provider 内置提示词**：写清楚上游支持的情绪/停顿/副语言
  标记，`core/front_brain.py` 渲染时经 `voice_guidance` 注入前脑——Nora 写 `[VOICE]`
  内容时就知道能用 `[happy]` `[break]` 这些标记。
- 单个 provider 加载失败只 log + 跳过，不崩启动；下划线/点开头目录跳过。
- ⚠️ provider 代码进程内执行、无沙箱，只放自己写的或审阅过的代码。

## Fish Audio 契约（2026-08 实测自 openapi.json）

```text
POST {base_url}/v1/tts        默认 https://api.fish.audio
Authorization: Bearer <api_key>
Model: <model>                ← 模型经 header，不在 body（默认 s2.1-pro）
Content-Type: application/json

body: text / reference_id / format(opus|mp3|wav|pcm) / mp3_bitrate /
      latency(normal|balanced|low) / normalize / prosody{speed,volume}
200 → 音频二进制流；错误 → JSON {"status", "message"}
```

- `format: opus` 落盘 `.oga`（Ogg 容器）而非 `.opus`——Telegram 语音条
  （send_voice）的 voice 扩展集只有 `.oga`。
- 声音模型：fish.audio 网页创建，`GET /model?self=true` 可列出，id 即 `reference_id`。
- `proxy` 配置键：单独给这个 provider 指代理；留空 trust_env 继承全局 `network.proxy`。
- S2 系情绪标记（写进 get_text_guidance 注入前脑）：句首 `[happy]` `[sad]`…（支持
  自然语言 `[warm and happy]`、强度 `[very excited]`），语气/音效任意位置
  `[whispering]` `[laughing]` `[break]` `[long-break]`；每句 ≤3 种；标记不会被念出。

## 前脑提示词

`brain/templates/front_brain.jinja` 的 `{% if tts_enabled %}` 块（`tts_available()`
为真才注入，避免模型输出无法执行的标记）：

- 用法 + 异步语义（文字先发、语音几秒后追发）+ 单轮一条 + 独立于 NEED_BACKEND
- **主动发语音判据**：情绪浓/深夜亲密/长解释/用户反馈好 → 可主动；正事/用户在忙/
  文字党 → 文字。语音内容可与文字不同。
- `{{ voice_guidance }}` 注入当前 provider 的写法指南

## 触发点与生命周期

- 触发块在 `core/message_handler.py`：**必须**在 send_front_reply 块之后（用其
  `send_target.runtime_key`，语音追到文字实际去的目标）、`_apply_presence_markers`
  之前（presence_ended 提前 return）。投递目标被拒绝时不合成。
- `voice_gen_tasks` per-runtime_key 只保留最新一条（新请求 cancel 旧的）；
  `/stop` 与 `shutdown()` 单独 cancel（旁路任务不在 generation_tasks 里）。
- 失败只记日志，不向用户追发任何文本（对齐 `[DRAW:]` 约定）。

## 配置

```yaml
# config.yml
tts:
  provider: "fish_audio"   # = tts/ 下的文件夹名
```

连接参数全部在 `tts/fish_audio/config.json`（从 config.example.json 复制）。
CLI：`python cli.py configure` → 单项「🎤 语音合成 TTS」或向导中的 StepTTS。

## 相关文件

| 关注点 | 位置 |
|---|---|
| 标记正则与解析 | `core/routing.py`（`_VOICE_BLOCK_PATTERN` 等） |
| 触发块 | `core/message_handler.py`（3.10，紧随 DRAW 块） |
| 合成 Mixin | `core/speech_gen.py` |
| provider 基类/注册表/配置 | `tts/base.py` / `tts/registry.py` / `tts/config_utils.py` |
| 内置 provider | `tts/fish_audio/provider.py` |
| 编写文档 | `tts/TTS_GUIDE.md` |
| 前脑提示词 | `brain/templates/front_brain.jinja`（`{% if tts_enabled %}`） |
| Telegram 语音条 | `adapters/telegram/constants.py`（FILE_PATTERN 含 voice）+ `sender.py` send_voice |
| OneBot record 段 | `adapters/onebotv11/sender.py`（原本已支持） |
| 测试 | `tests/test_tts.py`、`tests/test_routing.py`（VOICE 用例）、`tests/test_front_brain_prompt.py` |
