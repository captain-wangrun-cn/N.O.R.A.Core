'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-27 00:00:00
Copyright © WR（captain-wangrun-cn）All rights reserved

TTS provider 注册表 — 扫描 tts/<name>/provider.py 并按需 importlib 加载。

tts/ 目录既是子系统也是 provider 容器（与 adapters/ 同构）：内置的
fish_audio 和用户自写的 provider 平级放在 tts/<name>/。这是仓库里第一个
importlib 动态加载用户代码的先例——provider 代码在 NORA 进程内执行，
只放自己写的、或审阅过的代码。

加载规则：
- 一个文件夹一个 provider：tts/<name>/provider.py 里定义 BaseTTSProvider 子类。
- 类选择：优先名为 ``Provider`` 的子类；没有则取第一个子类（多个时 warning）。
- 名字 = 文件夹名；类里的 name 属性若与文件夹名不一致，以文件夹名为准并 warning。
- 单个 provider 加载失败只 log + 跳过，绝不影响其它 provider 与主程序启动。
'''
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Optional, Type

from tts.base import BaseTTSProvider
from tts.config_utils import (
    load_provider_config,
    provider_dir,
)

logger = logging.getLogger(__name__)

# 模块名前缀，避免污染 sys.modules 常规命名空间
_MODULE_PREFIX = "tts._dynamic_"


def _load_provider_class(pdir: Path) -> Optional[Type[BaseTTSProvider]]:
    """加载一个 provider 目录里的 Provider 类。失败返回 None（只 log）。"""
    module_key = f"{_MODULE_PREFIX}{pdir.name}"
    try:
        spec = importlib.util.spec_from_file_location(module_key, pdir / "provider.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {pdir / 'provider.py'} 创建模块 spec")
        module = importlib.util.module_from_spec(spec)
        # 注册进 sys.modules，provider.py 里若 import 自己目录的兄弟模块可正常工作
        sys.modules[module_key] = module
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_key, None)
        logger.error("加载 TTS provider '%s' 失败，已跳过: %s", pdir.name, e, exc_info=True)
        return None

    subclasses = []
    for attr in vars(module).values():
        if (
            isinstance(attr, type)
            and issubclass(attr, BaseTTSProvider)
            and attr is not BaseTTSProvider
            and getattr(attr, "__module__", "") == module_key
        ):
            subclasses.append(attr)

    if not subclasses:
        logger.error(
            "TTS provider '%s' 的 provider.py 里没有 BaseTTSProvider 子类，已跳过。", pdir.name
        )
        return None

    preferred = next((c for c in subclasses if c.__name__ == "Provider"), None)
    if preferred is not None:
        cls = preferred
    else:
        cls = sorted(subclasses, key=lambda c: c.__name__)[0]
        if len(subclasses) > 1:
            logger.warning(
                "TTS provider '%s' 定义了多个 BaseTTSProvider 子类（%s），使用 '%s'。"
                "建议把要用的类命名为 Provider 消除歧义。",
                pdir.name, ", ".join(c.__name__ for c in subclasses), cls.__name__,
            )
    return cls


def get_provider_class(name: str) -> Optional[Type[BaseTTSProvider]]:
    """按名字取 provider 类；不存在 / 加载失败返回 None。"""
    pdir = provider_dir(name)
    if pdir is None:
        return None
    return _load_provider_class(pdir)


def build_tts_provider(name: str = "", **config_overrides) -> Optional[BaseTTSProvider]:
    """构建 provider 实例（每次新建；构造只读配置无网络，代价可忽略）。

    Args:
        name: provider 名；空则读全局 config.yml 的 tts.provider。
        **config_overrides: 直接覆盖配置 dict 的键（测试用）。

    Raises:
        ValueError: 配置了 provider 名但目录不存在。
    """
    import config

    if not name:
        name = str(config.get_tts_config().get("provider", "") or "").strip()

    cls = get_provider_class(name)
    if cls is None:
        raise ValueError(
            f"TTS provider '{name}' 不存在（在 tts/{name}/provider.py 找不到）。"
            f"可用的 provider 见 tts/ 目录。"
        )

    pdir = provider_dir(name)
    cfg = load_provider_config(pdir)
    cfg.update(config_overrides)
    instance = cls(cfg)

    folder_name = name
    if str(getattr(instance, "name", "") or "").strip() != folder_name:
        logger.warning(
            "TTS provider '%s' 的类 name 属性是 %r，与文件夹名不一致，以文件夹名为准。",
            folder_name, getattr(instance, "name", ""),
        )
        instance.name = folder_name
    return instance


def tts_available() -> bool:
    """是否配置了可用的 TTS provider（前脑注入 [VOICE] 说明的开关）。"""
    try:
        import config
        name = str(config.get_tts_config().get("provider", "") or "").strip()
    except Exception:
        return False
    if not name:
        return False
    return get_provider_class(name) is not None


def get_active_tts_guidance() -> str:
    """取当前 provider 的 [VOICE] 文本写法指南；任何失败返回空串。"""
    try:
        provider = build_tts_provider()
    except Exception as e:
        logger.warning("获取 TTS 写法指南失败: %s", e)
        return ""
    if provider is None:
        return ""
    try:
        return str(provider.get_text_guidance() or "").strip()
    except Exception:
        logger.warning("TTS provider %s 的 get_text_guidance() 抛异常。", provider.name, exc_info=True)
        return ""
