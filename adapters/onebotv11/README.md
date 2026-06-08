# OneBot v11 Adapter 配置说明

OneBot v11 adapter 用于连接 QQ/OneBot 实现，例如 NapCat、Lagrange、go-cqhttp 等。私有配置写在本目录的 `config.json` 中，不写入全局 `config.yml`。

## 快速配置

1. 如果没有 `config.json`，复制本目录的 `config.example.json` 为 `config.json`。
2. 按你的 OneBot 实现选择正向 WebSocket 或反向 WebSocket。
3. 启动：

```bash
python main.py --no-tui --adapter onebotv11
```

也可以使用默认自动选择：

```bash
python main.py --no-tui --adapter auto
```

`auto` 会跳过未配置或仍是占位值的其他平台 adapter。

## config.json

```json
{
  "connection_type": "websocket",
  "websocket_url": "ws://127.0.0.1:6700/",
  "reverse_host": "127.0.0.1",
  "reverse_port": 6701,
  "reverse_path": "/onebot/v11",
  "access_token": "",
  "self_id": "",
  "nickname": "Nora",
  "group_message_policy": "at_or_reply",
  "enable_napcat_api": false,
  "download_media": true,
  "reconnect_interval_seconds": 5
}
```

## 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `connection_type` | string | `websocket` | 连接方式。`websocket` 表示 NORA 主动连接 OneBot 正向 WebSocket；`reverse` 表示 NORA 启动反向 WebSocket 服务，等待 OneBot/NapCat 连接。也兼容 `reverse_websocket`、`ws_reverse`。 |
| `websocket_url` | string | `ws://127.0.0.1:6700/` | 正向 WebSocket 地址，仅 `connection_type="websocket"` 时使用。常见路径包括 `/`、`/api`、`/event`，建议配置到 OneBot 的 Universal WebSocket 地址。 |
| `reverse_host` | string | `127.0.0.1` | 反向 WebSocket 监听地址，仅 `connection_type="reverse"` 时使用。本机使用通常保持默认；需要让容器或局域网访问时改为 `0.0.0.0`。 |
| `reverse_port` | integer | `6701` | 反向 WebSocket 监听端口，仅 `connection_type="reverse"` 时使用。需要和 OneBot 实现里的反向 WebSocket URL 端口一致。 |
| `reverse_path` | string | `/onebot/v11` | 反向 WebSocket 路径，仅 `connection_type="reverse"` 时使用。OneBot 实现中的反向 URL 应类似 `ws://127.0.0.1:6701/onebot/v11`。 |
| `access_token` | string | 空字符串 | OneBot 鉴权 token。若 OneBot 端配置了 access token，这里必须填写相同值；正向连接会用 `Authorization: Bearer ...`，反向连接会校验传入请求头。未启用鉴权可留空。 |
| `self_id` | string | 空字符串 | 机器人自己的 QQ 号。可留空，adapter 会尽量通过 `get_login_info` 获取；如果群聊 @ 触发不稳定，可以手动填写。 |
| `nickname` | string | `Nora` | 机器人昵称备注。主要用于日志和后续扩展，当前不作为强触发条件。 |
| `group_message_policy` | string | `at_or_reply` | 群聊触发策略。`at_or_reply` 表示 @机器人或回复机器人消息时处理；`at`/`mention` 表示只处理 @；`all`/`always` 表示处理所有群消息；`none`/`never` 表示不处理群消息。私聊始终处理。 |
| `enable_napcat_api` | boolean | `false` | 是否向后脑暴露 NapCat 扩展工具。关闭时只暴露标准 OneBot v11 工具；开启后会额外注册 `onebotv11_napcat_*` 工具。 |
| `download_media` | boolean | `true` | 收到图片、视频、语音等媒体段时，是否尝试从消息段 URL 下载到 `workspace/data/onebotv11/`，并转成 NORA 的 `[image: ...]` / `[video: ...]` / `[file: ...]` 标记。 |
| `reconnect_interval_seconds` | integer | `5` | 正向 WebSocket 断线后的重连间隔，单位秒。 |

## 正向 WebSocket 示例

适合 OneBot 实现已经开启 WebSocket 服务的情况。NORA 会主动连过去：

```json
{
  "connection_type": "websocket",
  "websocket_url": "ws://127.0.0.1:6700/",
  "access_token": ""
}
```

## 反向 WebSocket 示例

适合 NapCat/OneBot 配置为主动连接 NORA 的情况。NORA 会监听本地端口：

```json
{
  "connection_type": "reverse",
  "reverse_host": "127.0.0.1",
  "reverse_port": 6701,
  "reverse_path": "/onebot/v11",
  "access_token": ""
}
```

OneBot/NapCat 端的反向 WebSocket URL 对应填写：

```text
ws://127.0.0.1:6701/onebot/v11
```

## NapCat 扩展 API

`enable_napcat_api=false` 时，后脑只会看到标准 OneBot v11 工具。

`enable_napcat_api=true` 时，会额外暴露 NapCat 特有工具，例如戳一戳、标记已读、好友历史、自定义表情、AI 语音、合并转发等。NapCat API 属于实现扩展，不是 OneBot v11 标准；如果调用失败，通常是当前 NapCat 版本、权限或参数不支持。

## 注意事项

- `config.json` 会被 `.gitignore` 忽略，避免误提交 access token、私有地址等信息。
- 群管、撤回、全员禁言、改群名片、退群等都是真实 QQ 操作，NORA 的 prompt 要求执行前先向主人确认。
- OneBot 不同实现的消息段字段可能略有差异，媒体下载依赖消息段里提供可访问的 `url`。
