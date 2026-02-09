'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 15:39:23
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
测试 CLI 中的模型价格显示功能
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 直接使用 cost_tracker，避免导入整个 CLI
from core.cost_tracker import CostTracker

def get_model_price_info_local(provider: str, model_name: str) -> str:
    """获取模型的价格信息（用于显示）"""
    tracker = CostTracker(db_path=":memory:")  # 仅用于查询价格
    prices = tracker.get_model_price(provider, model_name)
    
    if not prices:
        return ""
    
    input_price = prices["input"]
    output_price = prices["output"]
    
    if input_price == 0 and output_price == 0:
        return " 💚 FREE"
    
    # 格式化价格显示
    if input_price < 1 and output_price < 1:
        return f" 💰 ${input_price:.3f}/${output_price:.2f} per M"
    else:
        return f" 💰 ${input_price:.2f}/${output_price:.2f} per M"


def test_price_display():
    """测试价格信息格式化显示"""
    print("🧪 测试模型价格显示格式...")
    
    # 测试 Gemini 模型
    print("\n📝 Gemini 模型:")
    tests = [
        ("gemini", "gemini-1.5-pro-latest"),
        ("gemini", "gemini-1.5-flash-latest"),
        ("gemini", "gemini-2.0-flash-exp"),
        ("gemini", "gemini-exp-1206"),
    ]
    
    for provider, model in tests:
        price_info = get_model_price_info_local(provider, model)
        print(f"  {model:35s} {price_info}")
    
    # 测试 OpenAI 模型
    print("\n📝 OpenAI 模型:")
    tests = [
        ("openai", "gpt-4o"),
        ("openai", "gpt-4o-mini"),
        ("openai", "gpt-4-turbo"),
        ("openai", "gpt-3.5-turbo"),
        ("openai", "o1-preview"),
        ("openai", "o1-mini"),
    ]
    
    for provider, model in tests:
        price_info = get_model_price_info_local(provider, model)
        print(f"  {model:35s} {price_info}")
    
    # 测试未知模型
    print("\n📝 未知模型:")
    price_info = get_model_price_info_local("gemini", "unknown-model-xyz")
    print(f"  unknown-model-xyz{price_info} (应该为空)")
    
    print("\n✅ 测试完成！")


if __name__ == "__main__":
    test_price_display()
