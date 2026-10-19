"""Telegram 表情包落盘规格。

直接发（`incoming._handle_sticker`）和引用重下（`reply._redownload_replied_sticker`）
必须走同一份规则，否则两条路径会分叉。视觉侧能读的只有静态图片：

- 静态表情包：`.webp` 原样存。
- 视频表情包（`is_video`）：`.webm` 原样存，由 `brain.multimodal` 抽首帧后送视觉模型。
- 动画表情包（`is_animated`，`.tgs` Lottie）：视觉模型读不了，也抽不出帧；
  优先下载 Telegram 给的静态缩略图（通常就是首帧）存成 `.webp`。
"""

from __future__ import annotations

from typing import Tuple


def sticker_download_spec(sticker) -> Tuple[str, bool]:
    """返回 `(扩展名, 是否改用 Telegram 静态缩略图)`。"""
    if getattr(sticker, "is_video", False):
        return "webm", False
    if getattr(sticker, "is_animated", False):
        if getattr(sticker, "thumbnail", None) is not None:
            return "webp", True
        return "tgs", False
    return "webp", False


def sticker_download_source(sticker):
    """按 `sticker_download_spec` 选出真正要下载的文件对象。"""
    _, use_thumbnail = sticker_download_spec(sticker)
    if use_thumbnail:
        thumb = getattr(sticker, "thumbnail", None)
        if thumb is not None:
            return thumb
    return sticker
