'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 15:58:52
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
测试上下文长度相关的定价
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.cost_tracker import CostTracker


def test_short_context_pricing():
    """测试短上下文定价 (<=200k tokens)"""
    tracker = CostTracker(db_path=":memory:")
    
    # Gemini 1.5 Pro - 短上下文 (100k tokens)
    input_cost, output_cost, total = tracker.calculate_cost(
        "gemini", "gemini-1.5-pro-latest", 100_000, 50_000
    )
    # 应该使用 -short 定价: $1.25/$5.00
    expected_input = (100_000 / 1_000_000) * 1.25
    expected_output = (50_000 / 1_000_000) * 5.00
    assert abs(input_cost - expected_input) < 0.001, f"Expected {expected_input}, got {input_cost}"
    assert abs(output_cost - expected_output) < 0.001, f"Expected {expected_output}, got {output_cost}"
    print(f"✓ Pro短上下文 (100k): ${total:.4f} (input: ${input_cost:.4f}, output: ${output_cost:.4f})")
    
    # Gemini 1.5 Flash - 短上下文 (150k tokens)
    input_cost, output_cost, total = tracker.calculate_cost(
        "gemini", "gemini-1.5-flash", 150_000, 30_000
    )
    # 应该使用 -short 定价: $0.075/$0.30
    expected_input = (150_000 / 1_000_000) * 0.075
    expected_output = (30_000 / 1_000_000) * 0.30
    assert abs(input_cost - expected_input) < 0.001, f"Expected {expected_input}, got {input_cost}"
    assert abs(output_cost - expected_output) < 0.001, f"Expected {expected_output}, got {output_cost}"
    print(f"✓ Flash短上下文 (150k): ${total:.4f} (input: ${input_cost:.4f}, output: ${output_cost:.4f})")


def test_long_context_pricing():
    """测试长上下文定价 (>200k tokens)"""
    tracker = CostTracker(db_path=":memory:")
    
    # Gemini 1.5 Pro - 长上下文 (500k tokens)
    input_cost, output_cost, total = tracker.calculate_cost(
        "gemini", "gemini-1.5-pro-latest", 500_000, 50_000
    )
    # 应该使用 -long 定价: $2.50/$10.00
    expected_input = (500_000 / 1_000_000) * 2.50
    expected_output = (50_000 / 1_000_000) * 10.00
    assert abs(input_cost - expected_input) < 0.001, f"Expected {expected_input}, got {input_cost}"
    assert abs(output_cost - expected_output) < 0.001, f"Expected {expected_output}, got {output_cost}"
    print(f"✓ Pro长上下文 (500k): ${total:.4f} (input: ${input_cost:.4f}, output: ${output_cost:.4f})")
    
    # Gemini 1.5 Flash - 长上下文 (300k tokens)
    input_cost, output_cost, total = tracker.calculate_cost(
        "gemini", "gemini-1.5-flash", 300_000, 30_000
    )
    # 应该使用 -long 定价: $0.15/$0.60
    expected_input = (300_000 / 1_000_000) * 0.15
    expected_output = (30_000 / 1_000_000) * 0.60
    assert abs(input_cost - expected_input) < 0.001, f"Expected {expected_input}, got {input_cost}"
    assert abs(output_cost - expected_output) < 0.001, f"Expected {expected_output}, got {output_cost}"
    print(f"✓ Flash长上下文 (300k): ${total:.4f} (input: ${input_cost:.4f}, output: ${output_cost:.4f})")


def test_boundary_cases():
    """测试边界情况 (200k tokens)"""
    tracker = CostTracker(db_path=":memory:")
    
    # 正好 200k - 应该使用 short
    input_cost, output_cost, _ = tracker.calculate_cost(
        "gemini", "gemini-1.5-pro", 200_000, 10_000
    )
    expected_input = (200_000 / 1_000_000) * 1.25  # short pricing
    assert abs(input_cost - expected_input) < 0.001
    print(f"✓ 边界 200k tokens: 使用 short 定价 (${input_cost:.4f})")
    
    # 200001 - 应该使用 long
    input_cost, output_cost, _ = tracker.calculate_cost(
        "gemini", "gemini-1.5-pro", 200_001, 10_000
    )
    expected_input = (200_001 / 1_000_000) * 2.50  # long pricing
    assert abs(input_cost - expected_input) < 0.001
    print(f"✓ 边界 200001 tokens: 使用 long 定价 (${input_cost:.4f})")


def test_model_name_normalization():
    """测试模型名称规范化"""
    tracker = CostTracker(db_path=":memory:")
    
    # 测试短上下文
    normalized = tracker._normalize_model_name("gemini", "gemini-1.5-pro-latest", 100_000)
    assert normalized == "gemini-1.5-pro-latest-short", f"Expected -short suffix, got {normalized}"
    print(f"✓ 规范化: gemini-1.5-pro-latest (100k) -> {normalized}")
    
    # 测试长上下文
    normalized = tracker._normalize_model_name("gemini", "gemini-1.5-flash", 500_000)
    assert normalized == "gemini-1.5-flash-long", f"Expected -long suffix, got {normalized}"
    print(f"✓ 规范化: gemini-1.5-flash (500k) -> {normalized}")
    
    # 测试已有后缀的情况
    normalized = tracker._normalize_model_name("gemini", "gemini-1.5-pro-short", 500_000)
    assert normalized == "gemini-1.5-pro-short", f"Should keep existing suffix, got {normalized}"
    print(f"✓ 规范化: gemini-1.5-pro-short (已有后缀) -> {normalized}")
    
    # 测试其他模型不受影响
    normalized = tracker._normalize_model_name("gemini", "gemini-2.0-flash-exp", 500_000)
    assert normalized == "gemini-2.0-flash-exp", f"Should not add suffix to 2.0 models, got {normalized}"
    print(f"✓ 规范化: gemini-2.0-flash-exp (不添加后缀) -> {normalized}")
    
    # 测试 OpenAI 模型不受影响
    normalized = tracker._normalize_model_name("openai", "gpt-4o", 500_000)
    assert normalized == "gpt-4o", f"Should not add suffix to OpenAI models, got {normalized}"
    print(f"✓ 规范化: gpt-4o (OpenAI, 不添加后缀) -> {normalized}")


def test_logging_with_context_length():
    """测试日志记录时正确使用上下文长度"""
    import tempfile
    import os
    
    # 使用临时文件而不是 :memory: 数据库
    fd, temp_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    try:
        tracker = CostTracker(db_path=temp_path)
        
        # 记录短上下文使用
        tracker.log_usage("gemini", "gemini-1.5-pro", 100_000, 50_000, "smart", "test")
        
        # 记录长上下文使用
        tracker.log_usage("gemini", "gemini-1.5-flash", 300_000, 30_000, "fast", "test")
        
        # 获取统计
        stats = tracker.get_stats()
        
        # 检查是否正确记录了规范化的模型名称
        models_used = set(stats["by_model"].keys())
        assert "gemini-1.5-pro-short" in models_used, f"Should log with -short suffix, got {models_used}"
        assert "gemini-1.5-flash-long" in models_used, f"Should log with -long suffix, got {models_used}"
        
        print(f"✓ 日志记录: 正确使用规范化的模型名称")
        print(f"  记录的模型: {models_used}")
    finally:
        # 清理临时文件
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except PermissionError:
            pass  # Windows 可能需要等待连接关闭


def test_price_display_helper():
    """测试 CLI 价格显示辅助函数能正确处理 -short/-long 模型"""
    tracker = CostTracker(db_path=":memory:")
    
    # 测试获取 short 模型价格
    price = tracker.get_model_price("gemini", "gemini-1.5-pro-short")
    assert price == {"input": 1.25, "output": 5.00}
    print(f"✓ 获取 Pro-short 价格: ${price['input']}/${price['output']}")
    
    # 测试获取 long 模型价格
    price = tracker.get_model_price("gemini", "gemini-1.5-flash-long")
    assert price == {"input": 0.15, "output": 0.60}
    print(f"✓ 获取 Flash-long 价格: ${price['input']}/${price['output']}")


if __name__ == "__main__":
    print("=" * 60)
    print("测试上下文长度相关的定价")
    print("=" * 60)
    
    print("\n1. 测试短上下文定价 (<=200k)")
    print("-" * 60)
    test_short_context_pricing()
    
    print("\n2. 测试长上下文定价 (>200k)")
    print("-" * 60)
    test_long_context_pricing()
    
    print("\n3. 测试边界情况 (200k)")
    print("-" * 60)
    test_boundary_cases()
    
    print("\n4. 测试模型名称规范化")
    print("-" * 60)
    test_model_name_normalization()
    
    print("\n5. 测试日志记录")
    print("-" * 60)
    test_logging_with_context_length()
    
    print("\n6. 测试价格显示辅助函数")
    print("-" * 60)
    test_price_display_helper()
    
    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)
