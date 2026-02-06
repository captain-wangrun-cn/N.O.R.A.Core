import requests
import logging
import time
from typing import List, Optional
import config

logger = logging.getLogger(__name__)

class EmbeddingClient:
    """
    负责将文本转换为向量 (Embeddings)。
    支持任何兼容 OpenAI API 格式的提供商 (如 SiliconFlow, DeepSeek, OpenAI)。
    """

    def __init__(self):
        cfg = config.get_config().get("memory", {}).get("embedding", {})
        
        self.api_key = cfg.get("api_key")
        self.base_url = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
        self.model = cfg.get("model", "text-embedding-3-small")
        self.dimensions = cfg.get("dimensions", 1536)

        if not self.api_key or "YOUR_" in self.api_key:
            logger.warning("Embedding API Key 未配置！记忆系统将无法工作。")
            self.enabled = False
        else:
            self.enabled = True
            logger.info(f"EmbeddingClient 已初始化。Provider: {self.base_url}, Model: {self.model}")

    def get_embedding(self, text: str) -> Optional[List[float]]:
        """
        获取单个文本的 Embedding 向量。
        """
        if not self.enabled:
            return None
            
        # 简单的预处理：去除多余空白，限制长度防止超标（BGE-M3 支持 8k，但为了安全限制在 8000 字符）
        text = text.replace("\n", " ").strip()[:8000]
        
        if not text:
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "input": text,
            "model": self.model,
            "encoding_format": "float"
        }

        # 简单的重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    f"{self.base_url}/embeddings",
                    headers=headers,
                    json=payload,
                    timeout=10
                )
                response.raise_for_status()
                
                data = response.json()
                embedding = data["data"][0]["embedding"]
                
                # 简单的维度检查
                if len(embedding) != self.dimensions:
                    logger.warning(f"Embedding 维度不匹配！期望 {self.dimensions}, 实际 {len(embedding)}")
                
                return embedding

            except Exception as e:
                logger.warning(f"获取 Embedding 失败 (尝试 {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1) #稍作等待
                else:
                    logger.error("Embedding 服务最终调用失败。", exc_info=True)
                    return None
