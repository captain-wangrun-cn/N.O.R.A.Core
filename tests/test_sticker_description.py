'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-17 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""表情包描述轮（fast-image）必须是客观分析，不能带人设/无关上下文。

背景：这一轮的输出不是给用户看的话，而是被压成一行 `[表情包: ...]` 注入上下文的
**观察数据**——和 `view_media` 的分析轮同一性质。历史实现是一句内联 prompt
「用一句简短的中文描述这个表情包表达的情绪或含义」，两个毛病：

1. 只问"情绪或含义"、不问画面 → 用户问"这是什么内容/哪个系列的梗图"时答不上来；
2. 走 `_chat_stream_wrapper` → 被自动注入词库全局说明 + 系统环境信息
   （时间/ChatID/OS/Python 版本），对看图说一句话来说全是噪音，还会把无关上下文
   带进描述；加上没有身份声明，模型容易写成一句主观感想。
"""
import asyncio
import inspect

from brain.prompts import render_template
from core.back_brain import BackBrainMixin


def _system_prompt() -> str:
    return render_template('sticker_analysis.jinja', 'sticker_analysis_system')


# --- 模板本身 ---

def test_template_renders_both_blocks():
    assert _system_prompt().strip()
    assert render_template(
        'sticker_analysis.jinja', 'sticker_analysis_user', sticker_count=1
    ).strip()


def test_system_prompt_strips_persona():
    """必须显式声明没有人设，否则模型会用 Nora 的口吻回一句寒暄。"""
    text = _system_prompt()
    assert "不是" in text and "人设" in text
    assert "emoji" in text.lower()
    # 输出是系统内部数据这件事要写明，模型才不会把它当成对话
    assert "用户看不到" in text


def test_system_prompt_asks_for_visual_content():
    """核心诉求：要画面，不只要情绪。

    原实现只问"情绪或含义"，所以"这是哪个系列的梗图"这类问题答不上来。
    """
    text = _system_prompt()
    assert "画面" in text
    # 能认出的具体角色/作品要写出名字——这正是用户想问的那类信息
    assert "角色" in text
    assert "文字" in text  # 表情包上的字要转录


def test_system_prompt_forbids_guessing():
    """认不出就描述外观，不能猜名字——猜错比不知道更糟。"""
    text = _system_prompt()
    assert "不要猜" in text or "不要推测" in text or "禁止推测" in text


def test_system_prompt_forbids_tag_blocks_and_controls():
    """表情包不入图片库，不该输出 IMAGE_TAGS；也不能吐消息控制标记。"""
    text = _system_prompt()
    assert "[IMAGE_TAGS]" in text
    assert "[SPLIT]" in text


def test_user_prompt_singular_and_plural():
    one = render_template('sticker_analysis.jinja', 'sticker_analysis_user', sticker_count=1)
    many = render_template('sticker_analysis.jinja', 'sticker_analysis_user', sticker_count=3)
    assert "这个表情包" in one
    assert "共 3 个" in many


# --- 调用点 ---

def test_describe_stickers_uses_template_not_inline_prompt():
    source = inspect.getsource(BackBrainMixin._describe_stickers)
    assert "sticker_analysis.jinja" in source
    # 旧的内联 prompt 不能再残留
    assert "表情包描述助手" not in source


def test_describe_stickers_bypasses_chat_stream_wrapper():
    """不能走 _chat_stream_wrapper——它会注入词库说明和系统环境信息。

    那些内容（当前时间/Chat ID/操作系统/Python 版本）对"看图说一句话"毫无用处，
    而且正是它们把无关上下文带进了描述。

    只看代码体、剔掉 docstring：那段注释本身就要提这个名字解释为什么不用它。
    """
    source = inspect.getsource(BackBrainMixin._describe_stickers)
    doc = BackBrainMixin._describe_stickers.__doc__ or ""
    body = source.replace(doc, "")
    assert "_chat_stream_wrapper" not in body
    assert "chat_stream(" in body


def test_describe_stickers_sends_no_history_and_no_tools():
    """带历史会把分析轮拽回对话口吻；给工具则可能让它去调用工具而不是描述。"""
    source = inspect.getsource(BackBrainMixin._describe_stickers)
    assert "history=[]" in source
    assert "tools=[]" in source


# --- 行为 ---

class _FakeFastImageLLM:
    def __init__(self, reply="一只白猫张嘴大笑，用来表示认可。"):
        self.reply = reply
        self.calls = []

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type": "text", "content": self.reply}


class _Host(BackBrainMixin):
    def __init__(self, fast_image_llm=None):
        self.fast_image_llm = fast_image_llm


def _sticker(path="a.webp"):
    return {"path": path, "mime_type": "image/webp", "base64": "x", "is_sticker": True}


def test_describe_stickers_returns_description():
    llm = _FakeFastImageLLM()
    host = _Host(fast_image_llm=llm)
    desc = asyncio.run(host._describe_stickers([_sticker()]))
    assert desc == "一只白猫张嘴大笑，用来表示认可。"


def test_describe_stickers_passes_only_stickers():
    """真图不该混进这一轮——它走的是主图模型，不是这里。"""
    llm = _FakeFastImageLLM()
    host = _Host(fast_image_llm=llm)
    real_image = {"path": "photo.jpg", "mime_type": "image/jpeg", "base64": "y"}
    asyncio.run(host._describe_stickers([real_image, _sticker()]))
    passed = llm.calls[0]["multimodal_images"]
    assert len(passed) == 1
    assert passed[0]["is_sticker"] is True


def test_describe_stickers_system_prompt_has_no_env_noise():
    """回归锁：描述轮的 system prompt 里不能出现系统环境信息的标记。"""
    from core.controller import NoraController

    llm = _FakeFastImageLLM()
    host = _Host(fast_image_llm=llm)
    asyncio.run(host._describe_stickers([_sticker()]))
    sent = llm.calls[0]["system_prompt"]
    assert NoraController._SYSTEM_ENV_MARKER not in sent
    assert NoraController._LEXICON_GLOBAL_MARKER not in sent


def test_describe_stickers_without_fast_image_llm_returns_empty():
    host = _Host(fast_image_llm=None)
    assert asyncio.run(host._describe_stickers([_sticker()])) == ""


def test_describe_stickers_without_stickers_returns_empty():
    """没有表情包就别浪费一次模型调用。"""
    llm = _FakeFastImageLLM()
    host = _Host(fast_image_llm=llm)
    real_image = {"path": "photo.jpg", "mime_type": "image/jpeg", "base64": "y"}
    assert asyncio.run(host._describe_stickers([real_image])) == ""
    assert llm.calls == []


def test_describe_stickers_swallows_model_errors():
    """描述失败不能影响消息处理主链路。"""
    class _Boom:
        async def chat_stream(self, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    host = _Host(fast_image_llm=_Boom())
    assert asyncio.run(host._describe_stickers([_sticker()])) == ""
