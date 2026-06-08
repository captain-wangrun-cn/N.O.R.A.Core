# Telegram Adapter 配置说明

Telegram adapter 通过 Telegram Bot API 收发消息。私有配置写在本目录的 `config.json` 中，不写入全局 `config.yml`。

## 快速配置

1. 如果没有 `config.json`，复制本目录的 `config.example.json` 为 `config.json`。
2. 在 `config.json` 中填写 BotFather 提供的机器人 token。
3. 启动：

```bash
python main.py --adapter telegram
```

无 TUI 模式：

```bash
python main.py --no-tui --adapter telegram
```

## config.json

```json
{
  "bot_token": "YOUR_TELEGRAM_BOT_TOKEN"
}
```

## 参数说明

| 参数 | 类型 | 默认值 | 必填 | 说明 |
|---|---|---|---|---|
| `bot_token` | string | `YOUR_TELEGRAM_BOT_TOKEN` | 是 | Telegram 机器人 token，由 BotFather 创建机器人后提供。启动时如果为空或仍是占位值，adapter 会报错并提示编辑本文件。 |

## 注意事项

- `config.json` 会被 `.gitignore` 忽略，避免误提交真实 token。
- 只提交 `config.example.json`，不要把真实 token 写入文档、测试或全局 `config.yml`。
- Telegram 群聊默认只有 @机器人时才进入处理链路；私聊默认处理。
- Telegram 群管工具是否成功取决于机器人在群里的管理员权限。
