"""MongoDB 图片全文索引升级测试。"""
from unittest.mock import MagicMock

import pymongo

from memory.image_store import (
    IMAGE_TEXT_INDEX_NAME,
    IMAGE_TEXT_INDEX_WEIGHTS,
    ImageStore,
)


def _text_index(name, weights):
    return {
        "name": name,
        "key": {"_fts": "text", "_ftsx": 1},
        "weights": weights,
    }


def _store_with_indexes(*responses):
    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    indexes = [[{"name": "_id_", "key": {"_id": 1}}, *response] for response in responses]
    store.mongo_col.list_indexes.side_effect = [iter(response) for response in indexes]
    return store


def _assert_target_created(store):
    store.mongo_col.create_index.assert_called_once_with(
        [("tags", pymongo.TEXT), ("ocr_text", pymongo.TEXT), ("description", pymongo.TEXT)],
        weights=IMAGE_TEXT_INDEX_WEIGHTS,
        name=IMAGE_TEXT_INDEX_NAME,
    )


def test_current_text_index_is_idempotent():
    store = _store_with_indexes([_text_index(IMAGE_TEXT_INDEX_NAME, IMAGE_TEXT_INDEX_WEIGHTS)])

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_not_called()
    store.mongo_col.create_index.assert_not_called()


def test_creates_text_index_when_missing():
    store = _store_with_indexes([])

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_not_called()
    _assert_target_created(store)


def test_migrates_known_same_name_legacy_index():
    store = _store_with_indexes([
        _text_index(IMAGE_TEXT_INDEX_NAME, {"tags": 2, "ocr_text": 3}),
    ])

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_called_once_with(IMAGE_TEXT_INDEX_NAME)
    _assert_target_created(store)


def test_migrates_known_tags_text_index():
    store = _store_with_indexes([_text_index("tags_text", {"tags": 1})])

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_called_once_with("tags_text")
    _assert_target_created(store)


def test_unknown_same_name_index_is_preserved(caplog):
    store = _store_with_indexes([
        _text_index(IMAGE_TEXT_INDEX_NAME, {"tags": 9, "ocr_text": 3}),
    ])

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_not_called()
    store.mongo_col.create_index.assert_not_called()
    assert "规格未知" in caplog.text


def test_unknown_text_index_is_preserved(caplog):
    store = _store_with_indexes([_text_index("custom_text", {"content": 1})])

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_not_called()
    store.mongo_col.create_index.assert_not_called()
    assert "未知全文索引" in caplog.text


def test_concurrent_create_accepts_final_target_index():
    target = _text_index(IMAGE_TEXT_INDEX_NAME, IMAGE_TEXT_INDEX_WEIGHTS)
    store = _store_with_indexes([], [target])
    store.mongo_col.create_index.side_effect = RuntimeError("index created concurrently")

    store._ensure_image_text_index()

    store.mongo_col.drop_index.assert_not_called()
    _assert_target_created(store)


def test_index_migration_never_updates_documents():
    store = _store_with_indexes([
        _text_index(IMAGE_TEXT_INDEX_NAME, {"tags": 2, "ocr_text": 3}),
    ])

    store._ensure_image_text_index()

    store.mongo_col.update_many.assert_not_called()
    store.mongo_col.replace_one.assert_not_called()
    store.mongo_col.bulk_write.assert_not_called()
