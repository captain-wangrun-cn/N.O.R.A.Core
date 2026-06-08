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


def read_adapter_config(adapter_name: str) -> dict[str, Any] | None:
    """Read adapters/<adapter>/config.json without creating it."""
    path = adapter_config_path(adapter_name)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def load_adapter_config(
    adapter_name: str,
    defaults: dict[str, Any] | None = None,
    *,
    create_if_missing: bool = True,
) -> dict[str, Any]:
    """Load adapters/<adapter>/config.json, optionally creating it from defaults."""
    path = adapter_config_path(adapter_name)
    defaults = dict(defaults or {})
    data = read_adapter_config(adapter_name)
    if data is None:
        if create_if_missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(defaults, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return defaults

    merged = dict(defaults)
    merged.update(data)
    return merged


def is_placeholder_value(value: Any) -> bool:
    """Return true for common unedited placeholder strings."""
    text = str(value or "").strip()
    if not text:
        return True
    upper = text.upper()
    return upper.startswith("YOUR_") or upper in {"PLACEHOLDER", "CHANGE_ME", "TODO"}
