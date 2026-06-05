import logging
import uuid
import time
from typing import List, Dict, Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.http import models
import config

logger = logging.getLogger(__name__)

class VectorStore:
    """
    Qdrant 向量数据库的封装层。
    负责存储和检索记忆片段。
    """

    def __init__(self):
        mem_cfg = config.get_config().get("memory", {})
        qdrant_cfg = mem_cfg.get("qdrant", {})
        embed_cfg = mem_cfg.get("embedding", {})

        self.host = qdrant_cfg.get("host", "localhost")
        self.port = qdrant_cfg.get("port", 6333)
        self.collection_name = "nora_memory"
        self.vector_size = embed_cfg.get("dimensions", 1536) # 默认为 1536 (OpenAI small)

        try:
            self.client = QdrantClient(host=self.host, port=self.port)
            self._ensure_collection()
            logger.info(f"VectorStore 已连接到 Qdrant ({self.host}:{self.port})。")
        except Exception as e:
            logger.error(f"无法连接到 Qdrant: {e}")
            self.client = None

    def _ensure_collection(self):
        """确保 Collection 存在，不存在则创建。"""
        if not self.client: return

        try:
            collections = self.client.get_collections()
            exists = any(c.name == self.collection_name for c in collections.collections)

            if not exists:
                logger.info(f"Collection '{self.collection_name}' 不存在，正在创建...")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.vector_size,
                        distance=models.Distance.COSINE
                    )
                )
                logger.info(f"Collection '{self.collection_name}' 创建成功。")
            for field in ("user_id", "platform", "chat_id", "storage_id", "chat_type"):
                self._ensure_payload_index(field)
            logger.info(f"Collection '{self.collection_name}' 会话维度索引已确认。")
        except Exception as e:
            logger.error(f"检查/创建 Collection 失败: {e}")

    def _ensure_payload_index(self, field_name: str) -> None:
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD
            )
        except Exception as e:
            logger.debug(f"VectorStore payload 索引已存在或创建失败: {field_name}, {e}")

    def upsert(self, text: str, vector: List[float], metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        插入或更新一条记忆。
        """
        if not self.client: return False
        
        point_id = str(uuid.uuid4()) # 生成唯一的 UUID
        payload = {"text": text, "timestamp": time.time()}
        if metadata:
            payload.update(metadata)

        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload
                    )
                ]
            )
            return True
        except Exception as e:
            logger.error(f"写入向量失败: {e}")
            return False

    def query(self, vector: List[float], top_k: int = 5, filter_criteria: Dict = None) -> List[Dict[str, Any]]:
        """
        根据向量搜索最近邻。
        返回结果列表: [{'text': '...', 'score': 0.85, ...}]
        """
        if not self.client: return []

        try:
            qdrant_filter = None
            if filter_criteria:
                qdrant_filter = models.Filter(
                    must=[
                        models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=value)
                        ) for key, value in filter_criteria.items()
                    ]
                )

            # 适配 Qdrant Client v1.10+ (search 方法可能被 query_points 替代或环境差异)
            # 使用 query_points 接口
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                query_filter=qdrant_filter,
                limit=top_k
            )
            
            # query_points 返回的是 QueryResponse 对象，包含 points 列表
            search_result = response.points

            results = []
            for hit in search_result:
                item = hit.payload
                item['score'] = hit.score
                results.append(item)
            
            return results

        except Exception as e:
            logger.error(f"向量搜索失败: {e}")
            return []
