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

from qdrant_client.http import models as qdrant_models
import pymongo

from memory.embed import EmbeddingClient
from memory.qdrant_conn import build_qdrant_client
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IMAGE_COLLECTION = "nora_images"          # Qdrant collection 名称
DEFAULT_MONGO_DB_NAME = "nora"             # MongoDB 数据库名称（无显式配置且 URI 未带库名时的兜底）
MONGO_COLLECTION = "images"                # MongoDB collection 名称
IMAGE_TEXT_INDEX_NAME = "tags_ocr_text_index"
IMAGE_TEXT_INDEX_KEYS = (
    ("tags", pymongo.TEXT),
    ("ocr_text", pymongo.TEXT),
    ("description", pymongo.TEXT),
)
IMAGE_TEXT_INDEX_WEIGHTS = {"tags": 2, "ocr_text": 3, "description": 2}
LEGACY_IMAGE_TEXT_INDEX_WEIGHTS = {"tags": 2, "ocr_text": 3}
LEGACY_TAGS_TEXT_INDEX_NAME = "tags_text"
LEGACY_TAGS_TEXT_INDEX_WEIGHTS = {"tags": 1}


def resolve_mongo_db_name(mongo_cfg: Dict[str, Any], client: "pymongo.MongoClient") -> str:
    """
    解析 MongoDB 数据库名称，优先级：
      1. 配置项 memory.mongo.db_name（显式指定）
      2. URI 路径里携带的默认库名（如 .../nora_qiuxi）
      3. 兜底常量 DEFAULT_MONGO_DB_NAME

    注意：受限用户通常只对自己的库有权限，硬编码库名会触发 not authorized。
    """
    explicit = (mongo_cfg.get("db_name") or "").strip()
    if explicit:
        return explicit
    try:
        default_db = client.get_default_database()
        if default_db is not None and default_db.name:
            return default_db.name
    except Exception:
        pass
    return DEFAULT_MONGO_DB_NAME


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
        mongo_cfg = mem_cfg.get("mongo", {})
        embed_cfg = mem_cfg.get("embedding", {})

        self.vector_size = embed_cfg.get("dimensions", 1024)

        # --- Qdrant ---
        try:
            self.qdrant = build_qdrant_client(mem_cfg)
            self._ensure_qdrant_collection()
        except Exception as e:
            logger.error(f"ImageStore: Qdrant 连接失败: {e}")
            self.qdrant = None

        # --- MongoDB ---
        try:
            mongo_uri = mongo_cfg.get("uri", "mongodb://localhost:27017/")
            self.mongo_client = pymongo.MongoClient(mongo_uri)
            db_name = resolve_mongo_db_name(mongo_cfg, self.mongo_client)
            self.mongo_db = self.mongo_client[db_name]
            self.mongo_col = self.mongo_db[MONGO_COLLECTION]
            self._ensure_mongo_indexes()
            logger.info(f"ImageStore: MongoDB 已连接（db={db_name}）。")
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
            # 新旧 collection 都补齐 payload 索引，保证升级后的上下文过滤可高效执行。
            self._ensure_qdrant_payload_index("image_id", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("user_id", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("storage_id", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("platform", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("chat_id", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("memory_scope_id", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("place_scope_id", qdrant_models.PayloadSchemaType.KEYWORD)
            self._ensure_qdrant_payload_index("timestamp", qdrant_models.PayloadSchemaType.FLOAT)
        except Exception as e:
            logger.error(f"创建 Qdrant collection 失败: {e}")

    def _ensure_qdrant_payload_index(self, field_name: str, field_schema) -> None:
        try:
            self.qdrant.create_payload_index(
                collection_name=IMAGE_COLLECTION,
                field_name=field_name,
                field_schema=field_schema,
            )
        except Exception as e:
            logger.debug(f"ImageStore: Qdrant payload 索引已存在或创建失败: {field_name}, {e}")

    @staticmethod
    def _normalize_text_index(index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """提取文本索引的稳定元数据；MongoDB 会将 text key 返回为内部 _fts 结构。"""
        key = index.get("key") or {}
        key_items = tuple(key.items()) if hasattr(key, "items") else tuple(key)
        is_text = any(str(kind).lower() == "text" for _field, kind in key_items)
        if not is_text:
            return None
        try:
            weights = {str(field): int(weight) for field, weight in (index.get("weights") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            weights = {}
        return {
            "name": str(index.get("name") or ""),
            "weights": weights,
        }

    def _list_mongo_text_indexes(self) -> List[Dict[str, Any]]:
        indexes = []
        for index in self.mongo_col.list_indexes():
            normalized = self._normalize_text_index(index)
            if normalized is not None:
                indexes.append(normalized)
        return indexes

    @staticmethod
    def _find_text_index(indexes: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
        return next((index for index in indexes if index["name"] == name), None)

    def _has_current_text_index(self) -> bool:
        current = self._find_text_index(self._list_mongo_text_indexes(), IMAGE_TEXT_INDEX_NAME)
        return bool(current and current["weights"] == IMAGE_TEXT_INDEX_WEIGHTS)

    def _create_image_text_index(self) -> None:
        try:
            self.mongo_col.create_index(
                list(IMAGE_TEXT_INDEX_KEYS),
                weights=IMAGE_TEXT_INDEX_WEIGHTS,
                name=IMAGE_TEXT_INDEX_NAME,
            )
        except Exception as exc:
            # 多实例可能同时完成升级；以服务端最终状态为准。
            try:
                if self._has_current_text_index():
                    logger.info("MongoDB 图片全文索引已由另一实例完成创建。")
                    return
            except Exception:
                pass
            logger.error(f"创建 MongoDB 图片全文索引失败: {exc}")

    def _ensure_image_text_index(self) -> None:
        """安全、幂等地创建或升级图片全文索引。"""
        try:
            text_indexes = self._list_mongo_text_indexes()
        except Exception as exc:
            logger.error(f"读取 MongoDB 图片全文索引失败: {exc}")
            return

        current = self._find_text_index(text_indexes, IMAGE_TEXT_INDEX_NAME)
        if current:
            if current["weights"] == IMAGE_TEXT_INDEX_WEIGHTS:
                return
            if current["weights"] != LEGACY_IMAGE_TEXT_INDEX_WEIGHTS:
                logger.error(
                    "MongoDB 图片全文索引规格未知，保留原索引不自动删除: name=%s weights=%s",
                    current["name"], current["weights"],
                )
                return
            old_name = IMAGE_TEXT_INDEX_NAME
        else:
            legacy = self._find_text_index(text_indexes, LEGACY_TAGS_TEXT_INDEX_NAME)
            if legacy:
                if legacy["weights"] != LEGACY_TAGS_TEXT_INDEX_WEIGHTS:
                    logger.error(
                        "MongoDB 旧图片全文索引规格未知，保留原索引不自动删除: name=%s weights=%s",
                        legacy["name"], legacy["weights"],
                    )
                    return
                old_name = LEGACY_TAGS_TEXT_INDEX_NAME
            elif text_indexes:
                logger.error(
                    "MongoDB images 集合存在未知全文索引，保留原索引不自动删除: %s",
                    text_indexes,
                )
                return
            else:
                self._create_image_text_index()
                return

        logger.info("升级 MongoDB 图片全文索引: %s -> %s", old_name, IMAGE_TEXT_INDEX_NAME)
        try:
            self.mongo_col.drop_index(old_name)
        except Exception as exc:
            # 若另一实例已完成升级，无需再次操作。
            try:
                if self._has_current_text_index():
                    return
            except Exception:
                pass
            logger.error(f"删除 MongoDB 旧图片全文索引失败: {old_name}, {exc}")
            return
        self._create_image_text_index()

    def _ensure_mongo_indexes(self):
        """确保 MongoDB 索引存在。"""
        if self.mongo_col is None:
            return
        try:
            self.mongo_col.create_index("image_id", unique=True)
            self.mongo_col.create_index("user_id")
            self.mongo_col.create_index("storage_id")
            self.mongo_col.create_index([("platform", 1), ("chat_id", 1), ("platform_message_id", 1)])
            self.mongo_col.create_index([("platform", 1), ("storage_id", 1), ("timestamp", -1)])
            self.mongo_col.create_index([("memory_scope_id", 1), ("timestamp", -1)])
            self.mongo_col.create_index("timestamp")
        except Exception as e:
            logger.error(f"创建 MongoDB 普通索引失败: {e}")
        self._ensure_image_text_index()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def generate_image_id(self) -> str:
        """公开接口，生成唯一图片 ID。"""
        return _generate_image_id()

    def generate_video_id(self) -> str:
        """公开接口，生成唯一视频 ID。"""
        return _generate_video_id()

    @staticmethod
    def _scope_payload(
        *,
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
        platform: str = "",
        chat_id: str = "",
    ) -> Dict[str, Any]:
        """
        构建跨平台接力的作用域 payload。
        memory_scope_id 为空时不写（保持旧记录形态）；place_scope_id 缺省由 platform:chat_id 生成，
        方便日后按来源地点回查。
        """
        fields: Dict[str, Any] = {}
        if memory_scope_id:
            fields["memory_scope_id"] = memory_scope_id
            fields["place_scope_id"] = place_scope_id or (
                f"{platform}:{chat_id}" if (platform or chat_id) else ""
            )
            if owner_id:
                fields["owner_id"] = owner_id
            if relationship_id:
                fields["relationship_id"] = relationship_id
            if actor_display_name:
                fields["actor_display_name"] = actor_display_name
        return fields

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
        storage_id: str = "",
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
    ) -> bool:
        """
        保存/更新一张图片或视频的元数据（默认完成态）。

        若 tags 为空仅写 Mongo，不写向量；为兼容新增的「先暂存，后补标签」流程。
        """
        if not self.enabled:
            logger.warning("ImageStore 未启用，跳过图片元数据保存。")
            return False

        now = time.time()
        scope_fields = self._scope_payload(
            memory_scope_id=memory_scope_id,
            place_scope_id=place_scope_id,
            owner_id=owner_id,
            relationship_id=relationship_id,
            actor_display_name=actor_display_name,
            platform=platform,
            chat_id=chat_id,
        )

        # ---- MongoDB ----
        doc = {
            "image_id": image_id,
            "file_path": file_path,
            "tags": tags,
            "user_id": user_id,
            "chat_id": chat_id,
            "storage_id": storage_id or user_id,
            "platform": platform,
            "platform_message_id": platform_message_id,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "tag_status": tag_status,
            "media_type": media_type,
            **scope_fields,
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
                                    "storage_id": storage_id or user_id,
                                    "platform": platform,
                                    "timestamp": now,
                                    "media_type": media_type,
                                    **scope_fields,
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
        storage_id: str = "",
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
    ) -> bool:
        """先写入占位元数据（无标签、不写向量），等待模型回复后再补标签。"""
        if not self.enabled:
            logger.warning("ImageStore 未启用，跳过图片占位入库。")
            return False

        now = time.time()
        scope_fields = self._scope_payload(
            memory_scope_id=memory_scope_id,
            place_scope_id=place_scope_id,
            owner_id=owner_id,
            relationship_id=relationship_id,
            actor_display_name=actor_display_name,
            platform=platform,
            chat_id=chat_id,
        )
        doc = {
            "image_id": image_id,
            "file_path": file_path,
            "tags": "",
            "user_id": user_id,
            "chat_id": chat_id,
            "storage_id": storage_id or user_id,
            "platform": platform,
            "platform_message_id": platform_message_id,
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "tag_status": "pending",
            "media_type": media_type,
            **scope_fields,
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
        description: str = "",
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        file_path: Optional[str] = None,
        platform: str = "",
        platform_message_id: Optional[str] = None,
        storage_id: str = "",
        memory_scope_id: str = "",
        place_scope_id: str = "",
        owner_id: str = "",
        relationship_id: str = "",
        actor_display_name: str = "",
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
        base_storage_id = storage_id or existing.get("storage_id") or base_user_id
        base_ts = existing.get("timestamp", now)
        # 作用域字段：显式传入优先，否则继承占位记录里已有的
        base_memory_scope = memory_scope_id or existing.get("memory_scope_id", "")
        base_place_scope = place_scope_id or existing.get("place_scope_id", "")
        base_owner = owner_id or existing.get("owner_id", "")
        base_relationship = relationship_id or existing.get("relationship_id", "")
        base_actor = actor_display_name or existing.get("actor_display_name", "")
        scope_fields = self._scope_payload(
            memory_scope_id=base_memory_scope,
            place_scope_id=base_place_scope,
            owner_id=base_owner,
            relationship_id=base_relationship,
            actor_display_name=base_actor,
            platform=platform or existing.get("platform", ""),
            chat_id=base_chat_id,
        )

        set_doc: Dict[str, Any] = {
            "tags": tags,
            "tag_status": "completed" if tags.strip() else existing.get("tag_status", "pending"),
            "updated_at": now,
            "updated_datetime": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }
        if ocr_text:
            set_doc["ocr_text"] = ocr_text
        if description:
            set_doc["description"] = description
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
        if storage_id:
            set_doc["storage_id"] = storage_id
        # 把（继承到的或新传入的）作用域字段补进文档，便于按 scope 检索
        set_doc.update(scope_fields)

        try:
            result = self.mongo_col.update_one({"image_id": image_id}, {"$set": set_doc}, upsert=False)
            if result.matched_count == 0:
                # 占位记录缺失，补一条
                fallback_doc = {
                    "image_id": image_id,
                    "file_path": base_file_path,
                    "user_id": base_user_id,
                    "chat_id": base_chat_id,
                    "storage_id": base_storage_id,
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
                                    "storage_id": base_storage_id,
                                    "platform": platform or existing.get("platform", ""),
                                    "timestamp": base_ts,
                                    "media_type": media_type_val,
                                    **scope_fields,
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
            + (f", desc='{description[:40]}...'" if description else "")
        )
        return True

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    @staticmethod
    def _legacy_optional_match(field: str, value: str) -> Dict[str, Any]:
        return {"$or": [{field: value}, {field: {"$exists": False}}, {field: ""}, {field: None}]}

    @classmethod
    def _add_optional_context_filters(
        cls,
        query: Dict[str, Any],
        *,
        platform: str = "",
        chat_id: str = "",
        storage_id: str = "",
        memory_scope_id: str = "",
    ) -> Dict[str, Any]:
        """
        给 Mongo 查询追加上下文过滤。

        跨平台接力：传入 memory_scope_id 时按共享作用域聚合（旧记录缺字段时放行），
        并忽略 platform/chat_id/storage_id 以跨平台取回；不传时保持旧的逐平台过滤。
        """
        filters = []
        if memory_scope_id:
            filters.append(cls._legacy_optional_match("memory_scope_id", memory_scope_id))
        else:
            if platform:
                filters.append(cls._legacy_optional_match("platform", platform))
            if chat_id:
                filters.append(cls._legacy_optional_match("chat_id", chat_id))
            if storage_id:
                filters.append(cls._legacy_optional_match("storage_id", storage_id))
        if not filters:
            return query
        if "$and" in query:
            query["$and"].extend(filters)
        elif "$text" in query:
            query["$and"] = filters
        elif not query:
            query["$and"] = filters
        else:
            base = dict(query)
            query.clear()
            query["$and"] = [base, *filters]
        return query

    @staticmethod
    def _payload_matches_context(
        payload: Dict[str, Any],
        *,
        platform: str = "",
        chat_id: str = "",
        storage_id: str = "",
        memory_scope_id: str = "",
    ) -> bool:
        # 跨平台接力：scope 模式只校验 memory_scope_id（旧记录缺字段放行），忽略 platform/chat_id。
        if memory_scope_id:
            actual = payload.get("memory_scope_id")
            if actual in (None, ""):
                return True
            return str(actual) == str(memory_scope_id)
        for field, expected in (("platform", platform), ("chat_id", chat_id), ("storage_id", storage_id)):
            if not expected:
                continue
            actual = payload.get(field)
            if actual in (None, ""):
                continue
            if str(actual) != str(expected):
                return False
        return True

    @staticmethod
    def _dedupe_by_image_id(results: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        seen = set()
        deduped: List[Dict[str, Any]] = []
        for item in results:
            image_id = item.get("image_id")
            key = image_id or (item.get("file_path"), item.get("timestamp"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        return deduped

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
        chat_id: Optional[str] = None,
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
            if chat_id:
                query["chat_id"] = str(chat_id)
            if platform:
                query["platform"] = platform
            return self.mongo_col.find_one(query, {"_id": 0})
        except Exception as e:
            logger.error(f"按 platform_message_id 查询图片失败: {e}")
            return None

    def search_by_time_range(
        self,
        user_id: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 20,
        storage_id: str = "",
        platform: str = "",
        chat_id: str = "",
        memory_scope_id: str = "",
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """按时间范围查询图片（MongoDB）。offset 用于翻页跳过前 N 条。"""
        if self.mongo_col is None:
            return []
        try:
            query: Dict[str, Any] = {}
            query = self._add_optional_context_filters(
                query,
                platform=platform,
                chat_id=chat_id,
                storage_id=storage_id,
                memory_scope_id=memory_scope_id,
            )
            if user_id:
                query["user_id"] = user_id
            time_filter: Dict[str, Any] = {}
            if start_time is not None:
                time_filter["$gte"] = start_time
            if end_time is not None:
                time_filter["$lte"] = end_time
            if time_filter:
                query["timestamp"] = time_filter

            cursor = self.mongo_col.find(query, {"_id": 0}).sort("timestamp", -1)
            if offset > 0:
                cursor = cursor.skip(int(offset))
            cursor = cursor.limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f"按时间范围查询图片失败: {e}")
            return []

    def search_by_keyword(
        self,
        keyword: str,
        user_id: str = "",
        limit: int = 10,
        storage_id: str = "",
        platform: str = "",
        chat_id: str = "",
        memory_scope_id: str = "",
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """按关键词在 tags 中做文本搜索（MongoDB $text）。offset 用于翻页。"""
        if self.mongo_col is None:
            return []
        try:
            query: Dict[str, Any] = {"$text": {"$search": keyword}}
            query = self._add_optional_context_filters(
                query,
                platform=platform,
                chat_id=chat_id,
                storage_id=storage_id,
                memory_scope_id=memory_scope_id,
            )
            if user_id:
                query["user_id"] = user_id
            cursor = (
                self.mongo_col.find(query, {"_id": 0, "score": {"$meta": "textScore"}})
                .sort([("score", {"$meta": "textScore"})])
            )
            if offset > 0:
                cursor = cursor.skip(int(offset))
            cursor = cursor.limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f"关键词搜索图片失败: {e}")
            return []

    def search_by_ocr_text(
        self,
        text_query: str,
        user_id: str = "",
        limit: int = 10,
        storage_id: str = "",
        platform: str = "",
        chat_id: str = "",
        memory_scope_id: str = "",
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        在图片 OCR 文字中做模糊搜索（支持类搜索引擎的模糊匹配）。

        搜索规则（较强的模糊搜索）：
        1. 使用 MongoDB $regex 进行正则匹配
        2. 支持多词搜索：将查询拆分为关键词，每个词独立匹配（AND 逻辑）
        3. 忽略大小写
        4. 支持部分匹配（子串匹配）
        5. 忽略空格和标点差异
        6. offset 用于翻页跳过前 N 条
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
            query = self._add_optional_context_filters(
                query,
                platform=platform,
                chat_id=chat_id,
                storage_id=storage_id,
                memory_scope_id=memory_scope_id,
            )
            if user_id:
                if "$and" in query:
                    query["$and"].append({"user_id": user_id})
                else:
                    query["user_id"] = user_id

            cursor = self.mongo_col.find(query, {"_id": 0}).sort("timestamp", -1)
            if offset > 0:
                cursor = cursor.skip(int(offset))
            cursor = cursor.limit(limit)
            return list(cursor)
        except Exception as e:
            logger.error(f"OCR 文字搜索图片失败: {e}")
            return []

    def search_by_semantic(
        self,
        query_text: str,
        user_id: str = "",
        top_k: int = 5,
        storage_id: str = "",
        platform: str = "",
        chat_id: str = "",
        memory_scope_id: str = "",
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """语义向量检索（Qdrant）。offset 用于翻页（在上下文过滤后按序跳过）。"""
        if self.qdrant is None or not self.embed_client.enabled:
            return []
        try:
            vector = self.embed_client.get_embedding(query_text)
            if not vector:
                return []

            qdrant_filter = None
            must_conditions = []
            if user_id:
                must_conditions.append(qdrant_models.FieldCondition(key="user_id", match=qdrant_models.MatchValue(value=user_id)))
            if must_conditions:
                qdrant_filter = qdrant_models.Filter(must=must_conditions)

            offset = max(0, int(offset or 0))
            response = self.qdrant.query_points(
                collection_name=IMAGE_COLLECTION,
                query=vector,
                query_filter=qdrant_filter,
                limit=top_k + offset,
            )
            results = []
            for hit in response.points:
                item = dict(hit.payload)
                if not self._payload_matches_context(
                    item,
                    platform=platform,
                    chat_id=chat_id,
                    storage_id=storage_id,
                    memory_scope_id=memory_scope_id,
                ):
                    continue
                item["score"] = hit.score
                results.append(item)
                if len(results) >= top_k + offset:
                    break
            return results[offset:offset + top_k]
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
        storage_id: str = "",
        platform: str = "",
        chat_id: str = "",
        memory_scope_id: str = "",
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        统一检索入口 —— 自动选择最佳策略：
        1. 有 image_id   → 精确查询
        2. 有 text_query  → 在图片 OCR 文字中做模糊搜索
        3. 有 keyword     → 先尝试语义搜索，再回退关键词搜索
        4. 有时间范围     → 时间过滤
        5. 什么都没有     → 返回最近的图片/视频

        media_type: 可选 "image"/"video"/""（空=不过滤）
        offset: 结果偏移量（翻页用）。image_id 精确查询时忽略。
        当 text_query 和 keyword 同时存在时，text_query 结果优先，
        keyword 结果作为补充（去重合并）。
        """
        # 精确查询
        if image_id:
            doc = self.get_by_id(image_id)
            return [doc] if doc else []

        offset = max(0, int(offset or 0))

        def _apply_media_filter(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not media_type:
                return results
            return [r for r in results if r.get("media_type", "image") == media_type]

        def _context_kwargs(*, include_user: bool = True, limit_key: str = "limit") -> Dict[str, Any]:
            kwargs: Dict[str, Any] = {}
            if include_user:
                kwargs["user_id"] = user_id
            kwargs[limit_key] = limit
            kwargs["offset"] = offset
            if storage_id:
                kwargs["storage_id"] = storage_id
            if platform:
                kwargs["platform"] = platform
            if chat_id:
                kwargs["chat_id"] = chat_id
            if memory_scope_id:
                kwargs["memory_scope_id"] = memory_scope_id
            return kwargs

        # OCR 文字搜索 + 关键词搜索组合
        # 合并路径需要先取到 offset+limit 条再统一切片，否则两路各自跳过会错位。
        if text_query and keyword:
            merged_kwargs = _context_kwargs()
            merged_kwargs["limit"] = limit + offset
            merged_kwargs["offset"] = 0
            semantic_kwargs = dict(merged_kwargs)
            semantic_kwargs["top_k"] = semantic_kwargs.pop("limit")

            ocr_results = self.search_by_ocr_text(text_query, **merged_kwargs)
            keyword_results = self.search_by_semantic(keyword, **semantic_kwargs)
            if not keyword_results:
                keyword_results = self.search_by_keyword(keyword, **merged_kwargs)
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
            return _apply_media_filter(merged[offset:offset + limit])

        # 纯 OCR 文字搜索
        if text_query:
            return _apply_media_filter(self.search_by_ocr_text(text_query, **_context_kwargs()))

        # 语义 + 关键词
        if keyword:
            semantic_results = self.search_by_semantic(keyword, **_context_kwargs(limit_key="top_k"))
            if semantic_results:
                # Qdrant 语义搜索结果需要从 MongoDB 补全 ocr_text 等字段
                return _apply_media_filter(self._enrich_with_mongo(semantic_results))
            return _apply_media_filter(self.search_by_keyword(keyword, **_context_kwargs()))

        # 时间范围
        if start_time is not None or end_time is not None:
            time_kwargs = _context_kwargs(limit_key="limit")
            time_kwargs.pop("user_id", None)
            return _apply_media_filter(self.search_by_time_range(user_id, start_time, end_time, **time_kwargs))

        # 默认：最近的图片/视频
        time_kwargs = _context_kwargs(limit_key="limit")
        time_kwargs.pop("user_id", None)
        return _apply_media_filter(self.search_by_time_range(user_id, **time_kwargs))

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
