'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-27 00:00:00
Copyright © WR（captain-wangrun-cn）All rights reserved

TTS provider 模板 — 复制整个 _template/ 文件夹为 tts/<你的名字>/ 开始编写。
编写文档见 tts/TTS_GUIDE.md。
'''
from typing import Any, Dict

from tts.base import BaseTTSProvider


class Provider(BaseTTSProvider):
    name = "_template"

    def __init__(self, cfg: Dict[str, Any] | None = None):
        super().__init__(cfg)
        self.api_key = str(self.cfg.get("api_key", "") or "").strip()
        if not self.api_key:
            raise ValueError("_template 缺少 api_key（编辑本目录 config.json）")

    async def synthesize(self, text: str) -> Dict[str, Any]:
        # TODO: 调你的上游 TTS API，拿到音频 bytes。
        # 失败路径必须 raise RuntimeError，不要返回错误文本。
        raise RuntimeError("_template 是模板，未实现 synthesize")

    def get_text_guidance(self) -> str:
        # 写清楚你的上游支持的特殊标记（情绪/停顿/SSML…）与示例；无则返回 ""。
        return ""
