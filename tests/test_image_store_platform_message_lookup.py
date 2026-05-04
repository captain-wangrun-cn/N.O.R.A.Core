from unittest.mock import MagicMock


def test_get_by_platform_message_id_builds_query():
    """按 platform_message_id 反查 image_id 应使用精确匹配 + platform 过滤。"""
    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    store.mongo_col.find_one.return_value = {
        "image_id": "img_abcd1234",
        "platform": "telegram",
        "platform_message_id": "98765",
        "file_path": "data/telegram/photo_xxx.jpg",
    }

    doc = store.get_by_platform_message_id("telegram", "98765")
    assert doc is not None
    assert doc["image_id"] == "img_abcd1234"

    query_arg = store.mongo_col.find_one.call_args[0][0]
    assert query_arg["platform_message_id"] == "98765"
    assert query_arg["platform"] == "telegram"


def test_get_by_platform_message_id_returns_none_when_missing():
    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    store.mongo_col.find_one.return_value = None

    assert store.get_by_platform_message_id("telegram", "404") is None


def test_get_by_platform_message_id_handles_no_mongo():
    """mongo_col 不可用时应优雅返回 None。"""
    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = None
    store.qdrant = None
    store.embed_client = None
    store.enabled = False

    assert store.get_by_platform_message_id("telegram", "123") is None


def test_get_by_platform_message_id_ignores_empty_id():
    from memory.image_store import ImageStore

    store = ImageStore.__new__(ImageStore)
    store.mongo_col = MagicMock()
    store.qdrant = None
    store.embed_client = MagicMock()
    store.enabled = True

    assert store.get_by_platform_message_id("telegram", "") is None
    store.mongo_col.find_one.assert_not_called()
