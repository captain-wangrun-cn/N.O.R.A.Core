'''
统一媒体存储路径
=========
职责：
  按平台 + 场景 + chat_id + 媒体类型 统一组织媒体文件目录。

目录结构：
  <data_dir>/media/<platform>/<scene>/<chat_id>/<media_type>/<filename>

  platform  : telegram / onebotv11 / ...
  scene     : private / group / channel
  chat_id   : 会话 ID（私聊也为 user_id）
  media_type: image / video / record / file / sticker / document

兼容性：
  - 旧媒体文件落点为 data/telegram、data/onebotv11（扁平）；
  - 本模块只负责描述「新」目录布局，旧目录由 sender 兜底解析、由
    scripts/migrate_media_layout.py 迁移；
  - file_path 仍以相对 workspace root 的字符串写入 DB。
'''

from __future__ import annotations

import os
from typing import Optional

# 新旧平台目录名，用于迁移与兜底解析
PLATFORM_LEGACY_DIRS = {
    "telegram": "telegram",
    "onebotv11": "onebotv11",
}

# OneBot 段类型 -> 媒体类型子目录
SEGMENT_TO_MEDIA_TYPE = {
    "image": "image",
    "video": "video",
    "record": "record",
    "file": "file",
    "sticker": "sticker",
    "document": "document",
    "photo": "image",
}


def infer_scene(chat_type: str = "", chat_id: str = "", message_type: str = "") -> str:
    """
    由会话类型/chat_id 符号/OneBot message_type 推断场景。

    优先级：显式 chat_type > message_type > chat_id 符号推断。
    chat_id 以负号代表群聊（Telegram/OneBot 群号均为负数）。
    """
    ct = (chat_type or "").strip().lower()
    if ct in {"private", "group", "supergroup", "channel"}:
        if ct == "supergroup":
            return "group"
        return ct
    mt = (message_type or "").strip().lower()
    if mt in {"private", "group", "channel"}:
        return mt
    cid = str(chat_id or "").strip()
    if cid:
        try:
            if int(cid) < 0:
                return "group"
        except ValueError:
            pass
    return "private"


def safe_chat_id_part(chat_id: str, scene: str) -> str:
    """目录中使用的 chat_id 段，做安全化（去斜杠、非空兜底）。"""
    part = str(chat_id or "").strip().replace("/", "_").replace("\\", "_")
    if not part:
        # 私聊无 user_id 时也兜底，避免空目录段
        part = "unknown" if scene == "private" else "unknown_group"
    return part


def build_media_subdir(
    *,
    data_dir: str,
    platform: str,
    scene: str,
    chat_id: str,
    media_type: str,
) -> str:
    """
    构建新结构的媒体子目录绝对路径，并确保存在。

    返回形如 <data_dir>/media/<platform>/<scene>/<chat_id>/<media_type>
    """
    scene = scene or "private"
    media_type = media_type or "misc"
    parts = [
        data_dir,
        "media",
        platform,
        scene,
        safe_chat_id_part(chat_id, scene),
        media_type,
    ]
    target = os.path.join(*[p for p in parts if p])
    os.makedirs(target, exist_ok=True)
    return target


def build_media_relpath(
    *,
    workspace_root: str,
    abs_target_dir: str,
    filename: str,
) -> str:
    """将绝对目标目录 + 文件名转成相对 workspace root 的正斜杠路径。"""
    abs_file = os.path.join(abs_target_dir, filename)
    return os.path.relpath(abs_file, workspace_root).replace("\\", "/")


def resolve_legacy_data_dirs(data_dir: str, platform: str) -> Optional[str]:
    """返回该平台的旧扁平目录（若存在约定），便于兜底解析/迁移。"""
    legacy = PLATFORM_LEGACY_DIRS.get(platform)
    if not legacy:
        return None
    return os.path.join(data_dir, legacy)
