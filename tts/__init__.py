"""TTS 语音合成子系统。

tts/ 目录既是子系统也是 provider 容器（与 adapters/ 同构）：
- base.py / registry.py / config_utils.py / TTS_GUIDE.md 是子系统文件；
- 每个 tts/<name>/ 文件夹是一个 provider（内置 fish_audio，模板 _template）。
"""

from tts.base import BaseTTSProvider

__all__ = ["BaseTTSProvider"]
