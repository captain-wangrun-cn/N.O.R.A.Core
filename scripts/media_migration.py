'''
media_migration.py
==================
将旧的扁平媒体目录（data/telegram、data/onebotv11）迁移到按
  平台 / 场景 / chat_id / 媒体类型
分层的新结构：
  data/media/<platform>/<scene>/<chat_id>/<media_type>/<filename>

迁移策略（与优先选择对齐）：
  - 仅更新 MongoDB 中的 file_path，不回填 scene / media_type 字段。
  - 按 file_path 反查 MongoDB 取回 chat_id、platform；
    scene 由 chat_id 符号/平台约定推断（负数=群）。
  - 找不到 DB 记录的文件保留原位（不动），避免无主文件被错误归档。
  - 幂等：通过 marker 文件（data/.media_migrated_v1）记录已迁移；
    已经迁移过则直接跳过，后续启动零开销。
  - 迁移后旧目录若为空则删除；非空保留（仍有未识别文件）。
'''

from __future__ import annotations

import logging
import os
import shutil
from typing import Iterable, Optional

from adapters.media_path import build_media_subdir, infer_scene

logger = logging.getLogger(__name__)

MIGRATION_MARKER = ".media_migrated_v1"


def _load_image_store():
    """惰性加载 ImageStore；失败返回 None（迁移降级为仅扫盘不查库）。"""
    try:
        from memory.image_store import ImageStore
        return ImageStore()
    except Exception as exc:  # pragma: no cover - 配置/依赖缺失时降级
        logger.warning("[media_migration] ImageStore 不可用，跳过迁移: %s", exc)
        return None


def _new_relpath_for(
    *,
    data_dir: str,
    workspace_root: str,
    platform: str,
    chat_id: str,
    media_type: str,
    filename: str,
) -> tuple[str, str]:
    target_dir = build_media_subdir(
        data_dir=data_dir,
        platform=platform,
        scene=infer_scene(chat_id=chat_id),
        chat_id=chat_id,
        media_type=media_type,
    )
    rel = os.path.relpath(os.path.join(target_dir, filename), workspace_root).replace("\\", "/")
    return target_dir, rel


def _media_type_from_filename(filename: str) -> str:
    """根据文件名粗分类（photo_/video_/sticker_/document_ 前缀或扩展名）。"""
    name = filename.lower()
    if name.startswith("photo_") or name.endswith((".jpg", ".jpeg", ".png", ".webp")) and not name.startswith("sticker_"):
        return "image"
    if name.startswith("video_") or name.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
        return "video"
    if name.startswith("sticker_"):
        return "sticker"
    if name.endswith((".mp3", ".wav", ".m4a", ".ogg", ".amr", ".silk")):
        return "record"
    if name.startswith("document_"):
        return "document"
    return "file"


def _iter_files(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        return []
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            out.append(os.path.join(dirpath, fn))
    return out


def _migrate_platform(
    *,
    data_dir: str,
    workspace_root: str,
    platform: str,
    legacy_dir: str,
    image_store,
) -> dict:
    stats = {"scanned": 0, "migrated": 0, "skipped_no_record": 0, "already_new": 0, "errors": 0}
    rel_root = os.path.relpath(legacy_dir, workspace_root).replace("\\", "/")
    for abs_file in _iter_files(legacy_dir):
        stats["scanned"] += 1
        try:
            rel_file = os.path.relpath(abs_file, workspace_root).replace("\\", "/")
            # 已经是 media/<platform>/... 新结构的跳过（理论上不会出现在旧目录里）
            if rel_file.startswith(f"data/media/{platform}/"):
                stats["already_new"] += 1
                continue

            chat_id = ""
            media_type = ""
            doc_platform = platform

            if image_store is not None and image_store.mongo_col is not None:
                try:
                    doc = image_store.mongo_col.find_one({"file_path": rel_file}, {"_id": 0})
                except Exception:
                    doc = None
                if doc:
                    chat_id = str(doc.get("chat_id") or doc.get("storage_id") or doc.get("user_id") or "")
                    media_type = doc.get("media_type") or ""
                    doc_platform = str(doc.get("platform") or platform) or platform

            filename = os.path.basename(abs_file)
            if not media_type:
                media_type = _media_type_from_filename(filename)
            if not chat_id:
                # 无 DB 记录也无法从文件名推断归属：保留原位
                stats["skipped_no_record"] += 1
                continue

            target_dir, new_rel = _new_relpath_for(
                data_dir=data_dir,
                workspace_root=workspace_root,
                platform=doc_platform or platform,
                chat_id=chat_id,
                media_type=media_type,
                filename=filename,
            )
            if rel_file == new_rel:
                stats["already_new"] += 1
                continue

            new_abs = os.path.join(target_dir, filename)
            if os.path.isfile(new_abs):
                # 目标已存在（同名），跳过物理移动避免覆盖
                stats["already_new"] += 1
                continue
            shutil.move(abs_file, new_abs)

            if image_store is not None and image_store.mongo_col is not None:
                try:
                    image_store.mongo_col.update_many(
                        {"file_path": rel_file},
                        {"$set": {"file_path": new_rel}},
                    )
                except Exception as exc:
                    logger.warning("[media_migration] 更新 file_path 失败 rel=%s: %s", rel_file, exc)
                    stats["errors"] += 1
                    continue
            stats["migrated"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[media_migration] 处理文件失败 %s: %s", abs_file, exc)
            stats["errors"] += 1

    # 旧目录迁空后清理
    if os.path.isdir(legacy_dir):
        remaining = _iter_files(legacy_dir)
        if not remaining and os.path.relpath(legacy_dir, workspace_root).replace("\\", "/").startswith("data/"):
            try:
                shutil.rmtree(legacy_dir, ignore_errors=True)
            except Exception:
                pass
    return stats


def migrate_media_layout_once(
    *,
    data_dir: str,
    workspace_root: str,
    platforms: Iterable[tuple[str, str]],
    marker_dir: Optional[str] = None,
) -> bool:
    """
    幂等迁移：已迁移过（marker 存在）则直接返回 False 不干活。

    Args:
        data_dir: workspace data 目录
        workspace_root: workspace 根目录
        platforms: [(platform_name, legacy_dir_abs), ...]
        marker_dir: marker 文件放哪，默认 data_dir

    Returns:
        True 表示本次执行了迁移；False 表示已迁移过被跳过。
    """
    marker_dir = marker_dir or data_dir
    marker_path = os.path.join(marker_dir, MIGRATION_MARKER)
    if os.path.isfile(marker_path):
        return False

    image_store = _load_image_store()
    totals = {"scanned": 0, "migrated": 0, "skipped_no_record": 0, "already_new": 0, "errors": 0}
    for platform, legacy_dir in platforms:
        if not os.path.isdir(legacy_dir):
            continue
        stats = _migrate_platform(
            data_dir=data_dir,
            workspace_root=workspace_root,
            platform=platform,
            legacy_dir=legacy_dir,
            image_store=image_store,
        )
        for k in totals:
            totals[k] += stats[k]
    logger.info(
        "[media_migration] 扫描=%s 迁移=%s 无记录跳过=%s 已新结构=%s 错误=%s",
        totals["scanned"], totals["migrated"], totals["skipped_no_record"],
        totals["already_new"], totals["errors"],
    )

    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write("1\n")
    except Exception as exc:  # pragma: no cover
        logger.warning("[media_migration] 写 marker 失败: %s", exc)
    return True
