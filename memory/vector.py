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
        except Exception as e:
            logger.error(f"检查/创建 Collection 失败: {e}")

    def upsert(self, text: str, vector: List[float], metadata: Dict[str, Any] = None) -> bool:
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
            # 可以在这里添加 filter_criteria 的处理逻辑 (Qdrant Filter)
            # 目前暂时只做纯向量搜索
            search_result = self.client.search(
                collection_name=self.collection_name,
                query_vector=vector,
                limit=top_k
            )

            results = []
            for hit in search_result:
                item = hit.payload
                item['score'] = hit.score
                results.append(item)
            
            return results

        except Exception as e:
            logger.error(f"向量搜索失败: {e}")
            return []
