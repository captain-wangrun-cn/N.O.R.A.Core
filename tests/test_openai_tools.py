'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-03-06 22:34:32
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
测试 OpenAI provider 的 tool schema 转换和 history 转换。
不需要实际 API key，仅测试纯函数逻辑。
"""
import json
import sys
import os

# 添加项目根目录到 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 不需要 config 初始化，我们直接测试静态/实例方法的逻辑


def test_convert_tools_schema():
    """测试 Gemini 格式 → OpenAI 格式的 schema 转换。"""
    from brain.providers.openai import OpenAIProvider
    
    # Gemini 格式输入（_generate_schema 的输出）
    gemini_schemas = [
        {
            "name": "edit_file",
            "description": "Edit a file by replacing old_code with new_code.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING"},
                    "old_code": {"type": "STRING"},
                    "new_code": {"type": "STRING"}
                },
                "required": ["path", "old_code", "new_code"]
            }
        },
        {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING"},
                    "max_results": {"type": "INTEGER"}
                },
                "required": ["query"]
            }
        }
    ]
    
    result = OpenAIProvider._convert_tools_schema(gemini_schemas)
    
    # 验证外层包装
    assert len(result) == 2
    for item in result:
        assert item["type"] == "function"
        assert "function" in item
        assert "name" in item["function"]
        assert "description" in item["function"]
        assert "parameters" in item["function"]
    
    # 验证类型转换
    edit_params = result[0]["function"]["parameters"]
    assert edit_params["type"] == "object"  # OBJECT → object
    assert edit_params["properties"]["path"]["type"] == "string"  # STRING → string
    assert edit_params["required"] == ["path", "old_code", "new_code"]
    
    search_params = result[1]["function"]["parameters"]
    assert search_params["properties"]["max_results"]["type"] == "integer"  # INTEGER → integer
    
    print("✅ test_convert_tools_schema passed")


def test_convert_tools_schema_passthrough():
    """测试已经是 OpenAI 格式的 schema 直接透传。"""
    from brain.providers.openai import OpenAIProvider
    
    openai_schema = [{
        "type": "function",
        "function": {
            "name": "test_tool",
            "description": "A test tool",
            "parameters": {"type": "object", "properties": {}}
        }
    }]
    
    result = OpenAIProvider._convert_tools_schema(openai_schema)
    assert result == openai_schema
    print("✅ test_convert_tools_schema_passthrough passed")


def test_convert_tools_schema_empty():
    """测试空 tools 列表。"""
    from brain.providers.openai import OpenAIProvider
    
    result = OpenAIProvider._convert_tools_schema([])
    assert result == []
    print("✅ test_convert_tools_schema_empty passed")


def test_convert_history_basic():
    """测试基本的 history 转换（user/assistant 角色）。"""
    from brain.providers.openai import OpenAIProvider
    
    # 创建一个 mock provider 对象来调用实例方法
    # 我们直接调用 unbound method
    provider = type('MockProvider', (), {'_convert_history': OpenAIProvider._convert_history})()
    
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好主人"},
        {"role": "user", "content": "帮我搜索"},
    ]
    
    result = provider._convert_history(history)
    assert len(result) == 3
    assert result[0] == {"role": "user", "content": "你好"}
    assert result[1] == {"role": "assistant", "content": "你好主人"}
    assert result[2] == {"role": "user", "content": "帮我搜索"}
    print("✅ test_convert_history_basic passed")


def test_convert_history_tool_call_response_pair():
    """测试 tool_call + tool_response 配对转换为 OpenAI 原生格式。"""
    from brain.providers.openai import OpenAIProvider
    
    provider = type('MockProvider', (), {'_convert_history': OpenAIProvider._convert_history})()
    
    history = [
        {"role": "user", "content": "帮我搜索 Python 教程"},
        {"role": "tool_call", "tool_name": "web_search", "tool_args": {"query": "Python 教程"}},
        {"role": "tool_response", "tool_name": "web_search", "content": "找到了 10 个结果..."},
        {"role": "assistant", "content": "我帮你搜索到了 Python 教程..."},
    ]
    
    result = provider._convert_history(history)
    
    # 应该有 4 条消息: user, assistant(tool_calls), tool, assistant
    assert len(result) == 4
    
    # 第一条: user
    assert result[0]["role"] == "user"
    
    # 第二条: assistant with tool_calls
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] is None
    assert "tool_calls" in result[1]
    tc = result[1]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "web_search"
    assert json.loads(tc["function"]["arguments"]) == {"query": "Python 教程"}
    assert tc["id"] == "call_1"
    
    # 第三条: tool response with matching id
    assert result[2]["role"] == "tool"
    assert result[2]["tool_call_id"] == "call_1"
    assert "找到了 10 个结果" in result[2]["content"]
    
    # 第四条: assistant
    assert result[3]["role"] == "assistant"
    
    print("✅ test_convert_history_tool_call_response_pair passed")


def test_convert_history_multiple_tool_calls():
    """测试多轮工具调用的 ID 递增。"""
    from brain.providers.openai import OpenAIProvider
    
    provider = type('MockProvider', (), {'_convert_history': OpenAIProvider._convert_history})()
    
    history = [
        {"role": "tool_call", "tool_name": "read_file", "tool_args": {"path": "a.py"}},
        {"role": "tool_response", "tool_name": "read_file", "content": "file content A"},
        {"role": "tool_call", "tool_name": "edit_file", "tool_args": {"path": "a.py", "old_code": "x", "new_code": "y"}},
        {"role": "tool_response", "tool_name": "edit_file", "content": "OK"},
    ]
    
    result = provider._convert_history(history)
    
    # 4 条消息: assistant+tool, assistant+tool
    assert len(result) == 4
    
    # 第一组 tool_call_id = call_1
    assert result[0]["tool_calls"][0]["id"] == "call_1"
    assert result[1]["tool_call_id"] == "call_1"
    
    # 第二组 tool_call_id = call_2
    assert result[2]["tool_calls"][0]["id"] == "call_2"
    assert result[3]["tool_call_id"] == "call_2"
    
    print("✅ test_convert_history_multiple_tool_calls passed")


def test_convert_history_orphan_tool_response():
    """测试孤立的 tool_response（没有配对的 tool_call）回退为 user。"""
    from brain.providers.openai import OpenAIProvider
    
    provider = type('MockProvider', (), {'_convert_history': OpenAIProvider._convert_history})()
    
    history = [
        {"role": "tool_response", "tool_name": "web_search", "content": "search results"},
    ]
    
    result = provider._convert_history(history)
    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert "web_search" in result[0]["content"]
    assert "search results" in result[0]["content"]
    
    print("✅ test_convert_history_orphan_tool_response passed")


def test_convert_history_unknown_role():
    """测试未知 role 回退为 user。"""
    from brain.providers.openai import OpenAIProvider
    
    provider = type('MockProvider', (), {'_convert_history': OpenAIProvider._convert_history})()
    
    history = [
        {"role": "some_unknown_role", "content": "hello"},
    ]
    
    result = provider._convert_history(history)
    assert len(result) == 1
    assert result[0]["role"] == "user"
    
    print("✅ test_convert_history_unknown_role passed")


if __name__ == "__main__":
    test_convert_tools_schema()
    test_convert_tools_schema_passthrough()
    test_convert_tools_schema_empty()
    test_convert_history_basic()
    test_convert_history_tool_call_response_pair()
    test_convert_history_multiple_tool_calls()
    test_convert_history_orphan_tool_response()
    test_convert_history_unknown_role()
    print("\n🎉 All OpenAI provider tests passed!")
