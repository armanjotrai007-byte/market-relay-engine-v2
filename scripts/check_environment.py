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
    "db",
    "db/schema",
    "src/market_relay_engine",
    "src/market_relay_engine/common",
    "src/market_relay_engine/contracts",
    "src/market_relay_engine/market_data",
    "src/market_relay_engine/questdb",
    "src/market_relay_engine/context",
    "src/market_relay_engine/ai_context",
    "src/market_relay_engine/model",
    "src/market_relay_engine/risk",
    "src/market_relay_engine/execution",
    "src/market_relay_engine/ledger",
    "src/market_relay_engine/analysis",
    "scripts",
    "tests/fixtures",
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
    "scripts/check_contracts.py",
    "scripts/check_fixtures.py",
    "scripts/check_historical_parquet.py",
    "scripts/check_dbn_inspector.py",
    "scripts/check_feature_builder.py",
    "scripts/check_feature_parity.py",
    "scripts/check_cost_model.py",
    "scripts/check_label_builder.py",
    "scripts/check_risk_filter.py",
    "scripts/check_risk_logging.py",
    "scripts/check_order_manager.py",
    "scripts/check_position_state.py",
    "scripts/check_alpaca_paper.py",
    "scripts/check_execution_metrics.py",
    "scripts/check_fill_reconciliation.py",
    "scripts/check_fake_paper_loop.py",
    "scripts/check_context_state_cache.py",
    "scripts/check_yfinance_proxy.py",
    "scripts/check_questdb.py",
    "scripts/check_questdb_schema.py",
    "scripts/check_questdb_writer.py",
    "scripts/check_questdb_analysis.py",
    "scripts/inspect_historical_parquets.py",
    "scripts/inspect_dbn_file.py",
    "scripts/run_tests.ps1",
    "docs/configuration.md",
    "docs/data_contracts.md",
    "docs/historical_parquet_reader.md",
    "docs/dbn_inspection.md",
    "docs/feature_builder.md",
    "docs/feature_parity.md",
    "docs/cost_model.md",
    "docs/label_builder.md",
    "docs/risk_filter.md",
    "docs/risk_logging.md",
    "docs/order_manager.md",
    "docs/position_state.md",
    "docs/alpaca_paper.md",
    "docs/execution_metrics.md",
    "docs/fill_reconciliation.md",
    "docs/fake_paper_loop.md",
    "docs/context_state_cache.md",
    "docs/yfinance_proxy.md",
    "docs/questdb_health.md",
    "docs/questdb_schema.md",
    "docs/questdb_writer.md",
    "docs/questdb_analysis.md",
    "db/schema/questdb_ledger_v1.sql",
    "src/market_relay_engine/common/ids.py",
    "src/market_relay_engine/common/serialization.py",
    "src/market_relay_engine/market_data/historical_parquet.py",
    "src/market_relay_engine/market_data/dbn_inspector.py",
    "src/market_relay_engine/market_data/feature_builder.py",
    "src/market_relay_engine/market_data/feature_parity.py",
    "src/market_relay_engine/market_data/cost_model.py",
    "src/market_relay_engine/market_data/label_builder.py",
    "src/market_relay_engine/context/__init__.py",
    "src/market_relay_engine/context/state_cache.py",
    "src/market_relay_engine/context/yfinance_proxy.py",
    "src/market_relay_engine/risk/__init__.py",
    "src/market_relay_engine/risk/risk_filter.py",
    "src/market_relay_engine/risk/rules.py",
    "src/market_relay_engine/risk/decisions.py",
    "src/market_relay_engine/risk/logging.py",
    "src/market_relay_engine/execution/order_manager.py",
    "src/market_relay_engine/execution/position_state.py",
    "src/market_relay_engine/execution/alpaca_paper.py",
    "src/market_relay_engine/execution/execution_metrics.py",
    "src/market_relay_engine/execution/fill_reconciliation.py",
    "src/market_relay_engine/execution/fake_paper_loop.py",
    "src/market_relay_engine/questdb/__init__.py",
    "src/market_relay_engine/questdb/health.py",
    "src/market_relay_engine/questdb/writer.py",
    "src/market_relay_engine/questdb/analysis.py",
    "src/market_relay_engine/contracts/__init__.py",
    "src/market_relay_engine/contracts/base.py",
    "src/market_relay_engine/contracts/market.py",
    "src/market_relay_engine/contracts/features.py",
    "src/market_relay_engine/contracts/model.py",
    "src/market_relay_engine/contracts/risk.py",
    "src/market_relay_engine/contracts/context.py",
    "src/market_relay_engine/contracts/execution.py",
    "src/market_relay_engine/contracts/ledger.py",
    "src/market_relay_engine/contracts/system.py",
    "tests/unit/test_imports.py",
    "tests/unit/test_time_utils.py",
    "tests/unit/test_ids.py",
    "tests/unit/test_logging.py",
    "tests/unit/test_serialization.py",
    "tests/unit/test_contracts_market.py",
    "tests/unit/test_contracts_features.py",
    "tests/unit/test_contracts_model.py",
    "tests/unit/test_contracts_risk.py",
    "tests/unit/test_contracts_context.py",
    "tests/unit/test_contracts_execution.py",
    "tests/unit/test_contracts_ledger.py",
    "tests/unit/test_contracts_system.py",
    "tests/unit/test_historical_parquet.py",
    "tests/unit/test_config_files_exist.py",
    "tests/unit/test_config_loader.py",
    "tests/unit/test_config_validation.py",
    "tests/unit/test_feature_parity.py",
    "tests/unit/test_cost_model.py",
    "tests/unit/test_label_builder.py",
    "tests/unit/test_risk_filter.py",
    "tests/unit/test_risk_logging.py",
    "tests/unit/test_order_manager.py",
    "tests/unit/test_position_state.py",
    "tests/unit/test_alpaca_paper.py",
    "tests/unit/test_execution_metrics.py",
    "tests/unit/test_fill_reconciliation.py",
    "tests/unit/test_fake_paper_loop.py",
    "tests/unit/test_context_state_cache.py",
    "tests/unit/test_yfinance_proxy.py",
    "tests/unit/test_questdb_health.py",
    "tests/unit/test_questdb_analysis.py",
    "tests/fixtures/__init__.py",
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
    "market_relay_engine.common.ids",
    "market_relay_engine.common.serialization",
    "market_relay_engine.contracts",
    "market_relay_engine.contracts.base",
    "market_relay_engine.contracts.market",
    "market_relay_engine.contracts.features",
    "market_relay_engine.contracts.model",
    "market_relay_engine.contracts.risk",
    "market_relay_engine.contracts.context",
    "market_relay_engine.contracts.execution",
    "market_relay_engine.contracts.ledger",
    "market_relay_engine.contracts.system",
    "market_relay_engine.market_data.historical_parquet",
    "market_relay_engine.market_data.dbn_inspector",
    "market_relay_engine.market_data.feature_builder",
    "market_relay_engine.market_data.feature_parity",
    "market_relay_engine.market_data.cost_model",
    "market_relay_engine.market_data.label_builder",
    "market_relay_engine.context",
    "market_relay_engine.context.state_cache",
    "market_relay_engine.context.yfinance_proxy",
    "market_relay_engine.risk",
    "market_relay_engine.risk.risk_filter",
    "market_relay_engine.risk.rules",
    "market_relay_engine.risk.decisions",
    "market_relay_engine.risk.logging",
    "market_relay_engine.execution.order_manager",
    "market_relay_engine.execution.position_state",
    "market_relay_engine.execution.alpaca_paper",
    "market_relay_engine.execution.execution_metrics",
    "market_relay_engine.execution.fill_reconciliation",
    "market_relay_engine.execution.fake_paper_loop",
    "market_relay_engine.questdb",
    "market_relay_engine.questdb.health",
    "market_relay_engine.questdb.writer",
    "market_relay_engine.questdb.analysis",
    "tests.fixtures",
    "tests.fixtures.ids",
    "tests.fixtures.times",
    "tests.fixtures.market_records",
    "tests.fixtures.feature_snapshots",
    "tests.fixtures.model_signals",
    "tests.fixtures.risk_decisions",
    "tests.fixtures.context",
    "tests.fixtures.execution",
    "tests.fixtures.ledger",
    "tests.fixtures.system",
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
