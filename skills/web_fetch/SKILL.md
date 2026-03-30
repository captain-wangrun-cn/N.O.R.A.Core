---
name: web_fetch
description: 抓取指定网页的内容并使用 AI 提取关键信息。当需要阅读和理解某个网页的内容时使用此技能。
version: 1.0.0
author: N.O.R.A. Core
parameters:
  type: object
  properties:
    url:
      type: string
      description: 要抓取的网页完整 URL
    query:
      type: string
      description: 需要从网页中提取的信息描述
    raw:
      type: boolean
      description: 是否直接返回 Markdown 原文
  required:
    - url
    - query
---

# Web Fetch Skill

## 何时使用

需要阅读网页并提取关键信息、摘要或直接获取 Markdown 原文时。

## 如何调用（必须使用 `execute_skill`）

- `execute_skill("web_fetch", '{"url": "https://example.com", "query": "核心观点", "raw": false}')`
- 禁止使用 `exec_command` 或直接运行 `python main.py`。

## 参数说明

| 参数  | 类型    | 必填 | 默认值 | 说明                                    |
| ----- | ------- | ---- | ------ | --------------------------------------- |
| url   | string  | 是   | -      | 要抓取的网页完整 URL                    |
| query | string  | 是   | -      | 要从网页中提取的信息描述                |
| raw   | boolean | 否   | false  | 直接返回 Markdown 原文，不经过 LLM 提取 |

## 使用示例

- 提取文章信息：
	- `execute_skill("web_fetch", '{"url": "https://example.com/article", "query": "核心观点"}')`
- 获取页面摘要：
	- `execute_skill("web_fetch", '{"url": "https://news.ycombinator.com", "query": "今日热门话题"}')`
- 获取原始 Markdown：
	- `execute_skill("web_fetch", '{"url": "https://example.com", "query": "placeholder", "raw": true}')`

## 工作流程

1. 将 URL 发送到 `r.jina.ai` 转换为 Markdown
2. 截断超长内容（上限 80,000 字符）
3. 结合查询交给 fast 模型提取要点
4. 返回精炼回答；若 LLM 失败，回退输出 Markdown 前 5,000 字符

## 依赖与配置

- Python 标准库（无需额外安装）
- `config.yml` 中的 LLM 配置（provider、api_key、fast 模型）
