---
name: web_search
description: 使用 Tavily API 进行智能网络搜索，返回高质量的搜索结果和内容摘要
version: 1.0.1
author: N.O.R.A. Core
parameters:
  type: object
  properties:
    query:
      type: string
      description: 搜索查询词
    max_results:
      type: integer
      description: 最大结果数量 (1-10)
      default: 5
    search_depth:
      type: string
      description: 搜索深度 basic 或 advanced
      default: basic
    include_answer:
      type: boolean
      description: 是否包含 AI 生成的答案摘要
      default: true
    include_raw_content:
      type: boolean
      description: 是否包含原始网页内容
      default: false
  required: 
      - query
---

# Web Search Skill

## 何时使用

需要进行网络搜索、获取实时信息或答案摘要时。

## 如何调用（必须使用 `execute_skill`）

- `execute_skill("web_search", '{"query": "最新的 AI 技术进展", "max_results": 5, "search_depth": "basic"}')`
- 禁止使用 `exec_command` 或直接运行 `python main.py`。

## 参数说明

| 参数                | 类型    | 必填 | 默认值 | 说明                        |
| ------------------- | ------- | ---- | ------ | --------------------------- |
| query               | string  | 是   | -      | 搜索查询词                  |
| max_results         | integer | 否   | 5      | 最大结果数量 (1-10)         |
| search_depth        | string  | 否   | basic  | 搜索深度 basic 或 advanced  |
| include_answer      | boolean | 否   | true   | 是否包含 AI 生成的答案摘要  |
| include_raw_content | boolean | 否   | false  | 是否包含原始网页内容        |

## 使用示例

- 基础搜索：
  - `execute_skill("web_search", '{"query": "最新的 AI 技术进展"}')`
- 深度搜索：
  - `execute_skill("web_search", '{"query": "量子计算最新突破", "search_depth": "advanced", "max_results": 8}')`
- 仅获取答案：
  - `execute_skill("web_search", '{"query": "什么是大语言模型", "include_answer": true, "max_results": 3}')`

## 返回格式（示例）

```json
{
  "status": "success",
  "query": "搜索查询词",
  "answer": "AI 生成的答案摘要（如果启用）",
  "results": [
    { "title": "结果标题", "url": "https://example.com", "content": "内容摘要", "score": 0.95 }
  ],
  "images": ["图片URL列表"],
  "response_time": 1.23
}
```

## 配置要求

在 `config.yml` 中添加：

```yaml
tavily:
  api_key: "your_tavily_api_key_here"
  timeout: 10
```

## 注意事项

- API Key 必填；免费版有配额限制
- `advanced` 搜索深度更慢且更耗配额
- 搜索结果可能受地区/语言影响

## 技术实现

- Tavily Search API v1；可选 `tavily-python` 或直接 HTTP 请求
- 支持错误重试；可扩展结果缓存

## 更新日志

- v1.0.0 (2026-02-09) 初始版本：基础搜索、AI 答案、多参数配置
