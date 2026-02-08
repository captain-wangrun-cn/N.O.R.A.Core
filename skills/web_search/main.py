'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 01:03:12
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
Web Search Skill - 使用 Tavily API 进行智能网络搜索
"""
import argparse
import json
import sys
import time
import os
from typing import Dict, Any, List, Optional

# 尝试导入 tavily 库
try:
    from tavily import TavilyClient
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False
    # 回退到直接 HTTP 请求
    import urllib.request
    import urllib.parse


def load_config() -> Dict[str, Any]:
    """加载配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yml')
    
    if not os.path.exists(config_path):
        return {}
    
    try:
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Warning: Failed to load config: {e}", file=sys.stderr)
        return {}


def search_with_tavily_sdk(
    api_key: str,
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
    include_raw_content: bool = False,
    timeout: int = 10
) -> Dict[str, Any]:
    """使用 Tavily SDK 执行搜索"""
    try:
        client = TavilyClient(api_key=api_key)
        
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=include_answer,
            include_raw_content=include_raw_content
        )
        
        return {
            "status": "success",
            "query": query,
            "answer": response.get("answer", ""),
            "results": response.get("results", []),
            "images": response.get("images", []),
            "response_time": response.get("response_time", 0)
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Tavily SDK error: {str(e)}"
        }


def search_with_http(
    api_key: str,
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
    include_raw_content: bool = False,
    timeout: int = 10
) -> Dict[str, Any]:
    """使用 HTTP 请求执行搜索（备用方案）"""
    url = "https://api.tavily.com/search"
    
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        "include_raw_content": include_raw_content
    }
    
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            
            return {
                "status": "success",
                "query": query,
                "answer": result.get("answer", ""),
                "results": result.get("results", []),
                "images": result.get("images", []),
                "response_time": result.get("response_time", 0)
            }
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        try:
            error_data = json.loads(error_body)
            error_msg = error_data.get("error", str(e))
        except:
            error_msg = f"HTTP {e.code}: {error_body}"
        
        return {
            "status": "error",
            "message": f"API request failed: {error_msg}"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Request error: {str(e)}"
        }


def format_results(data: Dict[str, Any], include_raw: bool = False) -> str:
    """格式化搜索结果为易读文本"""
    if data.get("status") != "success":
        return f"❌ 搜索失败: {data.get('message', 'Unknown error')}"
    
    output = []
    output.append(f"🔍 搜索查询: {data['query']}\n")
    
    # AI 答案
    if data.get("answer"):
        output.append("📝 AI 答案摘要:")
        output.append(data["answer"])
        output.append("")
    
    # 搜索结果
    results = data.get("results", [])
    if results:
        output.append(f"📋 搜索结果 ({len(results)} 条):\n")
        
        for i, result in enumerate(results, 1):
            output.append(f"{i}. 【{result.get('title', 'No Title')}】")
            output.append(f"   🔗 {result.get('url', 'No URL')}")
            
            score = result.get('score', 0)
            if score:
                output.append(f"   ⭐ 相关度: {score:.2f}")
            
            content = result.get('content', '')
            if content:
                # 截取摘要，避免过长
                summary = content[:200] + "..." if len(content) > 200 else content
                output.append(f"   📄 {summary}")
            
            if include_raw and result.get('raw_content'):
                output.append(f"   📜 原始内容: {result['raw_content'][:500]}...")
            
            output.append("")
    
    # 图片结果
    images = data.get("images", [])
    if images:
        output.append(f"🖼️ 相关图片 ({len(images)} 张):")
        for img_url in images[:5]:  # 最多显示 5 张
            output.append(f"   - {img_url}")
        output.append("")
    
    # 响应时间
    resp_time = data.get("response_time", 0)
    if resp_time:
        output.append(f"⏱️ 响应时间: {resp_time:.2f}s")
    
    return "\n".join(output)


def run(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
    include_raw_content: bool = False,
    output_format: str = "text",
    **kwargs
) -> int:
    """主执行逻辑"""
    
    # 验证参数
    if not query or not query.strip():
        print(json.dumps({
            "status": "error",
            "message": "查询词不能为空"
        }))
        return 1
    
    if max_results < 1 or max_results > 10:
        print(json.dumps({
            "status": "error",
            "message": "max_results 必须在 1-10 之间"
        }))
        return 1
    
    if search_depth not in ["basic", "advanced"]:
        print(json.dumps({
            "status": "error",
            "message": "search_depth 必须是 'basic' 或 'advanced'"
        }))
        return 1
    
    # 加载配置
    config = load_config()
    tavily_config = config.get("tavily", {})
    api_key = tavily_config.get("api_key") or os.environ.get("TAVILY_API_KEY")
    timeout = tavily_config.get("timeout", 10)
    
    if not api_key:
        print(json.dumps({
            "status": "error",
            "message": "Tavily API Key 未配置。请在 config.yml 中设置 tavily.api_key 或设置环境变量 TAVILY_API_KEY"
        }))
        return 1
    
    # 执行搜索
    start_time = time.time()
    
    if TAVILY_AVAILABLE:
        result = search_with_tavily_sdk(
            api_key=api_key,
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=include_answer,
            include_raw_content=include_raw_content,
            timeout=timeout
        )
    else:
        result = search_with_http(
            api_key=api_key,
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=include_answer,
            include_raw_content=include_raw_content,
            timeout=timeout
        )
    
    # 添加本地计时
    if not result.get("response_time"):
        result["response_time"] = time.time() - start_time
    
    # 输出结果
    if output_format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_results(result, include_raw_content))
    
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="使用 Tavily API 进行智能网络搜索",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --query "最新的 AI 技术进展"
  python main.py --query "量子计算" --search_depth advanced --max_results 8
  python main.py --query "大语言模型" --include_answer --output_format json
        """
    )
    
    parser.add_argument(
        "--query",
        required=True,
        help="搜索查询词"
    )
    parser.add_argument(
        "--max_results",
        type=int,
        default=5,
        help="最大结果数量 (1-10)，默认: 5"
    )
    parser.add_argument(
        "--search_depth",
        choices=["basic", "advanced"],
        default="basic",
        help="搜索深度: basic (快速) 或 advanced (深度)，默认: basic"
    )
    parser.add_argument(
        "--include_answer",
        action="store_true",
        default=True,
        help="包含 AI 生成的答案摘要，默认: True"
    )
    parser.add_argument(
        "--no_answer",
        action="store_true",
        help="不包含 AI 答案摘要"
    )
    parser.add_argument(
        "--include_raw_content",
        action="store_true",
        help="包含原始网页内容（会增加响应大小）"
    )
    parser.add_argument(
        "--output_format",
        choices=["text", "json"],
        default="text",
        help="输出格式: text (易读) 或 json (结构化)，默认: text"
    )
    
    args = parser.parse_args()
    
    # 处理 no_answer 标志
    if args.no_answer:
        args.include_answer = False
    
    result = run(**vars(args))
    sys.exit(result)
