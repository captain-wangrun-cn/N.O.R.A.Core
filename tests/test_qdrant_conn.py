"""测试 Qdrant 连接构造：本地 host:port 与 Cloud url+api_key 双模式。"""

from unittest.mock import patch

from memory.qdrant_conn import build_qdrant_client, describe_qdrant_target


def test_local_host_port_mode():
    """未配置 url 时，走 host+port 连接。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({
            "qdrant": {"host": "127.0.0.1", "port": 6333},
        })
    mock_cls.assert_called_once_with(host="127.0.0.1", port=6333)


def test_local_host_port_defaults():
    """缺失 host/port 时使用默认值。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({})
    mock_cls.assert_called_once_with(host="localhost", port=6333)


def test_cloud_url_mode_with_api_key():
    """配置 url + api_key 时，走 HTTPS + API key 连接。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({
            "qdrant": {
                "url": "https://xxxx.cloud.qdrant.io",
                "api_key": "secret-token",
            },
        })
    mock_cls.assert_called_once_with(
        url="https://xxxx.cloud.qdrant.io",
        api_key="secret-token",
    )


def test_cloud_url_mode_without_api_key():
    """只有 url 没有 api_key 时，api_key 传 None（本地匿名远程实例）。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({
            "qdrant": {"url": "https://xxxx.cloud.qdrant.io"},
        })
    mock_cls.assert_called_once_with(
        url="https://xxxx.cloud.qdrant.io",
        api_key=None,
    )


def test_cloud_url_takes_precedence_over_host_port():
    """同时配置 url 与 host/port 时，优先 url。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({
            "qdrant": {
                "host": "localhost",
                "port": 6333,
                "url": "https://xxxx.cloud.qdrant.io",
                "api_key": "secret-token",
            },
        })
    mock_cls.assert_called_once_with(
        url="https://xxxx.cloud.qdrant.io",
        api_key="secret-token",
    )


# ---------------------------------------------------------------------------
# 旧 config.yml 兼容性：新增的 url/api_key 是可选字段，旧配置行为必须完全不变
# ---------------------------------------------------------------------------

def test_legacy_config_qdrant_key_missing():
    """旧配置整个 qdrant 段缺失时，兜底本地默认值。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({"embedding": {"dimensions": 1024}})
    mock_cls.assert_called_once_with(host="localhost", port=6333)


def test_legacy_config_memory_section_none():
    """memory 段整体为 None 时不应抛异常。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client(None)
    mock_cls.assert_called_once_with(host="localhost", port=6333)


def test_legacy_config_qdrant_empty_dict():
    """qdrant 为空 dict 时兜底本地默认值。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({"qdrant": {}})
    mock_cls.assert_called_once_with(host="localhost", port=6333)


def test_blank_url_falls_back_to_local():
    """残留的空串 url 不得被误判为 Cloud 配置。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({
            "qdrant": {"host": "h1", "port": 6333, "url": "   "},
        })
    mock_cls.assert_called_once_with(host="h1", port=6333)


def test_null_url_falls_back_to_local():
    """残留的 url: null 不得被误判为 Cloud 配置。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({
            "qdrant": {"host": "h1", "port": 6333, "url": None},
        })
    mock_cls.assert_called_once_with(host="h1", port=6333)


def test_string_port_is_coerced_to_int():
    """旧配置里 port 可能是字符串，需转成 int。"""
    with patch("memory.qdrant_conn.QdrantClient") as mock_cls:
        build_qdrant_client({"qdrant": {"host": "h1", "port": "6333"}})
    mock_cls.assert_called_once_with(host="h1", port=6333)


# ---------------------------------------------------------------------------
# describe_qdrant_target：CLI 展示用，必须与 build_qdrant_client 判定一致且不泄漏 key
# ---------------------------------------------------------------------------

def test_describe_local_target():
    desc = describe_qdrant_target({"qdrant": {"host": "localhost", "port": 6333}})
    assert "localhost:6333" in desc
    assert "本地" in desc


def test_describe_cloud_target_hides_api_key():
    """Cloud 描述只显示 URL，绝不能出现 api_key。"""
    desc = describe_qdrant_target({
        "qdrant": {
            "url": "https://xxxx.cloud.qdrant.io",
            "api_key": "super-secret-token",
            "host": "localhost",
            "port": 6333,
        },
    })
    assert "https://xxxx.cloud.qdrant.io" in desc
    assert "super-secret-token" not in desc
    # url 存在时不应展示本地 host/port，否则与实际连接目标矛盾
    assert "localhost:6333" not in desc


def test_describe_blank_url_shows_local():
    """残留空 url 时描述必须与实际连接（本地）一致。"""
    desc = describe_qdrant_target({"qdrant": {"host": "h1", "port": 6333, "url": ""}})
    assert "h1:6333" in desc
    assert "本地" in desc


def test_describe_missing_config_defaults():
    assert "localhost:6333" in describe_qdrant_target({})
    assert "localhost:6333" in describe_qdrant_target(None)
