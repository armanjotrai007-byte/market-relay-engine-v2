from pathlib import Path

import pytest

from market_relay_engine.common.config import (
    EXPECTED_CONFIG_FILES,
    REQUIRED_TOP_LEVEL_SECTIONS,
    ConfigValidationError,
    load_all_configs,
    load_yaml_config,
    validate_required_config_files,
    validate_required_top_level_sections,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_load_yaml_config_accepts_name_relative_path_and_absolute_path() -> None:
    by_name = load_yaml_config("symbols", base_dir=REPO_ROOT)
    by_relative_path = load_yaml_config("config/symbols.yaml", base_dir=REPO_ROOT)
    by_absolute_path = load_yaml_config(REPO_ROOT / "config" / "symbols.yaml")

    assert by_name == by_relative_path == by_absolute_path
    assert "tradable_universe" in by_name
    assert "context_symbols" in by_name


def test_load_all_configs_returns_expected_dictionaries() -> None:
    configs = load_all_configs(base_dir=REPO_ROOT)

    assert set(configs) == {Path(file_name).stem for file_name in EXPECTED_CONFIG_FILES}
    assert all(isinstance(config, dict) for config in configs.values())


def test_validate_required_config_files_returns_paths() -> None:
    paths = validate_required_config_files(base_dir=REPO_ROOT)

    assert len(paths) == len(EXPECTED_CONFIG_FILES)
    assert all(path.is_file() for path in paths)


def test_required_top_level_sections_are_present() -> None:
    configs = load_all_configs(base_dir=REPO_ROOT)

    validate_required_top_level_sections(configs=configs)

    for file_name, required_sections in REQUIRED_TOP_LEVEL_SECTIONS.items():
        config = configs[Path(file_name).stem]
        assert set(required_sections).issubset(config)


def test_required_top_level_sections_raise_clear_error_for_missing_section() -> None:
    configs = load_all_configs(base_dir=REPO_ROOT)
    broken_symbols = dict(configs["symbols"])
    broken_symbols.pop("context_symbols")
    configs["symbols"] = broken_symbols

    with pytest.raises(ConfigValidationError, match="symbols.yaml: missing context_symbols"):
        validate_required_top_level_sections(configs=configs)
