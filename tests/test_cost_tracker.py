'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 15:35:26
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
测试成本跟踪功能
"""
import os
import sys
import tempfile
from core.cost_tracker import CostTracker

def test_basic_tracking():
    """测试基本的成本跟踪功能"""
    print("🧪 测试基本成本跟踪功能...")
    
    # 使用临时数据库
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        # 创建 tracker
        tracker = CostTracker(db_path=db_path)
        
        # 测试 1: 记录 Gemini 调用
        print("\n📝 测试 1: 记录 Gemini 调用")
        tracker.log_usage(
            provider="gemini",
            model="gemini-1.5-pro-latest",
            input_tokens=1000,
            output_tokens=500,
            model_alias="smart",
            context="test_chat"
        )
        
        # 测试 2: 记录 OpenAI 调用
        print("📝 测试 2: 记录 OpenAI 调用")
        tracker.log_usage(
            provider="openai",
            model="gpt-4o",
            input_tokens=2000,
            output_tokens=1000,
            model_alias="smart",
            context="test_tool_call"
        )
        
        # 测试 3: 记录免费模型调用
        print("📝 测试 3: 记录免费模型调用")
        tracker.log_usage(
            provider="gemini",
            model="gemini-2.0-flash-exp",
            input_tokens=5000,
            output_tokens=3000,
            model_alias="fast",
            context="test_free"
        )
        
        # 测试 4: 自定义价格
        print("📝 测试 4: 自定义价格模型")
        custom_tracker = CostTracker(
            db_path=db_path,
            custom_prices={
                "my-custom-model": {"input": 0.5, "output": 2.0}
            }
        )
        custom_tracker.log_usage(
            provider="custom",
            model="my-custom-model",
            input_tokens=1000,
            output_tokens=500,
            model_alias="custom",
            context="test_custom"
        )
        
        # 查看统计
        print("\n" + "="*60)
        tracker.print_stats("total")
        
        # 测试筛选
        print("\n📊 测试筛选统计 - 仅 smart 模型:")
        stats = tracker.get_stats(period="total", model_alias="smart")
        print(f"  Calls: {stats['total_calls']}")
        print(f"  Cost: ${stats['total_cost']:.6f}")
        
        print("\n✅ 所有测试通过！")
        
    finally:
        # 清理临时文件
        if os.path.exists(db_path):
            os.unlink(db_path)
            print(f"\n🗑️  已删除临时数据库: {db_path}")


def test_price_calculation():
    """测试价格计算准确性"""
    print("\n🧮 测试价格计算...")
    
    tracker = CostTracker(db_path=":memory:")
    
    # Gemini Pro: $1.25/M input, $5.00/M output
    # 1000 input + 500 output = (1000/1M)*1.25 + (500/1M)*5.00 = 0.00125 + 0.0025 = 0.00375
    input_cost, output_cost, total = tracker.calculate_cost(
        "gemini", "gemini-1.5-pro-latest", 1000, 500
    )
    
    expected = 0.00375
    assert abs(total - expected) < 0.000001, f"Expected ${expected:.6f}, got ${total:.6f}"
    print(f"  ✅ Gemini Pro 计算正确: ${total:.6f}")
    
    # GPT-4o: $2.50/M input, $10.00/M output
    # 2000 input + 1000 output = (2000/1M)*2.50 + (1000/1M)*10.00 = 0.005 + 0.01 = 0.015
    input_cost, output_cost, total = tracker.calculate_cost(
        "openai", "gpt-4o", 2000, 1000
    )
    
    expected = 0.015
    assert abs(total - expected) < 0.000001, f"Expected ${expected:.6f}, got ${total:.6f}"
    print(f"  ✅ GPT-4o 计算正确: ${total:.6f}")
    
    # 免费模型
    input_cost, output_cost, total = tracker.calculate_cost(
        "gemini", "gemini-2.0-flash-exp", 10000, 5000
    )
    
    assert total == 0.0, f"Free model should cost $0, got ${total:.6f}"
    print(f"  ✅ 免费模型计算正确: ${total:.6f}")
    
    print("  ✅ 所有价格计算测试通过！")


if __name__ == "__main__":
    test_price_calculation()
    test_basic_tracking()
