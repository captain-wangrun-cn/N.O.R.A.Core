"""模型目录查询：telegram 与 qqbot 的 /model 菜单共用。

从 adapters/telegram/commands.py 抽出的纯函数，行为保持不变。
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List


def provider_for_alias(cfg: Dict[str, Any], alias: str) -> str:
    llm_cfg = cfg.get("llm", {}) or {}
    model_providers = llm_cfg.get("model_providers", {}) or {}
    return model_providers.get(alias) or llm_cfg.get("provider", "gemini")


def provider_config(cfg: Dict[str, Any], provider_name: str) -> Dict[str, Any]:
    llm_cfg = cfg.get("llm", {}) or {}
    providers_cfg = llm_cfg.get("providers", {}) or {}
    if provider_name in providers_cfg:
        return providers_cfg.get(provider_name) or {}

    api_keys = llm_cfg.get("api_keys", {}) or {}
    legacy = {
        "type": provider_name,
        "api_key": api_keys.get(provider_name, ""),
    }
    if provider_name == "openai":
        if llm_cfg.get("base_url"):
            legacy["base_url"] = llm_cfg.get("base_url")
        if llm_cfg.get("user_agent"):
            legacy["user_agent"] = llm_cfg.get("user_agent")
    return legacy


def provider_names(cfg: Dict[str, Any]) -> List[str]:
    llm_cfg = cfg.get("llm", {}) or {}
    providers_cfg = llm_cfg.get("providers", {}) or {}
    names = list(providers_cfg.keys())
    if not names:
        names = [llm_cfg.get("provider", "gemini")]
    return names


def fetch_provider_models(provider_name: str, provider_cfg: Dict[str, Any]) -> List[str]:
    provider_type = provider_cfg.get("type", provider_name)
    api_key = provider_cfg.get("api_key", "")
    if not api_key:
        return []

    try:
        if provider_type == "gemini":
            genai = importlib.import_module("google.generativeai")
            if hasattr(genai, "configure"):
                genai.configure(api_key=api_key)
            if hasattr(genai, "list_models"):
                models = [
                    m.name.replace("models/", "")
                    for m in genai.list_models()
                    if "generateContent" in getattr(m, "supported_generation_methods", [])
                ]
                return sorted(models)
            return []
        if provider_type == "openai":
            openai_module = importlib.import_module("openai")
            base_url = provider_cfg.get("base_url") or None
            client = openai_module.OpenAI(api_key=api_key, base_url=base_url)
            return sorted([model.id for model in client.models.list()])
    except Exception:
        return []

    return []
