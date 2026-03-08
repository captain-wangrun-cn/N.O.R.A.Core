# 图片记忆系统 (Image Memory System)

> 让 N.O.R.A. 能记住用户发送过的每一张图片，并在需要时通过 ID、时间、关键词或语义描述找回它们。

## 概述

当用户通过 Telegram 发送图片时，系统会：

1. **为每张图片分配唯一 ID**（格式: `img_<8位hex>`，如 `img_a1b2c3d4`）
2. **将图片原图发送给 LLM**（多模态输入，二进制 bytes）
3. **在文本提示中告知 LLM 图片 ID**，要求 LLM 返回关键词标签列表（用于检索）
4. **解析 LLM 回复中的标签**，与图片 ID、路径、时间等一起存入 MongoDB + Qdrant
5. **提供 `view_image` 工具**，支持多种检索方式
6. 当 `view_image(return_image=true)` 时，工具会返回图片内容标签，下一轮自动切换 `image` 模型进行图像分析

## 数据流

```
用户发送图片
     │
     ▼
Telegram Adapter (_handle_photo)
     │  下载图片 → workspace/data/telegram/photo_xxx.jpg
     │  构造 [image: path] 文本
     ▼
Controller (handle_new_message → _generate_response)
     │  extract_image_payloads(text)
     │    ├── 读取图片二进制 (bytes, base64)
     │    ├── 生成唯一 image_id (img_xxxxxxxx)
     │    └── 返回 clean_text + images[]
     │
     │  构建 user_prompt:
     │    原文 + "📎 本次消息附带了以下图片: img_xxx ..."
     │    + 要求 LLM 返回 [IMAGE_TAGS:img_xxx]...[/IMAGE_TAGS]
     │
     ▼
LLM (image 模型，带 multimodal_images)
     │  回复正文 + [IMAGE_TAGS:img_xxx] 标签 [/IMAGE_TAGS]
     │
     ▼
Controller (后处理)
     │  1. 从 final_response 提取 IMAGE_TAGS 块
     │  2. 从用户可见文本中移除 IMAGE_TAGS
     │  3. 发送清理后的文本给用户
     │  4. 异步保存 → ImageStore
     │
     ▼
ImageStore.save_image_metadata()
     ├── MongoDB: 结构化元数据 (id, path, tags, user, time)
     └── Qdrant:  标签文本向量 (语义检索)

用户请求找回图片（view_image）
     │  view_image(keyword/image_id/time, return_image=true)
     ▼
Tool 返回元数据 + MediaTag: [image: absolute_path]
     │
     ▼
Controller 检测到 return_image=true
     │  解析工具输出中的 [image: ...] → 多模态图片 payload
     ▼
下一轮强制切换 image_llm + multimodal_images 注入
     │
     ▼
LLM 基于找回图片继续分析并回复
```

## 存储结构

### MongoDB (`nora.images`)

```json
{
    "image_id": "img_a1b2c3d4",
     "file_path": "data/telegram/photo_xxxx.jpg",
    "tags": "猫咪, 橘猫, 沙发, 室内, 可爱, 蜷缩, 温暖, 阳光, 午后",
    "user_id": "123456789",
    "chat_id": "123456789",
    "timestamp": 1709856000.0,
    "datetime": "2024-03-08T00:00:00+00:00"
}
```

**索引:**
- `image_id` (唯一)
- `user_id`
- `timestamp`
- `tags` (文本索引, 支持关键词搜索)

### Qdrant (`nora_images`)

```json
{
    "id": "<uuid>",
    "vector": [0.123, ...],
    "payload": {
        "image_id": "img_a1b2c3d4",
        "text": "猫咪, 橘猫, 沙发, 室内, 可爱...",
          "file_path": "data/telegram/photo_xxxx.jpg",
        "user_id": "123456789",
        "chat_id": "123456789",
        "timestamp": 1709856000.0
    }
}
```

**payload 索引:**
- `image_id` (KEYWORD)
- `user_id` (KEYWORD)
- `timestamp` (FLOAT)

## LLM 标签协议

在用户发送图片时，系统会在 user prompt 末尾追加指令：

```
📎 本次消息附带了以下图片：
- 图片 ID: img_a1b2c3d4  文件: photo_xxxx.jpg

请在回复最末尾，为每张图片附上【关键词标签列表】，格式如下：
[IMAGE_TAGS:img_a1b2c3d4]
标签1, 标签2, 标签3, 标签4 ...（仅关键词，不要长句描述）
[/IMAGE_TAGS]
```

LLM 回复示例：

```
哇，好可爱的猫猫！橘猫蜷在沙发上晒太阳呢～

[IMAGE_TAGS:img_a1b2c3d4]
猫咪, 橘猫, 沙发, 室内, 蜷缩, 午后, 阳光, 温暖, 可爱, 宠物, 毛茸茸, 慵懒
[/IMAGE_TAGS]
```

系统会：
1. 提取 `[IMAGE_TAGS:xxx]...[/IMAGE_TAGS]` 中的标签文本
2. 从发送给用户的文本中移除这些标签块
3. 将标签文本向量化并存入 Qdrant

## `view_image` 工具

注册在 `brain/tools.py` 中的内置工具，LLM 可以在对话中调用它来检索图片。

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `image_id` | string | 否 | 按图片 ID 精确查找 |
| `keyword` | string | 否 | 关键词/语义搜索（优先语义检索） |
| `start_time` | string | 否 | 时间范围起点（Unix 时间戳） |
| `end_time` | string | 否 | 时间范围终点（Unix 时间戳） |
| `user_id` | string | 否 | 按用户过滤 |
| `limit` | int | 否 | 最大结果数（默认 10） |
| `return_image` | bool | 否 | `true` 时返回图片内容标签（`[image: absolute_path]`）供直接发送或二次图像分析 |

### 检索策略

1. **有 `image_id`** → MongoDB 精确查询
2. **有 `keyword`** → Qdrant 语义搜索 → 回退 MongoDB 文本搜索
3. **有时间范围** → MongoDB 时间过滤
4. **无参数** → 返回最近的图片

### `return_image` 模式说明

- `return_image=false`（默认）：返回元数据（ID、路径、tags、时间、相关度）
- `return_image=true`：除元数据外，额外返回 `MediaTag: [image: absolute_path]`
     - 该标签会被后续流程解析为真实多模态输入
     - Controller 会在下一轮自动切换到 `image` 模型继续分析图片

### 使用场景

LLM 在对话中可以这样调用：
- "找一下我之前发的那张猫的照片" → `view_image(keyword="猫")`
- "看看 img_a1b2c3d4 那张图" → `view_image(image_id="img_a1b2c3d4")`
- "上周发的照片有哪些" → `view_image(start_time="...", end_time="...")`

## 涉及的文件

| 文件 | 职责 |
|------|------|
| `memory/image_store.py` | 图片元数据的 MongoDB + Qdrant 存储和检索 |
| `brain/multimodal.py` | 图片标签解析、image_id 生成、二进制读取 |
| `core/controller.py` | 编排：ID 注入 → LLM 调用 → 标签提取 → 存储 |
| `brain/tools.py` | `view_image` 工具注册和执行（含 `return_image`） |
| `brain/templates/image_tags.jinja` | 图片标签提取提示词模板 |
| `tests/test_image_memory.py` | 单元测试 |

## 配置

图片记忆系统复用现有的 MongoDB、Qdrant、Embedding 配置（`config.yml`）：

```yaml
memory:
  qdrant:
    host: "localhost"
    port: 6333
  mongo:
    uri: "mongodb://nora:nora_password@localhost:27017/"
  embedding:
    base_url: "https://api.siliconflow.cn/v1"
    api_key: "YOUR_KEY"
    model: "BAAI/bge-m3"
    dimensions: 1024
```

无需额外配置，系统会自动创建所需的 MongoDB collection 和 Qdrant collection。

## 降级策略

- MongoDB 或 Qdrant 不可用 → `ImageStore.enabled = False`，图片标签仍会作为普通对话记忆存入 RAG
- Embedding 服务不可用 → 仅保存 MongoDB 元数据，语义检索不可用
- LLM 未按格式返回标签 → 使用文件名作为 fallback 标签
- `view_image(return_image=true)` 但图片文件已丢失/路径失效 → 跳过多模态注入，仅返回检索元数据
