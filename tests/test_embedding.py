import sys
import os
import logging

# 将项目根目录添加到 python path，以便能导入 memory 模块
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from memory.embed import EmbeddingClient
import config

# 配置日志输出到控制台
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestEmbedding")

def run_test():
    logger.info("开始测试 Embedding 服务...")
    
    # 1. 初始化 Client
    try:
        client = EmbeddingClient()
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        return

    if not client.enabled:
        logger.warning("EmbeddingClient 未启用。请检查 config.yml 中的 api_key 是否配置。")
        return

    # 2. 发送测试文本
    test_text = "Nora is a reliable maid."
    logger.info(f"正在获取文本 '{test_text}' 的向量...")
    
    vector = client.get_embedding(test_text)
    
    if vector:
        logger.info("✅ 测试成功！")
        logger.info(f"获取到的向量维度: {len(vector)}")
        logger.info(f"向量前 5 位: {vector[:5]}")
        
        # 验证维度是否符合预期
        expected_dim = client.dimensions
        if len(vector) == expected_dim:
            logger.info("维度检查通过。")
        else:
            logger.warning(f"维度不匹配！期望 {expected_dim}, 实际 {len(vector)}")
            
    else:
        logger.error("❌ 测试失败：未能获取到向量。请检查网络或 API Key。")

if __name__ == "__main__":
    run_test()
