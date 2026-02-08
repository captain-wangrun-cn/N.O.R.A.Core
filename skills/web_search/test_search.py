'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 01:04:50
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
#!/usr/bin/env python3
"""
Web Search Skill 测试脚本
用于验证 Tavily API 集成是否正常工作
"""

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_basic_search():
    """测试基础搜索功能"""
    print("=" * 60)
    print("测试 1: 基础搜索")
    print("=" * 60)
    
    from skills.web_search.main import run
    
    result = run(
        query="Python programming language",
        max_results=3,
        search_depth="basic",
        output_format="text"
    )
    
    print(f"\n返回码: {result}")
    print()


def test_advanced_search():
    """测试深度搜索"""
    print("=" * 60)
    print("测试 2: 深度搜索（中文）")
    print("=" * 60)
    
    from skills.web_search.main import run
    
    result = run(
        query="人工智能最新进展",
        max_results=5,
        search_depth="advanced",
        include_answer=True,
        output_format="text"
    )
    
    print(f"\n返回码: {result}")
    print()


def test_json_output():
    """测试 JSON 格式输出"""
    print("=" * 60)
    print("测试 3: JSON 格式输出")
    print("=" * 60)
    
    from skills.web_search.main import run
    
    result = run(
        query="Large Language Model",
        max_results=2,
        search_depth="basic",
        include_answer=True,
        output_format="json"
    )
    
    print(f"\n返回码: {result}")
    print()


def test_no_answer():
    """测试不包含 AI 答案"""
    print("=" * 60)
    print("测试 4: 仅搜索结果（无 AI 答案）")
    print("=" * 60)
    
    from skills.web_search.main import run
    
    result = run(
        query="GitHub",
        max_results=3,
        include_answer=False,
        output_format="text"
    )
    
    print(f"\n返回码: {result}")
    print()


def test_error_handling():
    """测试错误处理"""
    print("=" * 60)
    print("测试 5: 错误处理（空查询）")
    print("=" * 60)
    
    from skills.web_search.main import run
    
    result = run(
        query="",
        max_results=3,
        output_format="json"
    )
    
    print(f"\n返回码: {result} (应该是 1)")
    print()


def main():
    """运行所有测试"""
    print("\n🧪 Web Search Skill 测试套件\n")
    
    # 检查配置
    print("📋 检查配置...")
    from skills.web_search.main import load_config
    config = load_config()
    
    api_key = config.get("tavily", {}).get("api_key") or os.environ.get("TAVILY_API_KEY")
    
    if not api_key:
        print("❌ 错误: Tavily API Key 未配置")
        print("\n请在 config.yml 中添加:")
        print("tavily:")
        print("  api_key: \"your_api_key_here\"")
        print("\n或设置环境变量:")
        print("export TAVILY_API_KEY=\"your_api_key_here\"")
        return 1
    
    print(f"✅ API Key 已配置: {api_key[:10]}...{api_key[-4:]}")
    print()
    
    # 运行测试
    tests = [
        ("基础搜索", test_basic_search),
        ("深度搜索", test_advanced_search),
        ("JSON 输出", test_json_output),
        ("无 AI 答案", test_no_answer),
        ("错误处理", test_error_handling)
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"❌ 测试失败: {name}")
            print(f"   错误: {e}")
            failed += 1
            print()
    
    # 总结
    print("=" * 60)
    print("📊 测试总结")
    print("=" * 60)
    print(f"✅ 通过: {passed}/{len(tests)}")
    print(f"❌ 失败: {failed}/{len(tests)}")
    
    if failed == 0:
        print("\n🎉 所有测试通过！Web Search 工具运行正常。")
        return 0
    else:
        print("\n⚠️ 部分测试失败，请检查错误信息。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
