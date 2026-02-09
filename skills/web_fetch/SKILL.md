---
name: web_fetch
description: 抓取指定网页的内容并使用 AI 提取关键信息。当需要阅读和理解某个网页的内容时使用此技能。
version: 1.0.0
author: N.O.R.A. Core
---

# Web Fetch Skill

## 功能说明

通过 r.jina.ai 将网页转换为 Markdown 格式，再使用 fast 模型根据查询从中提取用户需要的信息。

## 参数说明

| 参数    | 类型   | 必填 | 默认值 | 说明                                    |
| ------- | ------ | ---- | ------ | --------------------------------------- |
| `url`   | string | 是   | -      | 要抓取的网页完整 URL                    |
| `query` | string | 是   | -      | 要从网页中提取的信息描述                |
| `raw`   | flag   | 否   | false  | 直接返回 Markdown 原文，不经过 LLM 提取 |

## 使用示例

### 提取文章信息

```bash
python main.py --url "https://example.com/article" --query "这篇文章的核心观点是什么"
```

### 获取页面摘要

```bash
python main.py --url "https://news.ycombinator.com" --query "今日热门话题有哪些"
```

### 获取原始 Markdown

```bash
python main.py --url "https://example.com" --query "placeholder" --raw
```

## 工作流程

1. 将 URL 发送到 `r.jina.ai` 获取 Markdown 格式内容
2. 截断过长内容（上限 80,000 字符）
3. 将 Markdown 和用户查询发送给 fast 模型提取关键信息
4. 返回 LLM 提取的精炼回答
5. 若 LLM 调用失败，回退返回原始 Markdown 前 5,000 字符

## 依赖

- Python 标准库（无需额外安装）
- `config.yml` 中的 LLM 配置（provider、api_key、fast 模型）
