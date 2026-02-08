---
name: web_search
description: 使用 Tavily API 进行智能网络搜索，返回高质量的搜索结果和内容摘要
version: 1.0.0
author: N.O.R.A. Core
---

# Web Search Skill

## 功能说明

通过 Tavily API 执行智能网络搜索，获取实时、准确的信息。

## 核心特性

- 🔍 **智能搜索**: 使用 Tavily 的 AI 优化搜索算法
- 📊 **结果摘要**: 自动提取和总结搜索结果
- 🌐 **多语言支持**: 支持中英文搜索
- 🎯 **精准定位**: 可指定搜索深度和结果数量
- ⚡ **快速响应**: 优化的 API 调用，快速返回结果

## 参数说明

| 参数                  | 类型    | 必填 | 默认值 | 说明                        |
| --------------------- | ------- | ---- | ------ | --------------------------- |
| `query`               | string  | 是   | -      | 搜索查询词                  |
| `max_results`         | integer | 否   | 5      | 最大结果数量 (1-10)         |
| `search_depth`        | string  | 否   | basic  | 搜索深度: basic 或 advanced |
| `include_answer`      | boolean | 否   | true   | 是否包含 AI 生成的答案摘要  |
| `include_raw_content` | boolean | 否   | false  | 是否包含原始网页内容        |

## 使用示例

### 基础搜索

```bash
python main.py --query "最新的 AI 技术进展"
```

### 深度搜索

```bash
python main.py --query "量子计算最新突破" --search_depth advanced --max_results 8
```

### 仅获取答案

```bash
python main.py --query "什么是大语言模型" --include_answer true --max_results 3
```

## 返回格式

```json
{
  "status": "success",
  "query": "搜索查询词",
  "answer": "AI 生成的答案摘要（如果启用）",
  "results": [
    {
      "title": "结果标题",
      "url": "https://example.com",
      "content": "内容摘要",
      "score": 0.95
    }
  ],
  "images": ["图片URL列表"],
  "response_time": 1.23
}
```

## 错误处理

- ❌ **API Key 未配置**: 请在 `config.yml` 中配置 `tavily.api_key`
- ❌ **查询为空**: 必须提供有效的搜索查询
- ❌ **API 限流**: 超出 API 调用限制，请稍后重试
- ❌ **网络错误**: 检查网络连接和 API 可用性

## 配置要求

在 `config.yml` 中添加：

```yaml
tavily:
  api_key: "your_tavily_api_key_here"
  timeout: 10 # 可选，请求超时时间（秒）
```

## 获取 API Key

1. 访问 [Tavily](https://tavily.com/)
2. 注册账号并创建 API Key
3. 将 Key 添加到配置文件

## 注意事项

- 🔑 API Key 是必需的，请妥善保管
- 📊 免费版有调用次数限制
- 🌍 搜索结果受地区和语言影响
- ⏱️ `advanced` 搜索深度会消耗更多时间和配额

## 技术实现

- **API**: Tavily Search API v1
- **依赖**: `tavily-python` 或直接 HTTP 请求
- **错误重试**: 支持自动重试机制
- **结果缓存**: 可选的本地缓存（未来功能）

## 更新日志

### v1.0.0 (2026-02-09)

- ✅ 初始版本
- ✅ 基础搜索功能
- ✅ AI 答案生成
- ✅ 多参数配置
