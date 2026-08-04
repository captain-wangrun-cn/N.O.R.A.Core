"""
Qdrant 连接构造
================
统一处理「本地 host:port」与「Qdrant Cloud / 远程 url+api_key」两种连接方式。
所有 QdrantClient 的构造都应走这里，避免各调用点各自复制 host/port 逻辑。
"""

import logging
from typing import Any, Dict, Optional

from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)


def build_qdrant_client(mem_cfg: Optional[Dict[str, Any]] = None) -> QdrantClient:
    """
    根据 memory 配置构造 QdrantClient。

    优先使用 `memory.qdrant.url`（Qdrant Cloud / 远程实例，HTTPS + API key）；
    未配置 url 时回落 `memory.qdrant.host` + `port`（本地实例）。
    """
    mem_cfg = mem_cfg or {}
    qdrant_cfg = mem_cfg.get("qdrant", {}) or {}

    url = str(qdrant_cfg.get("url") or "").strip()
    api_key = str(qdrant_cfg.get("api_key") or "").strip()

    if url:
        logger.info(f"Qdrant 使用 URL 连接: {url}")
        return QdrantClient(url=url, api_key=api_key or None)

    host = qdrant_cfg.get("host", "localhost")
    port = int(qdrant_cfg.get("port", 6333))
    logger.info(f"Qdrant 使用本地连接: {host}:{port}")
    return QdrantClient(host=host, port=port)


def describe_qdrant_target(mem_cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    返回当前 Qdrant 连接目标的可读描述，用于 CLI 展示与日志。

    与 build_qdrant_client 使用同一套判定，避免展示与实际连接不一致。
    Cloud 分支只显示 URL，不泄漏 api_key。
    """
    mem_cfg = mem_cfg or {}
    qdrant_cfg = mem_cfg.get("qdrant", {}) or {}

    url = str(qdrant_cfg.get("url") or "").strip()
    if url:
        return f"{url}（Cloud/远程）"

    host = qdrant_cfg.get("host", "localhost")
    port = qdrant_cfg.get("port", 6333)
    return f"{host}:{port}（本地）"
