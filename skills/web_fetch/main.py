'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-02-09 15:18:53
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""
Web Fetch Skill - 抓取网页内容并使用 Fast LLM 提取关键信息
通过 r.jina.ai 将网页转为 Markdown，再用快速模型提取用户需要的信息。
"""
import argparse
import json
import sys
import os
import urllib.request
import urllib.error
import ssl

# --- Constants ---
JINA_PREFIX = "https://r.jina.ai/"
MAX_MD_LENGTH = 80000
FETCH_TIMEOUT = 30
LLM_TIMEOUT = 60

EXTRACTION_SYSTEM_PROMPT = (
    "You are an information extraction assistant. "
    "The user will provide you with a Markdown-formatted webpage and a query. "
    "Extract and summarize ONLY the information relevant to the query. "
    "Be concise and factual. If the information is not found, say so clearly. "
    "Respond in the same language as the query."
)


def load_config() -> dict:
    """加载项目配置文件"""
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


def fetch_page(url: str) -> dict:
    """通过 r.jina.ai 抓取网页并转为 Markdown"""
    jina_url = f"{JINA_PREFIX}{url}"
    headers = {
        "Accept": "text/markdown",
        "User-Agent": "Mozilla/5.0 (compatible; NORA/1.0)"
    }

    req = urllib.request.Request(jina_url, headers=headers)
    ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
            if resp.status != 200:
                return {"status": "error", "message": f"HTTP {resp.status} from r.jina.ai"}
            content = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return {"status": "error", "message": f"HTTP error {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"status": "error", "message": f"Network error: {e.reason}"}
    except Exception as e:
        return {"status": "error", "message": f"Unexpected error: {e}"}

    if not content or len(content.strip()) < 50:
        return {"status": "error", "message": "Page returned empty or very short content."}

    if len(content) > MAX_MD_LENGTH:
        content = content[:MAX_MD_LENGTH] + "\n\n[... content truncated ...]"

    return {"status": "success", "markdown": content}


def call_llm_gemini(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    """直接调用 Gemini REST API"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096}
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            candidates = result.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
            return "[Gemini returned no content]"
    except Exception as e:
        raise RuntimeError(f"Gemini API error: {e}")


def call_llm_openai(api_key: str, model: str, system_prompt: str, user_prompt: str, base_url: str = None) -> str:
    """直接调用 OpenAI 兼容 REST API"""
    endpoint = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 4096
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(endpoint, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    })

    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return "[OpenAI returned no content]"
    except Exception as e:
        raise RuntimeError(f"OpenAI API error: {e}")


def extract_with_llm(config: dict, markdown: str, query: str) -> str:
    """使用配置中的 fast 模型提取信息"""
    llm_config = config.get("llm", {})
    provider = llm_config.get("provider", "gemini")
    api_keys = llm_config.get("api_keys", {})
    models = llm_config.get("models", {})

    api_key = api_keys.get(provider)
    model_name = models.get("fast")

    if not api_key:
        raise RuntimeError(f"No API key for provider '{provider}'. Check config.yml -> llm.api_keys.{provider}")
    if not model_name:
        raise RuntimeError("No 'fast' model configured. Check config.yml -> llm.models.fast")

    user_prompt = f"## Query\n{query}\n\n## Webpage Content\n{markdown}"

    if provider == "gemini":
        return call_llm_gemini(api_key, model_name, EXTRACTION_SYSTEM_PROMPT, user_prompt)
    elif provider == "openai":
        base_url = llm_config.get("base_url")
        return call_llm_openai(api_key, model_name, EXTRACTION_SYSTEM_PROMPT, user_prompt, base_url)
    else:
        raise RuntimeError(f"Unsupported LLM provider: '{provider}'")


def run(url: str, query: str, raw: bool = False, **kwargs) -> int:
    """主执行逻辑"""
    if not url or not url.strip():
        print(json.dumps({"status": "error", "message": "URL 不能为空"}, ensure_ascii=False))
        return 1

    if not query or not query.strip():
        print(json.dumps({"status": "error", "message": "查询内容不能为空"}, ensure_ascii=False))
        return 1

    # Step 1: 抓取网页
    result = fetch_page(url.strip())
    if result["status"] != "success":
        print(json.dumps(result, ensure_ascii=False))
        return 1

    markdown = result["markdown"]

    # raw 模式直接返回 Markdown
    if raw:
        print(markdown)
        return 0

    # Step 2: 使用 Fast LLM 提取信息
    config = load_config()
    try:
        extracted = extract_with_llm(config, markdown, query.strip())
        print(extracted)
        return 0
    except RuntimeError as e:
        fallback = markdown[:5000]
        print(f"[Extraction failed: {e}]\n\n{fallback}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="抓取网页并提取关键信息",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --url "https://example.com/article" --query "这篇文章的核心观点是什么"
  python main.py --url "https://news.ycombinator.com" --query "今日热门话题"
  python main.py --url "https://example.com" --query "placeholder" --raw
        """
    )

    parser.add_argument("--url", required=True, help="要抓取的网页完整 URL")
    parser.add_argument("--query", required=True, help="要从网页中提取的信息")
    parser.add_argument("--raw", action="store_true", help="直接返回 Markdown 原文，不经过 LLM 提取")

    args = parser.parse_args()
    result = run(**vars(args))
    sys.exit(result)
