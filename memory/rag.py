import logging
from typing import List, Dict, Any, Optional

from memory.embed import EmbeddingClient
from memory.vector import VectorStore

logger = logging.getLogger(__name__)

class RAGEngine:
    """
    RAG (检索增强生成) 引擎。
    作为记忆系统的统一入口，协调 EmbeddingClient 和 VectorStore。
    """

    def __init__(self):
        self.embed_client = EmbeddingClient()
        self.vector_store = VectorStore()
        
        # 检查依赖组件状态
        self.enabled = self.embed_client.enabled and (self.vector_store.client is not None)
        if self.enabled:
            logger.info("RAGEngine 已就绪。记忆系统在线。")
        else:
            logger.warning("RAGEngine 未就绪 (Embedding 或 VectorStore 异常)。记忆系统已降级为仅短期记忆。")

    def add_memory(self, text: str, metadata: Dict[str, Any] = None) -> bool:
        """
        存储一条新记忆。
        过程: Text -> Vector -> DB
        """
        if not self.enabled or not text.strip():
            return False

        try:
            # 1. 获取向量
            vector = self.embed_client.get_embedding(text)
            if not vector:
                logger.warning("无法获取向量，记忆存储失败。")
                return False

            # 2. 存入数据库
            return self.vector_store.upsert(text, vector, metadata)
            
        except Exception as e:
            logger.error(f"存储记忆时发生错误: {e}")
            return False

    def retrieve_memory(self, query_text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        检索相关记忆。
        过程: Query -> Vector -> DB Search
        """
        if not self.enabled or not query_text.strip():
            return []

        try:
            # 1. 获取查询向量
            vector = self.embed_client.get_embedding(query_text)
            if not vector:
                return []

            # 2. 搜索数据库
            results = self.vector_store.query(vector, top_k)
            return results
            
        except Exception as e:
            logger.error(f"检索记忆时发生错误: {e}")
            return []

    def get_context_string(self, query_text: str, top_k: int = 5) -> str:
        """
        [Helper] 检索并格式化为适合注入 Prompt 的字符串。
        """
        memories = self.retrieve_memory(query_text, top_k)
        if not memories:
            return ""

        # 格式化逻辑
        context_parts = []
        for mem in memories:
            # 过滤掉相似度太低的结果 (阈值可调，暂定 0.4)
            if mem.get('score', 0) < 0.4: 
                continue
                
            # 尝试获取元数据中的时间
            # ts = mem.get('timestamp') ... (可以在这里做时间格式化)
            
            source = f"[{mem.get('role', 'memory')}]" # e.g. [user] or [assistant]
            context_parts.append(f"- {source}: {mem['text']}")

        if not context_parts:
            return ""

        return "\n".join(context_parts)
