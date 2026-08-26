# TTS Provider 开发指南

本指南面向在 `tts/` 下新增语音合成 provider 的开发者。结构与 `adapters/` 同构：**tts/ 目录既是 TTS 子系统，也是 provider 容器**——内置的 `fish_audio` 和你自己写的 provider 平级放在 `tts/<name>/`。

## 1. 组成

一个 provider = 一个文件夹：

```text
tts/<name>/
├─ provider.py          # 必需：定义 Provider(BaseTTSProvider)
├─ config.json          # 你本地编辑的连接配置（gitignore，不入库）
└─ config.example.json  # 配置模板（入库，占位值用 YOUR_XXX）
```

子系统文件（不要动）：

- `tts/base.py`：`BaseTTSProvider` 基类。
- `tts/registry.py`：扫描 + importlib 加载 + 工厂。
- `tts/config_utils.py`：provider 目录 config.json 读取（仿 `adapters/config_utils.py`）。

## 2. 必需接口

`provider.py` 里定义 `Provider(BaseTTSProvider)`（类名叫 `Provider` 可消除歧义；用别的名字也行）：

```python
from tts.base import BaseTTSProvider


class Provider(BaseTTSProvider):
    name = "my_tts"  # 与文件夹名一致（不一致时以文件夹名为准并告警）

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.api_key = str(self.cfg.get("api_key", "") or "").strip()
        if not self.api_key:
            raise ValueError("my_tts 缺少 api_key")

    async def synthesize(self, text: str) -> dict:
        # 调上游 API……
        return {"bytes": audio_bytes, "ext": ".oga", "mime_type": "audio/ogg"}
```

### synthesize(text) -> dict

- 入参 `text`：要念的文本，**可能携带你自己在 get_text_guidance 里声明的情绪/副语言标记**，原样交给上游即可。
- 返回 `{"bytes": bytes, "ext": str, "mime_type": str}`：
  - `.oga`（Ogg/Opus）→ 两端都显示为**语音条**（Telegram send_voice / OneBot record 段）
  - `.mp3` → Telegram 显示为音频卡片
- **失败必须 raise**（`RuntimeError`）。不要学 LLM provider 的“返回错误文本”约定——TTS 产物是二进制，调用方是后台任务 Mixin，异常即失败、只记日志。

### get_text_guidance() -> str

给前脑 Nora 的 `[VOICE]` 文本写法指南，会注入前脑系统提示词。写清楚：

- 你的上游支持什么特殊标记（情绪、停顿、拟声、SSML……）与写法示例
- 注意事项（每句标记数上限、标记不会被念出等）

参考 `tts/fish_audio/provider.py` 的实现（S2 系模型的方括号情绪标记速查）。无特殊语法就返回空串。

### 可选

- `health_check() -> str`：启动自检，返回问题描述；空串健康。
- 构造函数里抛 `ValueError` 表示配置缺失/非法（registry 会把它透传给调用方）。

## 3. 配置约定

- 连接参数（api_key / endpoint / 声音 id / 格式偏好）**全部放你自己文件夹的 config.json**，提供 config.example.json 模板。不要把 key 写进全局 config.yml 或源码。
- 加载顺序：`config.json` 覆盖 `config.example.json` 覆盖 defaults——example 里除了占位值还可以带说明性默认值，缺 config.json 时按 example 起步。
- 全局 `config.yml` 的 `tts:` 块只有 `provider: "<文件夹名>"`，负责选择启用哪个。

## 4. 加载与安全

- registry（`tts/registry.py`）按需用 `importlib` 加载 `tts/<name>/provider.py`：优先取类名 `Provider` 的 `BaseTTSProvider` 子类；没有则取第一个（多个时 warning）。
- 单个 provider 加载失败只 log + 跳过，不影响主程序启动。
- ⚠️ provider 代码在 NORA 进程内执行，拥有与主程序相同的权限。**只放自己写的或审阅过的代码**——这个目录不做任何沙箱隔离。
- 下划线 / 点开头的文件夹与 `__pycache__` 会被跳过，可以用来放模板和素材。

## 5. 启用一个 provider

1. 复制 `tts/_template/` 为 `tts/<你的名字>/`（或直接手写三个文件）。
2. 编辑该文件夹的 `config.json` 填真实 key。
3. 全局 `config.yml` 写：

```yaml
tts:
  provider: "你的名字"
```

4. 重启 Nora。前脑会自动获得 `[VOICE]` 标记能力与你的写法指南。

## 6. 自检清单

- [ ] 文件夹名 = 类的 `name` 属性 = config.yml 里的 `provider` 值。
- [ ] `synthesize` 失败路径全部 raise，没有返回错误文本的分支。
- [ ] 返回的 `ext` 与上游音频实际容器一致（谎报扩展名会导致平台发送失败）。
- [ ] `get_text_guidance` 里的示例不会包含 `[VOICE]` / `[/VOICE]` 字样（会被当成真标记剥离）。
- [ ] config.example.json 入库、config.json 已被 gitignore。
- [ ] 网络调用走 aiohttp（异步），超时有上限；需要代理时用 `trust_env=True` 或读 cfg 里的 proxy。
