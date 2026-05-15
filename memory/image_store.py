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


def _generate_video_id() -> str:
    """生成一个短且可读的视频 ID，格式: vid_<8位hex>"""
    return f"vid_{uuid.uuid4().hex[:8]}"


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
        if self.qdrant is None:
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
        if self.mongo_col is None:
            return
        try:
            self.mongo_col.create_index("image_id", unique=True)
            self.mongo_col.create_index("user_id")
            self.mongo_col.create_index("timestamp")
            # 复合文本索引：同时覆盖 tags 和 ocr_text，支持全文搜索
            # 注意：MongoDB 每个 collection 只能有一个文本索引，需要先尝试删除旧的
            try:
                self.mongo_col.drop_index("tags_text")
            except Exception:
                pass  # 旧索引不存在时忽略
            self.mongo_col.create_index(
                [("tags", pymongo.TEXT), ("ocr_text", pymongo.TEXT)],
                weights={"tags": 2, "ocr_text": 3},  # OCR 文字权重更高
                name="tags_ocr_text_index",
            )
        except Exception as e:
            logger.error(f"创建 MongoDB 索引失败: {e}")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def generate_image_id(self) -> str:
        """公开接口，生成唯一图片 ID。"""
        return _generate_image_id()

    def generate_video_id(self) -> str:
        """公开接口，生成唯一视频 ID。"""
        return _generate_video_id()

    def save_image_metadata(
        self,
        image_id: str,
        file_path: str,
        tags: str,
        user_id: str,
        chat_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
        platform: str = "",
        platform_message_id: Optional[str] = None,
        tag_status: str = "completed",
        media_type: str = "image",
    ) -> bool:
        """
        保存/更新一张图片或视频的元数据（默认完成态）。

        若 tags 为空仅写 Mongo，不写向量；为兼容新增的「先暂存，后补标签」流程。
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
            "platform": platform,
            "platform_message_id": platform_message_id,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "tag_status": tag_status,
            "media_type": media_type,
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
                                    "media_type": media_type,
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
    # 新增：先暂存、后补标签
    # ------------------------------------------------------------------

    def save_image_stub(
        self,
        image_id: str,
        file_path: str,
        user_id: str,
        chat_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
        platform: str = "",
        platform_message_id: Optional[str] = None,
        media_type: str = "image",
    ) -> bool:
        """先写入占位元数据（无标签、不写向量），等待模型回复后再补标签。"""
        if not self.enabled:
            logger.warning("ImageStore 未启用，跳过图片占位入库。")
            return False

        now = time.time()
        doc = {
            "image_id": image_id,
            "file_path": file_path,
            "tags": "",
            "user_id": user_id,
            "chat_id": chat_id,
            "platform": platform,
            "platform_message_id": platform_message_id,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "tag_status": "pending",
            "media_type": media_type,
            **(extra or {}),
        }
        try:
            self.mongo_col.replace_one({"image_id": image_id}, doc, upsert=True)
            logger.info(f"媒体占位已入库（等待标签）：{image_id} -> {file_path}")
            return True
        except Exception as e:
            logger.error(f"MongoDB 占位写入失败: {e}")
            return False

    def update_image_tags(
        self,
        image_id: str,
        tags: str,
        ocr_text: str = "",
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        file_path: Optional[str] = None,
        platform: str = "",
        platform_message_id: Optional[str] = None,
    ) -> bool:
        """在模型回复后补充标签/OCR，并写入向量。"""
        if not self.enabled:
            logger.warning("ImageStore 未启用，跳过标签更新。")
            return False

        now = time.time()
        existing: Dict[str, Any] = {}
        try:
            existing = self.mongo_col.find_one({"image_id": image_id}, {"_id": 0}) or {}
        except Exception:
            existing = {}

        base_file_path = file_path or existing.get("file_path", "")
        base_user_id = user_id or existing.get("user_id", "")
        base_chat_id = chat_id or existing.get("chat_id", "")
        base_ts = existing.get("timestamp", now)

        set_doc: Dict[str, Any] = {
            "tags": tags,
            "tag_status": "completed" if tags.strip() else existing.get("tag_status", "pending"),
            "updated_at": now,
            "updated_datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }
        if ocr_text:
            set_doc["ocr_text"] = ocr_text
        if platform:
            set_doc["platform"] = platform
        if platform_message_id:
            set_doc["platform_message_id"] = platform_message_id
        if file_path:
            set_doc["file_path"] = file_path
        if user_id:
            set_doc["user_id"] = user_id
        if chat_id:
            set_doc["chat_id"] = chat_id

        try:
            result = self.mongo_col.update_one({"image_id": image_id}, {"$set": set_doc}, upsert=False)
            if result.matched_count == 0:
                # 占位记录缺失，补一条
                fallback_doc = {
                    "image_id": image_id,
                    "file_path": base_file_path,
                    "user_id": base_user_id,
                    "chat_id": base_chat_id,
                    "platform": platform,
                    "platform_message_id": platform_message_id,
                    "timestamp": base_ts,
                    "datetime": datetime.fromtimestamp(base_ts, tz=timezone.utc).isoformat(),
                    "tag_status": set_doc.get("tag_status", "completed"),
                }
                fallback_doc.update(set_doc)
                self.mongo_col.replace_one({"image_id": image_id}, fallback_doc, upsert=True)
        except Exception as e:
            logger.error(f"MongoDB 更新图片标签失败: {e}")
            return False

        # ---- Qdrant ----
        if tags.strip():
            try:
                vector = self.embed_client.get_embedding(tags)
                if vector:
                    point_id = str(uuid.uuid4())
                    media_type_val = existing.get("media_type", "image")
                    self.qdrant.upsert(
                        collection_name=IMAGE_COLLECTION,
                        points=[
                            qdrant_models.PointStruct(
                                id=point_id,
                                vector=vector,
                                payload={
                                    "image_id": image_id,
                                    "text": tags,
                                    "file_path": base_file_path,
                                    "user_id": base_user_id,
                                    "chat_id": base_chat_id,
                                    "timestamp": base_ts,
                                    "media_type": media_type_val,
                                },
                            )
                        ],
                    )
                else:
                    logger.warning(f"标签向量化失败（更新阶段），已仅写 MongoDB: {image_id}")
            except Exception as e:
                logger.error(f"Qdrant 写入向量失败（更新阶段）: {e}")

        logger.info(
            f"图片标签已更新: {image_id} tags='{tags[:60]}...'"
            + (f", ocr='{ocr_text[:40]}...'" if ocr_text else "")
        )
        return True

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def get_by_id(self, image_id: str) -> Optional[Dict[str, Any]]:
        """按图片 ID 精确查询。"""
        if self.mongo_col is None:
            return None
        try:
            doc = self.mongo_col.find_one({"image_id": image_id}, {"_id": 0})
            return doc
        except Exception as e:
            logger.error(f"按 ID 查询图片失败: {e}")
            return None

    def get_by_platform_message_id(
        self,
        platform: str,
        platform_message_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        按平台消息 ID 反查图片元数据。

        使用场景：用户在 Telegram 里 reply 一张历史图片，message_history 没找到对应消息
        （可能 raw_window 已滑出），但图片入库时记录了 platform_message_id，
        此时仍能通过该方法把 image_id 找回来。
        """
        if self.mongo_col is None or not platform_message_id:
            return None
        try:
            query: Dict[str, Any] = {"platform_message_id": str(platform_message_id)}
            if platform:
                query["platform"] = platform
            doc = self.mongo_col.find_one(query, {"_id": 0})
            return doc
        except Exception as e:
            logger.error(f"按 platform_message_id 查询图片失败: {e}")
            return None

    def search_by_time_range(
        self,
        user_id: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按时间范围查询图片（MongoDB）。"""
        if self.mongo_col is None:
            return []
        try:
            query: Dict[str, Any] = {}
            if user_id:
                query["user_id"] = user_id
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
        if self.mongo_col is None:
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

    def search_by_ocr_text(
        self,
        text_query: str,
        user_id: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        在图片 OCR 文字中做模糊搜索（支持类搜索引擎的模糊匹配）。

        搜索规则（较强的模糊搜索）：
        1. 使用 MongoDB $regex 进行正则匹配
        2. 支持多词搜索：将查询拆分为关键词，每个词独立匹配（AND 逻辑）
        3. 忽略大小写
        4. 支持部分匹配（子串匹配）
        5. 忽略空格和标点差异
        """
        if self.mongo_col is None:
            return []
        if not text_query.strip():
            return []
        try:
            # 将查询拆分为关键词（按空格和常见标点分割）
            import re as _re
            keywords = _re.split(r'[\s,，、;；|]+', text_query.strip())
            keywords = [kw.strip() for kw in keywords if kw.strip()]

            if not keywords:
                return []

            # 构建 $and 条件：每个关键词都必须出现在 ocr_text 中（模糊匹配）
            regex_conditions = []
            for kw in keywords:
                # 转义正则特殊字符，允许每个字符之间有可选空白/标点
                escaped = _re.escape(kw)
                # 在字符之间插入可选空白匹配，增强模糊度
                flexible_pattern = r'[\s\S]*?'.join(escaped)
                regex_conditions.append({
                    "ocr_text": {
                        "$regex": flexible_pattern,
                        "$options": "is",  # i=忽略大小写, s=.匹配换行
                    }
                })

            query: Dict[str, Any] = {"$and": regex_conditions} if len(regex_conditions) > 1 else regex_conditions[0]
            if user_id:
                if "$and" in query:
                    query["$and"].append({"user_id": user_id})
                else:
                    query["user_id"] = user_id

            cursor = self.mongo_col.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f"OCR 文字搜索图片失败: {e}")
            return []

    def search_by_semantic(
        self,
        query_text: str,
        user_id: str = "",
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """语义向量检索（Qdrant）。"""
        if self.qdrant is None or not self.embed_client.enabled:
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
        text_query: str = "",
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        user_id: str = "",
        limit: int = 10,
        media_type: str = "",
    ) -> List[Dict[str, Any]]:
        """
        统一检索入口 —— 自动选择最佳策略：
        1. 有 image_id   → 精确查询
        2. 有 text_query  → 在图片 OCR 文字中做模糊搜索
        3. 有 keyword     → 先尝试语义搜索，再回退关键词搜索
        4. 有时间范围     → 时间过滤
        5. 什么都没有     → 返回最近的图片/视频

        media_type: 可选 "image"/"video"/""（空=不过滤）
        当 text_query 和 keyword 同时存在时，text_query 结果优先，
        keyword 结果作为补充（去重合并）。
        """
        # 精确查询
        if image_id:
            doc = self.get_by_id(image_id)
            return [doc] if doc else []

        def _apply_media_filter(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not media_type:
                return results
            return [r for r in results if r.get("media_type", "image") == media_type]

        # OCR 文字搜索 + 关键词搜索组合
        if text_query and keyword:
            ocr_results = self.search_by_ocr_text(text_query, user_id=user_id, limit=limit)
            keyword_results = self.search_by_semantic(keyword, user_id=user_id, top_k=limit)
            if not keyword_results:
                keyword_results = self.search_by_keyword(keyword, user_id=user_id, limit=limit)
            # Qdrant 语义搜索结果需要从 MongoDB 补全 ocr_text 等字段
            keyword_results = self._enrich_with_mongo(keyword_results)
            # 合并去重（以 image_id 去重，OCR 结果优先）
            seen_ids = set()
            merged = []
            for r in ocr_results + keyword_results:
                rid = r.get("image_id", "")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    merged.append(r)
            return _apply_media_filter(merged[:limit])

        # 纯 OCR 文字搜索
        if text_query:
            return _apply_media_filter(self.search_by_ocr_text(text_query, user_id=user_id, limit=limit))

        # 语义 + 关键词
        if keyword:
            semantic_results = self.search_by_semantic(keyword, user_id=user_id, top_k=limit)
            if semantic_results:
                # Qdrant 语义搜索结果需要从 MongoDB 补全 ocr_text 等字段
                return _apply_media_filter(self._enrich_with_mongo(semantic_results))
            return _apply_media_filter(self.search_by_keyword(keyword, user_id=user_id, limit=limit))

        # 时间范围
        if start_time is not None or end_time is not None:
            return _apply_media_filter(self.search_by_time_range(user_id, start_time, end_time, limit=limit))

        # 默认：最近的图片/视频
        return _apply_media_filter(self.search_by_time_range(user_id, limit=limit))

    def _enrich_with_mongo(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        用 MongoDB 的完整元数据补全 Qdrant 返回的结果。
        Qdrant payload 中缺少 ocr_text、tags 等字段，需要从 MongoDB 获取。
        """
        if not results or self.mongo_col is None:
            return results
        enriched = []
        for r in results:
            img_id = r.get("image_id", "")
            if img_id:
                try:
                    mongo_doc = self.mongo_col.find_one({"image_id": img_id}, {"_id": 0})
                    if mongo_doc:
                        # 保留 Qdrant 的 score，用 MongoDB 数据补全其他字段
                        score = r.get("score")
                        merged = {**mongo_doc, **{k: v for k, v in r.items() if v}}
                        if score is not None:
                            merged["score"] = score
                        enriched.append(merged)
                        continue
                except Exception:
                    pass
            enriched.append(r)
        return enriched


# 兼容别名
MediaStore = ImageStore
