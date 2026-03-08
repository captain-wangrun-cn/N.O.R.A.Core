'''
图片记忆存储模块
===============
职责：
  1. 为每张图片生成唯一 ID（img_<short-uuid>）
  2. 将图片元数据（ID、本地路径、LLM 生成的标签、时间戳、用户 ID 等）写入 MongoDB
  3. 将标签文本向量化后存入 Qdrant，用于语义检索
  4. 提供按 ID、时间范围、关键词 / 向量的检索接口
'''

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models
import pymongo

from memory.embed import EmbeddingClient
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IMAGE_COLLECTION = "nora_images"          # Qdrant collection 名称
MONGO_DB_NAME = "nora"                     # MongoDB 数据库名称
MONGO_COLLECTION = "images"                # MongoDB collection 名称


def _generate_image_id() -> str:
    """生成一个短且可读的图片 ID，格式: img_<8位hex>"""
    return f"img_{uuid.uuid4().hex[:8]}"


class ImageStore:
    """
    图片记忆的统一存储 & 检索入口。
    内部同时操作 MongoDB（结构化元数据）和 Qdrant（语义向量）。
    """

    def __init__(self):
        mem_cfg = config.get_config().get("memory", {})
        qdrant_cfg = mem_cfg.get("qdrant", {})
        mongo_cfg = mem_cfg.get("mongo", {})
        embed_cfg = mem_cfg.get("embedding", {})

        self.vector_size = embed_cfg.get("dimensions", 1024)

        # --- Qdrant ---
        try:
            self.qdrant = QdrantClient(
                host=qdrant_cfg.get("host", "localhost"),
                port=qdrant_cfg.get("port", 6333),
            )
            self._ensure_qdrant_collection()
        except Exception as e:
            logger.error(f"ImageStore: Qdrant 连接失败: {e}")
            self.qdrant = None

        # --- MongoDB ---
        try:
            mongo_uri = mongo_cfg.get("uri", "mongodb://localhost:27017/")
            self.mongo_client = pymongo.MongoClient(mongo_uri)
            self.mongo_db = self.mongo_client[MONGO_DB_NAME]
            self.mongo_col = self.mongo_db[MONGO_COLLECTION]
            self._ensure_mongo_indexes()
            logger.info("ImageStore: MongoDB 已连接。")
        except Exception as e:
            logger.error(f"ImageStore: MongoDB 连接失败: {e}")
            self.mongo_client = None
            self.mongo_db = None
            self.mongo_col = None

        # --- Embedding ---
        self.embed_client = EmbeddingClient()

        self.enabled = (self.qdrant is not None
                        and self.mongo_col is not None
                        and self.embed_client.enabled)
        if self.enabled:
            logger.info("ImageStore 已就绪（MongoDB + Qdrant + Embedding）。")
        else:
            logger.warning("ImageStore 未完全就绪，图片记忆功能降级。")

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _ensure_qdrant_collection(self):
        """确保 Qdrant 中存在图片向量集合。"""
        if not self.qdrant:
            return
        try:
            collections = self.qdrant.get_collections()
            exists = any(c.name == IMAGE_COLLECTION for c in collections.collections)
            if not exists:
                logger.info(f"创建 Qdrant collection: {IMAGE_COLLECTION}")
                self.qdrant.create_collection(
                    collection_name=IMAGE_COLLECTION,
                    vectors_config=qdrant_models.VectorParams(
                        size=self.vector_size,
                        distance=qdrant_models.Distance.COSINE,
                    ),
                )
                # 创建常用 payload 索引
                for field in ("image_id", "user_id"):
                    self.qdrant.create_payload_index(
                        collection_name=IMAGE_COLLECTION,
                        field_name=field,
                        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
                    )
                self.qdrant.create_payload_index(
                    collection_name=IMAGE_COLLECTION,
                    field_name="timestamp",
                    field_schema=qdrant_models.PayloadSchemaType.FLOAT,
                )
        except Exception as e:
            logger.error(f"创建 Qdrant collection 失败: {e}")

    def _ensure_mongo_indexes(self):
        """确保 MongoDB 索引存在。"""
        if not self.mongo_col:
            return
        try:
            self.mongo_col.create_index("image_id", unique=True)
            self.mongo_col.create_index("user_id")
            self.mongo_col.create_index("timestamp")
            self.mongo_col.create_index([("tags", pymongo.TEXT)])
        except Exception as e:
            logger.error(f"创建 MongoDB 索引失败: {e}")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def generate_image_id(self) -> str:
        """公开接口，生成唯一图片 ID。"""
        return _generate_image_id()

    def save_image_metadata(
        self,
        image_id: str,
        file_path: str,
        tags: str,
        user_id: str,
        chat_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        保存一张图片的完整元数据。

        Parameters
        ----------
        image_id : str
            由 generate_image_id() 生成的唯一标识
        file_path : str
            图片在本地的文件路径
        tags : str
            LLM 生成的详细图片标签/描述文本
        user_id : str
            发送者 ID
        chat_id : str
            所在对话 ID
        extra : dict, optional
            其他自定义字段

        Returns
        -------
        bool  存储是否成功
        """
        if not self.enabled:
            logger.warning("ImageStore 未启用，跳过图片元数据保存。")
            return False

        now = time.time()

        # ---- MongoDB ----
        doc = {
            "image_id": image_id,
            "file_path": file_path,
            "tags": tags,
            "user_id": user_id,
            "chat_id": chat_id,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            **(extra or {}),
        }
        try:
            self.mongo_col.replace_one({"image_id": image_id}, doc, upsert=True)
        except Exception as e:
            logger.error(f"MongoDB 写入图片元数据失败: {e}")
            return False

        # ---- Qdrant ----
        if tags.strip():
            try:
                vector = self.embed_client.get_embedding(tags)
                if vector:
                    point_id = str(uuid.uuid4())
                    self.qdrant.upsert(
                        collection_name=IMAGE_COLLECTION,
                        points=[
                            qdrant_models.PointStruct(
                                id=point_id,
                                vector=vector,
                                payload={
                                    "image_id": image_id,
                                    "text": tags,
                                    "file_path": file_path,
                                    "user_id": user_id,
                                    "chat_id": chat_id,
                                    "timestamp": now,
                                },
                            )
                        ],
                    )
                else:
                    logger.warning(f"图片标签向量化失败，仅保存 MongoDB 元数据: {image_id}")
            except Exception as e:
                logger.error(f"Qdrant 写入图片向量失败: {e}")
                # MongoDB 已写入，不算完全失败
        
        logger.info(f"图片元数据已保存: {image_id} tags='{tags[:60]}...'")
        return True

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def get_by_id(self, image_id: str) -> Optional[Dict[str, Any]]:
        """按图片 ID 精确查询。"""
        if not self.mongo_col:
            return None
        try:
            doc = self.mongo_col.find_one({"image_id": image_id}, {"_id": 0})
            return doc
        except Exception as e:
            logger.error(f"按 ID 查询图片失败: {e}")
            return None

    def search_by_time_range(
        self,
        user_id: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按时间范围查询图片（MongoDB）。"""
        if not self.mongo_col:
            return []
        try:
            query: Dict[str, Any] = {"user_id": user_id}
            time_filter: Dict[str, Any] = {}
            if start_time is not None:
                time_filter["$gte"] = start_time
            if end_time is not None:
                time_filter["$lte"] = end_time
            if time_filter:
                query["timestamp"] = time_filter

            cursor = self.mongo_col.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f"按时间范围查询图片失败: {e}")
            return []

    def search_by_keyword(
        self,
        keyword: str,
        user_id: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """按关键词在 tags 中做文本搜索（MongoDB $text）。"""
        if not self.mongo_col:
            return []
        try:
            query: Dict[str, Any] = {"$text": {"$search": keyword}}
            if user_id:
                query["user_id"] = user_id
            cursor = (
                self.mongo_col.find(query, {"_id": 0, "score": {"$meta": "textScore"}})
                .sort([("score", {"$meta": "textScore"})])
                .limit(limit)
            )
            return list(cursor)
        except Exception as e:
            logger.error(f"关键词搜索图片失败: {e}")
            return []

    def search_by_semantic(
        self,
        query_text: str,
        user_id: str = "",
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """语义向量检索（Qdrant）。"""
        if not self.qdrant or not self.embed_client.enabled:
            return []
        try:
            vector = self.embed_client.get_embedding(query_text)
            if not vector:
                return []

            qdrant_filter = None
            if user_id:
                qdrant_filter = qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="user_id",
                            match=qdrant_models.MatchValue(value=user_id),
                        )
                    ]
                )

            response = self.qdrant.query_points(
                collection_name=IMAGE_COLLECTION,
                query=vector,
                query_filter=qdrant_filter,
                limit=top_k,
            )
            results = []
            for hit in response.points:
                item = dict(hit.payload)
                item["score"] = hit.score
                results.append(item)
            return results
        except Exception as e:
            logger.error(f"语义搜索图片失败: {e}")
            return []

    def search_images(
        self,
        image_id: str = "",
        keyword: str = "",
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        user_id: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        统一检索入口 —— 自动选择最佳策略：
        1. 有 image_id → 精确查询
        2. 有 keyword  → 先尝试语义搜索，再回退关键词搜索
        3. 有时间范围  → 时间过滤
        4. 什么都没有  → 返回最近的图片
        """
        # 精确查询
        if image_id:
            doc = self.get_by_id(image_id)
            return [doc] if doc else []

        # 语义 + 关键词
        if keyword:
            semantic_results = self.search_by_semantic(keyword, user_id=user_id, top_k=limit)
            if semantic_results:
                return semantic_results
            return self.search_by_keyword(keyword, user_id=user_id, limit=limit)

        # 时间范围
        if start_time is not None or end_time is not None:
            return self.search_by_time_range(user_id, start_time, end_time, limit=limit)

        # 默认：最近的图片
        return self.search_by_time_range(user_id, limit=limit)
