"""RTC 子系统配置读写（rtc/config.json）。

遵循 adapter 私有配置约定（见 docs/onboarding/CONVENTIONS.md §3）：
不进全局 config.yml，放 rtc/config.json，提供 config.example.json。
api_key 留空时回落全局 llm provider 里 type=gemini 的 key。
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

RTC_DIR = Path(__file__).resolve().parent
CONFIG_PATH = RTC_DIR / "config.json"
EXAMPLE_PATH = RTC_DIR / "config.example.json"

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "listen": {"host": "127.0.0.1", "port": 8085},
    "public_url": "",
    "api_key": "",
    "model": "gemini-3.1-flash-live-preview",
    "voice_name": "Kore",
    # 语音来源："live"（Live 模型内置音色，低延迟）| "tts"（Nora 台词走 TTS
    # provider 合成，可用 fish_audio 自定义音色，但延迟 +2~4s）
    "voice_source": "live",
    # 最大通话时长（秒），语音/视频通话共用这一个上限（30 分钟）
    "max_call_duration_seconds": 1800,
    # 首次启动为空时自动随机生成并写回 config.json（example 里永远留空，
    # git 仓库不携带任何真实 token）
    "static_token": "",
    "invitation_ttl_seconds": 300,
    "tools_whitelist": [],
    # 本机连不上 Google 时填（如 "http://127.0.0.1:7890"）；服务器直连留空
    "proxy": "",
}


def _generate_static_token() -> str:
    """生成随机静态 token（桌面浏览器调试入口用）。"""
    return f"rtc-{secrets.token_hex(16)}"


def ensure_static_token(cfg: Dict[str, Any]) -> None:
    """static_token 为空时随机生成并写回 config.json（首次启动调用一次）。

    真实 token 只存在于服务器本地的 config.json（gitignore），仓库和
    config.example.json 里永远是空串。写盘基于磁盘上已有的用户配置原样
    补一个键（cfg 可能是调用方注入的测试 dict，不能整份写下去）。
    """
    if str(cfg.get("static_token") or "").strip():
        return
    token = _generate_static_token()
    cfg["static_token"] = token
    try:
        on_disk: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    on_disk = loaded
            except (OSError, json.JSONDecodeError):
                pass  # 读不了就重建一份
        on_disk["static_token"] = token
        save_config(on_disk)
        logger.info("RTC static_token 首次启动已随机生成并写入 config.json")
    except OSError as e:
        # 写盘失败也不能让通话入口起不来——token 留在内存里本进程可用
        logger.warning("RTC static_token 写回 config.json 失败（仅本进程生效）: %s", e)


def get_max_call_seconds(rtc_cfg: Optional[Dict[str, Any]] = None) -> int:
    """最大通话时长（秒），语音/视频通话共用。旧字段 max_call_seconds 兼容读取。"""
    cfg = rtc_cfg if rtc_cfg is not None else load_config()
    try:
        return int(cfg.get("max_call_duration_seconds")
                   or cfg.get("max_call_seconds") or 1800)
    except (TypeError, ValueError):
        return 1800


def load_config() -> Dict[str, Any]:
    """读取 rtc/config.json，缺省项回落 DEFAULTS。文件不存在时返回默认值。"""
    cfg: Dict[str, Any] = dict(DEFAULTS)
    user_cfg: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                user_cfg = loaded
        except (OSError, json.JSONDecodeError) as e:
            logger.error("rtc/config.json 读取失败，使用默认配置: %s", e)
    cfg.update(user_cfg)
    # config.json 里存的是旧字段名（历史部署）时迁移到新字段，避免两处上限。
    # 注意判 user_cfg 而不是合并后的 cfg——DEFAULTS 本身就带新字段，判后者永不触发
    if "max_call_seconds" in user_cfg and "max_call_duration_seconds" not in user_cfg:
        cfg["max_call_duration_seconds"] = cfg["max_call_seconds"]
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_api_key(rtc_cfg: Optional[Dict[str, Any]] = None) -> str:
    """API key：rtc/config.json 优先，回落全局 gemini provider。

    回落顺序：providers.*.type == "gemini" 的第一个 key → 旧结构 llm.api_keys.gemini。
    """
    cfg = rtc_cfg if rtc_cfg is not None else load_config()
    key = str(cfg.get("api_key") or "").strip()
    if key:
        return key

    # 回落全局配置（延迟 import 避免测试环境拉起整个 config 单例的副作用）
    try:
        import config as global_config

        providers = (
            global_config.get_config().get("llm", {}).get("providers", {}) or {}
        )
        for name, provider_cfg in providers.items():
            if (provider_cfg or {}).get("type") == "gemini":
                key = str((provider_cfg or {}).get("api_key") or "").strip()
                if key and not key.startswith("YOUR_"):
                    return key
        key = str(
            global_config.get_config()
            .get("llm", {})
            .get("api_keys", {})
            .get("gemini", "")
        ).strip()
        if key and not key.startswith("YOUR_"):
            return key
    except Exception as e:  # noqa: BLE001 - 回落链路任何失败都不该阻断
        logger.warning("rtc api_key 回落全局配置失败: %s", e)
    return ""


def ensure_example() -> None:
    """仓库里维护 config.example.json（真实 config.json 被 gitignore）。"""
    example = {
        k: v for k, v in DEFAULTS.items() if k not in ("api_key", "static_token")
    }
    example["api_key"] = ""  # 留空回落全局 gemini key
    # 留空 = 首次启动随机生成并写回 config.json（真实 token 不进仓库）
    example["static_token"] = ""
    example["listen"] = {"host": "127.0.0.1", "port": 8085}
    example["tools_whitelist"] = ["search", "set_alarm", "list_alarms", "cancel_alarm"]
    with open(EXAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(example, f, ensure_ascii=False, indent=2)
    # 同步生成 .gitignore 检查（rtc/config.json 不进仓库）
    gitignore = RTC_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("config.json\n", encoding="utf-8")


if __name__ == "__main__":
    ensure_example()
    print(f"example written to {EXAMPLE_PATH}")
