"""Config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

EXPECTED_CONFIG_FILES: tuple[str, ...] = (
    "symbols.yaml",
    "context_sources.yaml",
    "risk_limits.yaml",
    "questdb.yaml",
    "model_config.yaml",
    "calendar_events.yaml",
    "execution.yaml",
    "context_refresh.yaml",
)

REQUIRED_TOP_LEVEL_SECTIONS: dict[str, tuple[str, ...]] = {
    "symbols.yaml": ("metadata", "tradable_universe", "context_symbols"),
    "context_sources.yaml": (
        "metadata",
        "structured_sources",
        "unstructured_sources",
        "ai_context_filter",
    ),
    "risk_limits.yaml": (
        "metadata",
        "mode",
        "signal_thresholds",
        "market_quality",
        "execution_quality",
        "position_limits",
        "daily_limits",
        "event_risk",
        "portfolio_risk",
    ),
    "questdb.yaml": (
        "metadata",
        "connection",
        "ledger_tables",
        "forbidden_uses",
        "jsonl_fallback",
    ),
    "model_config.yaml": (
        "metadata",
        "feature_pipeline",
        "model",
        "calibration",
        "prediction_horizons",
        "labels",
    ),
    "calendar_events.yaml": ("metadata", "market", "event_windows"),
    "execution.yaml": (
        "metadata",
        "broker",
        "order_defaults",
        "required_execution_metrics",
        "safety",
    ),
    "context_refresh.yaml": ("schema_version", "source_order", "sources"),
}


class ConfigValidationError(ValueError):
    """Raised when repository config files fail validation."""


def repo_root() -> Path:
    """Return the repository root when running from the source tree."""
    return Path(__file__).resolve().parents[3]


def config_dir(base_dir: str | Path | None = None) -> Path:
    """Return the config directory for a repository root."""
    root = Path(base_dir) if base_dir is not None else repo_root()
    return root / "config"


def _resolve_config_path(
    path_or_name: str | Path,
    base_dir: str | Path | None = None,
) -> Path:
    config_path = Path(path_or_name)
    if config_path.is_absolute():
        return config_path

    if config_path.suffix == "":
        config_path = config_path.with_suffix(".yaml")

    root = Path(base_dir) if base_dir is not None else repo_root()
    if config_path.parts and config_path.parts[0] == "config":
        return root / config_path
    return root / "config" / config_path


def load_yaml_config(path: str | Path, base_dir: str | Path | None = None) -> dict[str, Any]:
    """Load a YAML mapping from config name, relative path, or absolute path."""
    config_path = _resolve_config_path(path, base_dir=base_dir)

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


def validate_required_config_files(base_dir: str | Path | None = None) -> list[Path]:
    """Validate that all expected config files exist and return their paths."""
    expected_paths = [config_dir(base_dir) / file_name for file_name in EXPECTED_CONFIG_FILES]
    missing = [str(path) for path in expected_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing required config file(s): {', '.join(missing)}")
    return expected_paths


def load_all_configs(base_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load every expected config file from the config directory."""
    validate_required_config_files(base_dir=base_dir)
    return {
        Path(file_name).stem: load_yaml_config(file_name, base_dir=base_dir)
        for file_name in EXPECTED_CONFIG_FILES
    }


def validate_required_top_level_sections(
    configs: dict[str, dict[str, Any]] | None = None,
    base_dir: str | Path | None = None,
) -> None:
    """Validate required top-level sections for every expected config."""
    loaded_configs = configs if configs is not None else load_all_configs(base_dir=base_dir)
    missing_sections: list[str] = []

    for file_name, required_sections in REQUIRED_TOP_LEVEL_SECTIONS.items():
        config_name = Path(file_name).stem
        config = loaded_configs.get(config_name)
        if config is None:
            missing_sections.append(f"{file_name}: missing config")
            continue
        for section in required_sections:
            if section not in config:
                missing_sections.append(f"{file_name}: missing {section}")

    if missing_sections:
        raise ConfigValidationError(
            "Missing required top-level config section(s): "
            + "; ".join(missing_sections)
        )
