'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-27 00:00:00
Copyright © WR（captain-wangrun-cn）All rights reserved
'''
"""TTS provider 基类 — 所有语音合成 provider 的统一接口。

一个 provider = 一个文件夹（内置在 tts/providers/<name>/，自定义在 tts_providers/<name>/）：

    <name>/
    ├─ provider.py          # 必需：定义 Provider(BaseTTSProvider)
    ├─ config.json          # 用户本地编辑的连接配置（gitignore，不入库）
    └─ config.example.json  # 配置模板

编写文档见 tts/TTS_GUIDE.md。
"""

from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional


class BaseTTSProvider:
    """语音合成 provider 抽象基类。

    子类必须：
    - 设置类属性 ``name``（与文件夹名一致，registry 按它注册）。
    - 实现 ``synthesize(text)``，失败 **raise**（不要返回错误文本——TTS 产物是
      二进制，调用方是任务 Mixin，异常即失败）。

    可选：
    - 实现 ``open_live_session(on_audio)`` 给 RTC 实时通话提供低延迟流式语音
      （见下方 docstring；未实现时 RTC 该通话回落 Live 模型内置音色）。

    配置通过 ``__init__(self, cfg)`` 的 cfg 注入：registry 读 provider 目录下的
    config.json（缺失时回退 config.example.json 合并默认值），子类不要自己去读
    全局 config.yml。
    """

    #: provider 名，必须与所在文件夹名一致
    name: str = ""

    def __init__(self, cfg: Dict[str, Any] | None = None):
        self.cfg: Dict[str, Any] = dict(cfg or {})

    async def synthesize(self, text: str) -> Dict[str, Any]:
        """把文本合成为语音。

        Args:
            text: 要念的文本。可能携带 provider 自己的副语言/情绪标记
                  （见 get_text_guidance），子类原样交给上游 API 即可。

        Returns:
            {"bytes": bytes, "ext": ".oga", "mime_type": "audio/ogg"}
            ext 决定落盘扩展名与平台发送形态（.oga → 语音条）。

        Raises:
            RuntimeError: 合成失败（网络 / 鉴权 / 配额 / 空音频）。
        """
        raise NotImplementedError(f"{type(self).__name__} 未实现 synthesize")

    def open_live_session(
        self,
        on_audio: Callable[[bytes], Awaitable[None]],
    ) -> Optional["BaseTTSLiveSession"]:
        """（可选）为 RTC 实时通话开一个流式语音会话。

        RTC（voice_source=tts）优先走这条路：Nora 的台词增量到达就喂进
        会话，provider 边收边合成、音频块一出来就回调 on_audio——低延迟
        （流式实现得好可到几百毫秒首字）。

        实现约定（具体怎么做由 provider 开发者决定——WebSocket 流式、
        逐句合成、本地模型推流……都行，只要满足语义）：
        - feed_text(text)：增量喂文本（转写的一个个碎片）
        - flush()：提示"到这里先出声"（句子边界；实现可以忽略）
        - end_turn()：这一轮说完了（收尾剩余文本全部合成完）
        - close()：通话结束/换源，释放资源（幂等）
        - 生命周期内 on_audio 可能从任意任务上下文调用，实现方需自行保证
          线程/任务安全；异常不要抛给调用方，自己吞掉打日志

        **基类默认返回 None = 未实现**——RTC 检测到 None 自动回落：该通话继续用
        Live 模型内置音色（`rtc/tts_downlink.py`）。不回落到逐句 `synthesize()`：
        它的产物是带容器的音频（.oga/.mp3）且采样率不定，前端 PCM16/24k 播放
        管线吃不了，逐句延迟也太差——任何已有 provider 零改动保持兼容。
        """
        return None

    def get_text_guidance(self) -> str:
        """给前脑的 [VOICE] 文本写法指南，注入 front_brain.jinja。

        写清楚这个 provider 支持的特殊标记（情绪、停顿、拟声等）、写法示例与
        注意事项。返回空串表示无特殊语法。指南里不要出现 [VOICE] 块标记本身
        的嵌套写法（会被当成真的标记剥离）。
        """
        return ""

    def health_check(self) -> str:
        """可选：启动自检，返回问题描述；空串表示健康。默认跳过。"""
        return ""


class BaseTTSLiveSession:
    """RTC 实时通话的 TTS 流式会话（open_live_session 的返回类型约定）。

    这是一个纯约定（duck typing）基类：provider 的实现不要求继承它，
    只要具备同名方法即可。提供这个类只是为了文档与 isinstance 检查方便。
    """

    async def feed_text(self, text: str) -> None:
        """增量喂文本（转写碎片）。实现应尽快让文本进入合成管线。"""
        raise NotImplementedError

    async def flush(self) -> None:
        """提示尽快出声（句子边界）。实现可以忽略。"""
        raise NotImplementedError

    async def end_turn(self) -> None:
        """本轮台词结束：把剩余缓冲全部合成完（on_audio 会继续被调）。"""
        raise NotImplementedError

    async def close(self) -> None:
        """会话结束，释放资源。幂等；必须允许在 on_audio 仍在途时调用。"""
        raise NotImplementedError
