"""Local environment health check for Trading System V2."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

REQUIRED_CONFIG_FILES = [
    "config/symbols.yaml",
    "config/context_sources.yaml",
    "config/risk_limits.yaml",
    "config/questdb.yaml",
    "config/model_config.yaml",
    "config/calendar_events.yaml",
    "config/execution.yaml",
]

REQUIRED_DIRECTORIES = [
    "docs",
    "config",
    "src/market_relay_engine",
    "src/market_relay_engine/common",
    "src/market_relay_engine/market_data",
    "src/market_relay_engine/context",
    "src/market_relay_engine/ai_context",
    "src/market_relay_engine/model",
    "src/market_relay_engine/risk",
    "src/market_relay_engine/execution",
    "src/market_relay_engine/ledger",
    "src/market_relay_engine/analysis",
    "scripts",
    "tests/unit",
    "tests/integration",
    "data",
    "data/raw",
    "data/parquet",
    "data/reports",
    "data/logs",
    "data/emergency_ledger",
    "logs",
]

REQUIRED_FILES = [
    "README.md",
    "AGENTS.md",
    "handoff.md",
    "pyproject.toml",
    "requirements.txt",
    ".gitignore",
    ".env.example",
    "scripts/check_environment.py",
    "scripts/check_config.py",
    "scripts/run_tests.ps1",
    "docs/configuration.md",
    "tests/unit/test_imports.py",
    "tests/unit/test_time_utils.py",
    "tests/unit/test_config_files_exist.py",
    "tests/unit/test_config_loader.py",
    "tests/unit/test_config_validation.py",
    "tests/integration/.gitkeep",
    "data/.gitkeep",
    "data/raw/.gitkeep",
    "data/parquet/.gitkeep",
    "data/reports/.gitkeep",
    "data/logs/.gitkeep",
    "data/emergency_ledger/.gitkeep",
    "logs/.gitkeep",
]

REQUIRED_IMPORTS = [
    "market_relay_engine",
    "market_relay_engine.common.time",
    "market_relay_engine.common.logging",
    "market_relay_engine.common.config",
]


def _path(relative_path: str) -> Path:
    return REPO_ROOT / relative_path


def _record(results: list[tuple[bool, str]], ok: bool, message: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {message}")
    results.append((ok, message))


def _check_all_exist(paths: list[str], predicate: Callable[[Path], bool]) -> list[str]:
    return [path for path in paths if not predicate(_path(path))]


def main() -> int:
    results: list[tuple[bool, str]] = []

    _record(
        results,
        sys.version_info >= (3, 12),
        f"Python version is {sys.version.split()[0]} (requires 3.12+)",
    )

    missing_configs = _check_all_exist(REQUIRED_CONFIG_FILES, Path.is_file)
    _record(results, not missing_configs, f"Required config files exist: {missing_configs or 'ok'}")

    missing_dirs = _check_all_exist(REQUIRED_DIRECTORIES, Path.is_dir)
    _record(results, not missing_dirs, f"Required directories exist: {missing_dirs or 'ok'}")

    missing_files = _check_all_exist(REQUIRED_FILES, Path.is_file)
    _record(results, not missing_files, f"Required placeholder files exist: {missing_files or 'ok'}")

    _record(results, _path(".env.example").is_file(), ".env.example exists")

    import_errors: list[str] = []
    for module_name in REQUIRED_IMPORTS:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - health check should report all import failures.
            import_errors.append(f"{module_name}: {exc}")
    _record(results, not import_errors, f"Package imports work: {import_errors or 'ok'}")

    try:
        from market_relay_engine.common.config import load_yaml_config

        symbols = load_yaml_config("config/symbols.yaml")
        yaml_ok = "tradable_universe" in symbols and "context_symbols" in symbols
        yaml_message = "YAML loader can read config/symbols.yaml"
    except Exception as exc:  # noqa: BLE001 - health check should report clear failure.
        yaml_ok = False
        yaml_message = f"YAML loader failed: {exc}"
    _record(results, yaml_ok, yaml_message)

    try:
        from scripts.check_config import run_config_checks

        config_results = run_config_checks(REPO_ROOT)
        config_failures = [result.message for result in config_results if not result.ok]
        config_ok = not config_failures
        config_message = f"Config validation passes: {config_failures or 'ok'}"
    except Exception as exc:  # noqa: BLE001 - health check should report clear failure.
        config_ok = False
        config_message = f"Config validation failed to run: {exc}"
    _record(results, config_ok, config_message)

    failures = [message for ok, message in results if not ok]
    print()
    if failures:
        print(f"Environment check FAILED with {len(failures)} failure(s).")
        return 1

    print("Environment check PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
