import sys
import os
import logging
import time
import uuid

# 添加项目根目录到 Path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from memory.rag import RAGEngine
import config

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestRAG")

def run_test():
    logger.info("开始测试 RAGEngine (集成测试)...")

    # 1. 初始化
    rag = RAGEngine()
    if not rag.enabled:
        logger.error("❌ RAGEngine 未就绪。请检查 config.yml 中的 Embedding 和 Qdrant 配置。")
        return

    # 2. 准备测试数据 (使用 UUID 防止和之前的测试混淆)
    unique_id = str(uuid.uuid4())[:8]
    secret_code = f"Project-Zero-{unique_id}"
    test_memory = f"Nora's secret launch code is {secret_code}."
    
    logger.info(f"正在存入记忆: '{test_memory}'")

    # 3. 存储记忆
    success = rag.add_memory(
        text=test_memory, 
        metadata={"role": "system", "type": "secret"}
    )
    
    if success:
        logger.info("✅ 记忆存储调用成功。")
    else:
        logger.error("❌ 记忆存储失败。")
        return

    # 等待一小会儿让数据库索引刷新 (虽然 Qdrant 很快，但稳妥起见)
    time.sleep(1)

    # 4. 检索记忆 (使用相关的问题)
    query = "What is the launch code?"
    logger.info(f"正在提问: '{query}'")
    
    context = rag.get_context_string(query, top_k=3)
    
    logger.info(f"检索到的上下文:\n---\n{context}\n---")

    # 5. 验证
    if secret_code in context:
        logger.info("✅ 测试通过！成功通过语义检索找回了刚存入的秘密代码。")
    else:
        logger.warning(f"❌ 测试失败！未能找回秘密代码 '{secret_code}'。")
        logger.warning("可能原因：Embedding 尚未生效，或相似度阈值过滤太严格。")

if __name__ == "__main__":
    run_test()
