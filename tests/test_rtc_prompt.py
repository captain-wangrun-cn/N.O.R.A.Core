"""RTC 提示词组装测试（P1）。"""

from rtc.prompt import RTC_STYLE_INSTRUCTION, build_system_instruction


class TestBuildSystemInstruction:
    def test_identity_plus_style(self):
        text = build_system_instruction("身份上下文内容")
        assert text.startswith("身份上下文内容")
        assert "通话模式" in text

    def test_empty_identity_still_has_style(self):
        text = build_system_instruction("")
        assert RTC_STYLE_INSTRUCTION in text
        assert not text.startswith("\n")

    def test_recent_conversation_injected_between(self):
        text = build_system_instruction("身份", recent_conversation="<通话前的最近对话>\n主人: 嗨\n</通话前的最近对话>")
        # 顺序：身份 → 最近对话 → 通话指令
        assert text.index("身份") < text.index("通话前的最近对话") < text.index("通话模式")

    def test_no_markdown_markers_in_style_block(self):
        # 通话指令块禁 markdown / 输出类文本标记。
        # 注意：指令里【可以】提到 [SPLIT] 这个词（告诉模型忘掉打字习惯），
        # 但不能带 [SPLIT:数字] 延时语法和输出协议标记
        assert "[SPLIT:" not in RTC_STYLE_INSTRUCTION
        assert "[reply:" not in RTC_STYLE_INSTRUCTION
        assert "[VOICE]" not in RTC_STYLE_INSTRUCTION
        assert "```" not in RTC_STYLE_INSTRUCTION


class TestVoiceGuidanceInjection:
    def test_live_mode_no_guidance(self):
        """live 模式（默认）：不注入 TTS 标记指南。"""
        text = build_system_instruction("身份", voice_source="live")
        assert "语音标记" not in text

    def test_tts_mode_injects_guidance(self, monkeypatch):
        """tts 模式：注入 provider 的 get_text_guidance（含音素标记）。"""
        from tts.base import BaseTTSProvider

        class _P(BaseTTSProvider):
            name = "fake"

            def get_text_guidance(self):
                return "音素标记：<|phoneme_start|>chong2<|phoneme_end|>"

            async def synthesize(self, text):
                return {}

        import tts.registry as reg

        monkeypatch.setattr(reg, "build_tts_provider", lambda: _P({}))
        text = build_system_instruction("身份", voice_source="tts")
        assert "phoneme_start" in text
        assert "语音标记说明" in text

    def test_tts_mode_no_provider_no_crash(self, monkeypatch):
        """provider 缺失：不注入、不抛（通话照常）。"""
        import tts.registry as reg

        monkeypatch.setattr(reg, "build_tts_provider", lambda: None)
        text = build_system_instruction("身份", voice_source="tts")
        assert "语音标记" not in text

    def test_fish_audio_guidance_has_phoneme_examples(self):
        """内置 fish_audio 指南包含中/英/日三语发音控制示例。"""
        from tts.fish_audio.provider import Provider

        text = Provider({"api_key": "x"}).get_text_guidance()
        assert "phoneme_start" in text
        assert "chong2" in text          # 中文 tone3 拼音
        assert "R IY1 D" in text         # 英文 CMU Arpabet
        assert "ha1shi0ga0" in text      # 日文音高数字
