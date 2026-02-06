import sys
import os
import logging
import random
import time

# 添加项目根目录到 Path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from memory.vector import VectorStore
import config

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TestVector")

def run_test():
    logger.info("开始测试 VectorStore (Qdrant)...")

    # 1. 初始化
    try:
        store = VectorStore()
    except Exception as e:
        logger.error(f"初始化失败: {e}")
        return

    if not store.client:
        logger.error("❌ 无法连接到 Qdrant，测试中止。请检查 docker 容器是否运行。")
        return

    # 2. 准备测试数据
    test_text = f"Test Memory at {time.time()}"
    dim = store.vector_size
    # 生成一个随机向量 (模拟 Embedding)
    fake_vector = [random.random() for _ in range(dim)]
    
    logger.info(f"正在插入测试数据 (Text: '{test_text}', Dim: {dim})...")
    
    # 3. 插入数据
    success = store.upsert(
        text=test_text,
        vector=fake_vector,
        metadata={"source": "unit_test", "author": "Nora"}
    )

    if success:
        logger.info("✅ 插入成功！")
    else:
        logger.error("❌ 插入失败！")
        return

    # 4. 检索数据
    logger.info("正在尝试检索刚才插入的数据...")
    # 为了保证能搜到，我们直接用刚才那个向量去搜
    results = store.query(vector=fake_vector, top_k=1)
    
    if results:
        top_match = results[0]
        logger.info(f"检索结果: {top_match['text']} (Score: {top_match['score']})")
        
        if top_match['text'] == test_text:
            logger.info("✅ 内容匹配成功！读写闭环验证通过。")
        else:
            logger.warning(f"内容不匹配！期望 '{test_text}', 实际 '{top_match['text']}'")
    else:
        logger.error("❌ 检索失败，没有返回结果。")

if __name__ == "__main__":
    run_test()
