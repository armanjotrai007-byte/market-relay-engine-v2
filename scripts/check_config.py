"""Validate V2 configuration files without contacting external services."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.config import (  # noqa: E402
    EXPECTED_CONFIG_FILES,
    ConfigValidationError,
    load_all_configs,
    validate_required_config_files,
    validate_required_top_level_sections,
)

FORBIDDEN_V1_TERMS = (
    "use_for_v1_writes",
    "v1_schemas",
    "raw_trades",
    "raw_bbo",
    "raw_tbbo",
    "raw_ohlcv",
)

SECRET_FIELD_MARKERS = ("api_key", "secret", "token", "password", "credential")
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9A-Za-z]{12,}\b"),
)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


def _record(results: list[CheckResult], ok: bool, message: str) -> None:
    results.append(CheckResult(ok=ok, message=message))


def _walk_values(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        entries: list[tuple[str, Any]] = []
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            entries.extend(_walk_values(child, child_path))
        return entries
    if isinstance(value, list):
        entries = []
        for index, child in enumerate(value):
            entries.extend(_walk_values(child, f"{path}[{index}]"))
        return entries
    return [(path, value)]


def _has_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def _find_secret_issues(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for config_name, config in configs.items():
        for path, value in _walk_values(config):
            key = path.split(".")[-1].split("[")[0].lower()
            key_has_secret_marker = any(marker in key for marker in SECRET_FIELD_MARKERS)
            key_is_safe_reference = key.endswith("_env") or key.endswith("_required")
            value_is_empty = value in ("", None, False)

            if key_has_secret_marker and not key_is_safe_reference and not value_is_empty:
                issues.append(f"{config_name}:{path} stores a secret-like value")

            if _has_secret_value(value):
                issues.append(f"{config_name}:{path} matches a known secret pattern")
    return issues


def _find_forbidden_v1_terms(base_dir: Path) -> list[str]:
    issues: list[str] = []
    for file_name in EXPECTED_CONFIG_FILES:
        config_path = base_dir / "config" / file_name
        text = config_path.read_text(encoding="utf-8")
        for term in FORBIDDEN_V1_TERMS:
            if term in text:
                issues.append(f"{file_name}: forbidden V1 term {term}")
    return issues


def _check_context_sources(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    context_sources = configs["context_sources"]

    for source_name, source in context_sources["structured_sources"].items():
        if source.get("enabled") is not False:
            issues.append(f"structured source {source_name} must be disabled by default")
        if source.get("used_in_per_tick_loop") is not False:
            issues.append(f"structured source {source_name} must not run in per-tick loop")

    yfinance = context_sources["structured_sources"]["yfinance_dev_only"]
    if yfinance.get("development_only") is not True:
        issues.append("yfinance_dev_only must be marked development_only")
    if yfinance.get("production_critical") is not False:
        issues.append("yfinance_dev_only must not be production critical")

    for source_name, source in context_sources["unstructured_sources"].items():
        if source.get("enabled") is not False:
            issues.append(f"unstructured source {source_name} must be disabled by default")
        if source.get("direct_trade_authority") is not False:
            issues.append(f"unstructured source {source_name} must not trade directly")

    ai_filter = context_sources["ai_context_filter"]
    if ai_filter.get("enabled") is not False:
        issues.append("AI context filter must be disabled by default")
    if ai_filter.get("direct_trade_authority") is not False:
        issues.append("AI context filter must not have direct trade authority")

    return issues


def _check_calendar_events(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for event_name, event_config in configs["calendar_events"]["event_windows"].items():
        if event_config.get("enabled") is not False:
            issues.append(f"calendar event {event_name} must be disabled by default")
    return issues


def _check_trading_defaults(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    risk_mode = configs["risk_limits"]["mode"]
    broker = configs["execution"]["broker"]
    safety = configs["execution"]["safety"]

    if risk_mode.get("default_trading_mode") != "paper":
        issues.append("risk default_trading_mode must be paper")
    if risk_mode.get("live_trading_enabled_by_default") is not False:
        issues.append("risk live_trading_enabled_by_default must be false")
    if broker.get("enabled") is not False:
        issues.append("broker must be disabled by default")
    if broker.get("paper_trading_only") is not True:
        issues.append("broker must be paper_trading_only by default")
    if broker.get("live_trading_enabled") is not False:
        issues.append("broker live_trading_enabled must be false")
    if safety.get("allow_direct_ai_orders") is not False:
        issues.append("execution safety must reject direct AI orders")
    if safety.get("allow_live_trading_without_manual_config_change") is not False:
        issues.append("live trading must require a manual config change")

    return issues


def _check_questdb_role(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    questdb = configs["questdb"]
    metadata = questdb["metadata"]
    ledger_tables = set(questdb["ledger_tables"])

    if metadata.get("questdb_role") != "bot_ledger_only":
        issues.append("QuestDB role must be bot_ledger_only")
    if metadata.get("not_market_data_warehouse") is not True:
        issues.append("QuestDB must be marked not_market_data_warehouse")
    forbidden_tables = ledger_tables.intersection(FORBIDDEN_V1_TERMS)
    if forbidden_tables:
        issues.append(f"QuestDB ledger tables include forbidden V1 names: {sorted(forbidden_tables)}")

    return issues


def _check_symbol_organization(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    symbols = configs["symbols"]
    tradable = symbols["tradable_universe"]
    context_symbols = symbols["context_symbols"]
    context_symbol_set = {
        symbol
        for group_symbols in context_symbols.values()
        for symbol in group_symbols
    }

    for sector_name, sector_config in tradable.items():
        for symbol in sector_config.get("symbols", []):
            ticker = symbol.get("ticker")
            if symbol.get("approved_for_live") is not False:
                issues.append(f"{ticker} in {sector_name} must not be approved for live trading")
            if ticker in context_symbol_set:
                issues.append(f"{ticker} appears in both tradable and context symbols")

    return issues


def run_config_checks(base_dir: Path | None = None) -> list[CheckResult]:
    root = base_dir or REPO_ROOT
    results: list[CheckResult] = []

    try:
        validate_required_config_files(base_dir=root)
        configs = load_all_configs(base_dir=root)
        _record(results, True, "All expected config files exist and load as YAML dictionaries")
    except Exception as exc:  # noqa: BLE001 - script should report clear validation failure.
        _record(results, False, f"Config loading failed: {exc}")
        return results

    try:
        validate_required_top_level_sections(configs=configs)
        _record(results, True, "Required top-level config sections exist")
    except ConfigValidationError as exc:
        _record(results, False, str(exc))

    secret_issues = _find_secret_issues(configs)
    _record(results, not secret_issues, f"No obvious committed secrets: {secret_issues or 'ok'}")

    context_issues = _check_context_sources(configs)
    _record(
        results,
        not context_issues,
        f"Context sources are disabled or development-safe by default: {context_issues or 'ok'}",
    )

    calendar_issues = _check_calendar_events(configs)
    _record(
        results,
        not calendar_issues,
        f"Calendar event windows are disabled by default: {calendar_issues or 'ok'}",
    )

    trading_issues = _check_trading_defaults(configs)
    _record(results, not trading_issues, f"Trading/execution defaults are paper-safe: {trading_issues or 'ok'}")

    questdb_issues = _check_questdb_role(configs)
    _record(results, not questdb_issues, f"QuestDB is bot-ledger-only: {questdb_issues or 'ok'}")

    symbol_issues = _check_symbol_organization(configs)
    _record(results, not symbol_issues, f"Symbol config separates tradable/context symbols: {symbol_issues or 'ok'}")

    forbidden_issues = _find_forbidden_v1_terms(root)
    _record(results, not forbidden_issues, f"No forbidden V1 raw market-data names: {forbidden_issues or 'ok'}")

    return results


def main() -> int:
    results = run_config_checks()

    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.message}")

    print()
    failures = [result for result in results if not result.ok]
    if failures:
        print(f"Config validation FAILED with {len(failures)} failure(s).")
        return 1

    print("Config validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
