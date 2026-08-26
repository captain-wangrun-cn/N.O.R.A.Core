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

from typing import Any, Dict


class BaseTTSProvider:
    """语音合成 provider 抽象基类。

    子类必须：
    - 设置类属性 ``name``（与文件夹名一致，registry 按它注册）。
    - 实现 ``synthesize(text)``，失败 **raise**（不要返回错误文本——TTS 产物是
      二进制，调用方是任务 Mixin，异常即失败）。

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
