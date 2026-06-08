from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def adapter_dir(adapter_name: str) -> Path:
    """Return the directory for one adapter."""
    return Path(__file__).resolve().parent / adapter_name


def adapter_config_path(adapter_name: str) -> Path:
    """Return the editable config.json path for one adapter."""
    return adapter_dir(adapter_name) / "config.json"


def adapter_config_example_path(adapter_name: str) -> Path:
    """Return the example config path for one adapter."""
    return adapter_dir(adapter_name) / "config.example.json"


def load_adapter_config(adapter_name: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load adapters/<adapter>/config.json, creating it from defaults when missing."""
    path = adapter_config_path(adapter_name)
    defaults = dict(defaults or {})
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(defaults, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return defaults

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")

    merged = dict(defaults)
    merged.update(data)
    return merged
