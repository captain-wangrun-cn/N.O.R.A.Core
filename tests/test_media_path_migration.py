"""
Tests for media storage path layout + migration.
"""
import os
import tempfile

import pytest

from adapters.media_path import (
    infer_scene,
    safe_chat_id_part,
    build_media_subdir,
    build_media_relpath,
    SEGMENT_TO_MEDIA_TYPE,
)


def test_infer_scene_by_chat_type_priority():
    assert infer_scene(chat_type="private") == "private"
    assert infer_scene(chat_type="group") == "group"
    assert infer_scene(chat_type="supergroup") == "group"
    assert infer_scene(chat_type="channel") == "channel"


def test_infer_scene_by_message_type():
    assert infer_scene(message_type="group") == "group"
    assert infer_scene(message_type="private") == "private"


def test_infer_scene_by_chat_id_sign():
    # 负 chat_id 代表群（Telegram/OneBot 群号均为负）
    assert infer_scene(chat_id="-100123456") == "group"
    assert infer_scene(chat_id="123456789") == "private"
    # chat_type 优先于符号推断
    assert infer_scene(chat_type="channel", chat_id="-1") == "channel"
    # 兜底
    assert infer_scene() == "private"


def test_safe_chat_id_part():
    assert safe_chat_id_part("-100123456", "group") == "-100123456"
    assert safe_chat_id_part("a/b\\c", "private") == "a_b_c"
    assert safe_chat_id_part("", "private") == "unknown"
    assert safe_chat_id_part("", "group") == "unknown_group"


def test_build_media_subdir_creates_dirs(tmp_path):
    target = build_media_subdir(
        data_dir=str(tmp_path),
        platform="telegram",
        scene="group",
        chat_id="-100123456",
        media_type="image",
    )
    assert os.path.isdir(target)
    assert target.replace("\\", "/").endswith("media/telegram/group/-100123456/image")


def test_build_media_relpath(tmp_path):
    target = build_media_subdir(
        data_dir=str(tmp_path),
        platform="telegram",
        scene="private",
        chat_id="123",
        media_type="video",
    )
    rel = build_media_relpath(workspace_root=str(tmp_path), abs_target_dir=target, filename="x.mp4")
    assert rel == "media/telegram/private/123/video/x.mp4"
    assert (tmp_path / rel.replace("/", os.sep)).parent.exists()


def test_segment_to_media_type_mapping():
    assert SEGMENT_TO_MEDIA_TYPE["record"] == "record"
    assert SEGMENT_TO_MEDIA_TYPE["photo"] == "image"


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def test_migration_idempotent_marker(tmp_path):
    from scripts import media_migration
    from scripts.media_migration import MIGRATION_MARKER

    legacy = tmp_path / "data" / "telegram"
    legacy.mkdir(parents=True)
    (legacy / "photo_abc.jpg").write_bytes(b"x")
    data_dir = str(tmp_path / "data")
    ws = str(tmp_path)

    # 跳过真实 Mongo/Qdrant 连接
    orig_load = media_migration._load_image_store
    media_migration._load_image_store = lambda: None
    try:
        # 无 DB 记录，文件被判定为无主，保留原位
        ran = media_migration.migrate_media_layout_once(
            data_dir=data_dir, workspace_root=ws, platforms=[("telegram", str(legacy))]
        )
        assert ran is True
        # 第二次：marker 存在，直接跳过
        ran2 = media_migration.migrate_media_layout_once(
            data_dir=data_dir, workspace_root=ws, platforms=[("telegram", str(legacy))]
        )
        assert ran2 is False
        assert os.path.isfile(os.path.join(data_dir, MIGRATION_MARKER))
    finally:
        media_migration._load_image_store = orig_load


def test_migration_moves_with_mongo_record(tmp_path):
    """有 DB file_path 记录时，文件移动并更新 file_path。仅更新 file_path，不回填 scene。"""
    from scripts import media_migration

    ws = tmp_path
    data_dir = ws / "data"
    legacy = data_dir / "telegram"
    legacy.mkdir(parents=True)
    fpath = legacy / "photo_abc.jpg"
    fpath.write_bytes(b"img")

    rel_old = os.path.relpath(str(fpath), str(ws)).replace("\\", "/")

    # mock ImageStore：mongo_col.find_one 返回记录，update_many 记录被调用
    class FakeCol:
        def __init__(self):
            self.records = {"file_path": rel_old}
            self.updated = []

        def find_one(self, q, *_a, **_k):
            if q == {"file_path": rel_old}:
                return {
                    "file_path": rel_old,
                    "chat_id": "-10042",
                    "platform": "telegram",
                    "media_type": "image",
                }
            return None

        def update_many(self, q, op, *_a, **_k):
            self.updated.append((q, op))

    class FakeStore:
        def __init__(self):
            self.mongo_col = FakeCol()

    fake = FakeStore()
    orig_load = media_migration._load_image_store
    media_migration._load_image_store = lambda: fake
    try:
        media_migration.migrate_media_layout_once(
            data_dir=str(data_dir),
            workspace_root=str(ws),
            platforms=[("telegram", str(legacy))],
        )
    finally:
        media_migration._load_image_store = orig_load

    # 文件被移走
    assert not fpath.exists()
    # DB 记录的 file_path 被更新为新结构
    assert fake.mongo_col.updated
    _q, op = fake.mongo_col.updated[0]
    new_rel = op["$set"]["file_path"]
    assert new_rel.startswith("data/media/telegram/group/-10042/image/")
    # 新文件确实在新位置
    assert (ws / new_rel.replace("/", os.sep)).is_file()
    # 旧目录已清理
    assert not legacy.exists()
