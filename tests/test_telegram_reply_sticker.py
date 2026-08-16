'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-16 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""引用（reply）表情包时，fast-image 识别链路必须照常跑起来。

背景：表情包**故意不进 ImageStore**，入库正文又被 media_placeholder_text
收敛成裸 "[表情包]"，所以历史里没有任何路径可还原。唯一能驱动
extract_image_payloads → _describe_stickers 的是 [sticker: path] 标记，
`_extract_reply_info` 必须重下文件把它重建出来。
"""
import asyncio
import inspect
import os

import pytest

from adapters.telegram.reply import TelegramReplyMixin
from brain.multimodal import extract_image_payloads, get_sticker_images


# 一张真实可解码的 1x1 PNG（内容只需能被读出来；mime 按扩展名判定）
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class _FakeFile:
    def __init__(self, dest_recorder):
        self._recorder = dest_recorder

    async def download_to_drive(self, path):
        self._recorder.append(path)
        with open(path, "wb") as f:
            f.write(PNG_1X1)


class _FakeSticker:
    def __init__(self, file_id="ABC", emoji="🐱", set_name="CatPack", is_video=False):
        self.file_id = file_id
        self.emoji = emoji
        self.set_name = set_name
        self.is_video = is_video
        self.downloads = []

    async def get_file(self):
        return _FakeFile(self.downloads)


class _FakeChat:
    def __init__(self, chat_id="123", chat_type="private"):
        self.id = chat_id
        self.type = chat_type


class _FakeMessage:
    def __init__(self, sticker=None, chat=None):
        self.sticker = sticker
        self.chat = chat or _FakeChat()
        self.photo = None
        self.text = None
        self.caption = None
        self.message_id = 999
        self.reply_to_message = None


class _StickerReplyAdapter:
    """只挂上被测的两个方法，避免拉起整个 TelegramAdapter。"""

    _redownload_replied_sticker = TelegramReplyMixin._redownload_replied_sticker
    _redownload_replied_sticker_as_input = TelegramReplyMixin._redownload_replied_sticker_as_input

    def __init__(self, workspace_root, data_dir):
        self.workspace_root = workspace_root
        self.data_dir = data_dir
        self.current_chat_id = "123"


@pytest.fixture
def adapter(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return _StickerReplyAdapter(str(tmp_path), str(data_dir))


def test_replied_sticker_rebuilds_sticker_tag(adapter):
    """核心断言：产出必须含 [sticker: path]，否则 fast-image 链路永远不触发。"""
    msg = _FakeMessage(sticker=_FakeSticker())

    result = asyncio.run(adapter._redownload_replied_sticker_as_input(msg))

    assert result is not None
    assert "[sticker:" in result
    # emoji/贴纸包那行给模型看语义，路径那行才是载荷——两行都要有
    assert "🐱 from CatPack" in result
    assert result.count("[sticker:") == 2


def test_rebuilt_tag_yields_sticker_payload_for_fast_image(adapter):
    """端到端：重建的标记要能被 extract_image_payloads 解析出 is_sticker 负载。

    这里把相对路径换成绝对路径再解析：`_resolve_local_image_path` 只认项目根 /
    cwd / 真实 workspace，认不出 pytest 的 tmp_path。相对路径在生产里由
    workspace 解析（和 `_handle_sticker` 完全同一条路），本测试要验证的是
    「这个标记格式能产出 is_sticker 负载」，与路径基准无关。
    """
    msg = _FakeMessage(sticker=_FakeSticker())
    tag = asyncio.run(adapter._redownload_replied_sticker_as_input(msg))

    rel = tag.rsplit("[sticker: ", 1)[1].rstrip("]")
    abs_webp = os.path.join(adapter.workspace_root, rel)
    # .webp 在部分环境没注册 mime，改名成 .png 以确保 mimetypes 认得
    abs_png = abs_webp[: -len(".webp")] + ".png"
    os.replace(abs_webp, abs_png)
    tag = tag.replace(rel, abs_png.replace("\\", "/"))

    # 模拟真实入站文本：标记被包在 [回复: ...] 里
    text = f"[回复: {tag}]\n这个表情包什么意思"
    clean, images = extract_image_payloads(text)
    stickers = get_sticker_images(images)

    assert len(stickers) == 1
    assert stickers[0]["is_sticker"] is True
    assert stickers[0]["bytes"]
    # 标记被剥离后，用户那句话仍要留在正文里
    assert "这个表情包什么意思" in clean


def test_replied_sticker_reuses_existing_file(adapter):
    """同 file_id 不该重复下载（对齐 _redownload_replied_photo 的行为）。"""
    sticker = _FakeSticker()
    msg = _FakeMessage(sticker=sticker)

    first = asyncio.run(adapter._redownload_replied_sticker_as_input(msg))
    assert len(sticker.downloads) == 1

    second = asyncio.run(adapter._redownload_replied_sticker_as_input(msg))
    assert second == first
    assert len(sticker.downloads) == 1, "文件已存在时不应再次下载"


def test_video_sticker_uses_webm_extension(adapter):
    msg = _FakeMessage(sticker=_FakeSticker(is_video=True))
    result = asyncio.run(adapter._redownload_replied_sticker_as_input(msg))
    assert result.endswith(".webm]")


def test_missing_sticker_returns_none(adapter):
    """非表情包消息必须干净地返回 None，让调用方退回原有兜底。"""
    assert asyncio.run(adapter._redownload_replied_sticker_as_input(_FakeMessage())) is None


def test_download_failure_returns_none_instead_of_raising(adapter):
    """下载失败不能把整条入站链路带崩。"""

    class _BrokenSticker(_FakeSticker):
        async def get_file(self):
            raise RuntimeError("network down")

    msg = _FakeMessage(sticker=_BrokenSticker())
    assert asyncio.run(adapter._redownload_replied_sticker_as_input(msg)) is None


def test_sticker_without_emoji_metadata_still_works(adapter):
    """emoji / set_name 可能缺失，缺省值不能让格式塌掉。"""
    msg = _FakeMessage(sticker=_FakeSticker(emoji=None, set_name=None))
    result = asyncio.run(adapter._redownload_replied_sticker_as_input(msg))
    assert "[sticker: 🎨 from 未知贴纸包]" in result


def test_rebuilt_tag_matches_direct_sticker_format():
    """格式必须和 _handle_sticker 逐字一致，否则两条路径的行为会分叉。"""
    from adapters.telegram import incoming

    direct = inspect.getsource(incoming.TelegramIncomingMixin._handle_sticker)
    rebuilt = inspect.getsource(TelegramReplyMixin._redownload_replied_sticker_as_input)
    assert "[sticker: {emoji} from {set_name}]" in direct
    assert "[sticker: {emoji} from {set_name}]" in rebuilt


def test_extract_reply_info_checks_sticker_before_history():
    """顺序很关键：历史查找会先返回裸 "[表情包]"，把 sticker 分支挡掉。"""
    source = inspect.getsource(TelegramReplyMixin._extract_reply_info)
    sticker_pos = source.index("_redownload_replied_sticker_as_input")
    history_pos = source.index("get_context_messages")
    assert sticker_pos < history_pos


def test_sticker_probe_tolerates_messages_without_sticker_attr():
    """这条链路会收到精简过的消息对象（SimpleNamespace / 其它 adapter 的伪造消息）。

    直接写 `reply_msg.sticker` 会在这些对象上 AttributeError，把整条入站解析带崩——
    `tests/test_telegram_adapter_modules.py` 里的 reply 测试就是这种形状。
    """
    source = inspect.getsource(TelegramReplyMixin._extract_reply_info)
    assert 'getattr(reply_msg, "sticker", None)' in source
    assert "if reply_msg.sticker:" not in source
