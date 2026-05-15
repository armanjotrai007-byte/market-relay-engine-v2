"""Config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def repo_root() -> Path:
    """Return the repository root when running from the source tree."""
    return Path(__file__).resolve().parents[3]


def load_yaml_config(path: str | Path, base_dir: str | Path | None = None) -> dict[str, Any]:
    """Load a YAML mapping from an absolute path or a path relative to repo root."""
    config_path = Path(path)
    if not config_path.is_absolute():
        root = Path(base_dir) if base_dir is not None else repo_root()
        config_path = root / config_path

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML config file: {config_path}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"YAML config must contain a top-level mapping: {config_path}")
    return loaded
