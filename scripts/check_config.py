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
from market_relay_engine.context.eia_wpsr import EIAWPSRConfig  # noqa: E402
from market_relay_engine.context.fred_collector import FREDConfig  # noqa: E402
from market_relay_engine.context.macro_calendar import (  # noqa: E402
    MacroCalendarConfig,
    load_macro_calendar,
)
from market_relay_engine.context.usaspending_collector import (  # noqa: E402
    USAspendingConfig,
    load_recipient_mappings,
)
from market_relay_engine.context.yfinance_proxy import YFinanceProxyConfig, build_proxy_registry  # noqa: E402
from market_relay_engine.questdb.writer import ALLOWED_LEDGER_TABLES  # noqa: E402

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
            key_is_safe_reference = (
                key.endswith("_env")
                or key.endswith("_required")
                or (key == "max_output_tokens" and isinstance(value, int))
            )
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


def _check_context_sources(
    configs: dict[str, dict[str, Any]],
    *,
    base_dir: Path,
) -> list[str]:
    issues: list[str] = []
    context_sources = configs["context_sources"]
    structured_sources = context_sources["structured_sources"]

    for source_name, source in structured_sources.items():
        if source.get("used_in_per_tick_loop") is not False:
            issues.append(f"structured source {source_name} must not run in per-tick loop")

    _check_eia_source(configs, issues)
    _check_fred_source(context_sources, issues)
    _check_usaspending_source(context_sources, issues, base_dir=base_dir)
    _check_macro_calendar_source(context_sources, issues, base_dir=base_dir)
    _check_sec_edgar_source(context_sources, issues, base_dir=base_dir)

    yfinance = structured_sources["yfinance_dev_only"]
    if yfinance.get("development_only") is not True:
        issues.append("yfinance_dev_only must be marked development_only")
    if yfinance.get("production_critical") is not False:
        issues.append("yfinance_dev_only must not be production critical")
    if yfinance.get("feeds_memory_cache") is not True:
        issues.append("yfinance_dev_only must feed the memory cache")
    if yfinance.get("writes_questdb_ledger") is not True:
        issues.append("yfinance_dev_only must declare optional QuestDB ledger writes")
    try:
        YFinanceProxyConfig.from_repository_configs(context_sources, configs["symbols"])
    except Exception as exc:  # noqa: BLE001 - validation script reports all config failures clearly.
        issues.append(f"yfinance_dev_only PR25 config invalid: {exc}")
    else:
        grace = yfinance.get("bar_completion_grace_seconds")
        staleness = yfinance.get("max_staleness_seconds")
        if yfinance.get("interval") != "5m":
            issues.append("yfinance_dev_only interval must be 5m")
        if not isinstance(grace, int) or not isinstance(staleness, int) or staleness < 300 + grace:
            issues.append("yfinance_dev_only max_staleness_seconds must be at least 300 + bar_completion_grace_seconds")

    for source_name, source in context_sources["unstructured_sources"].items():
        if source.get("direct_trade_authority") is not False:
            issues.append(f"unstructured source {source_name} must not trade directly")

    _check_ai_context_filter(context_sources["ai_context_filter"], issues)

    return issues


def _check_sec_edgar_source(
    context_sources: dict[str, Any],
    issues: list[str],
    *,
    base_dir: Path,
) -> None:
    """Validate the source-specific PR36 SEC boundary without contacting SEC."""
    try:
        from market_relay_engine.context.sec_edgar import SECEDGARSettings, load_sec_issuers

        SECEDGARSettings.from_repository_config(context_sources, base_dir=base_dir)
        load_sec_issuers(base_dir=base_dir)
    except Exception as exc:  # noqa: BLE001 - aggregate configuration diagnostics.
        issues.append(f"sec_edgar PR36 config invalid: {exc}")


def _check_ai_context_filter(ai_filter: Any, issues: list[str]) -> None:
    """Validate the fixed PR35 Gemini safety and resource boundaries."""

    if not isinstance(ai_filter, dict):
        issues.append("ai_context_filter must be a mapping")
        return

    if ai_filter.get("enabled") is not False:
        issues.append("ai_context_filter enabled must default to false")
    if ai_filter.get("provider") != "gemini":
        issues.append("ai_context_filter provider must be 'gemini'")
    for name in (
        "model",
        "api_key_env",
        "prompt_version",
        "response_schema_version",
    ):
        if not _non_empty_string(ai_filter.get(name)):
            issues.append(f"ai_context_filter {name} must be a non-empty string")
    if ai_filter.get("api_key_env") != "GEMINI_API_KEY":
        issues.append("ai_context_filter api_key_env must reference GEMINI_API_KEY")
    if ai_filter.get("prompt_version") != "context_filter_v1":
        issues.append("ai_context_filter prompt_version must be context_filter_v1")
    if ai_filter.get("response_schema_version") != "context_classification_response_v1":
        issues.append(
            "ai_context_filter response_schema_version must be context_classification_response_v1"
        )
    if ai_filter.get("temperature") != 0 or isinstance(ai_filter.get("temperature"), bool):
        issues.append("ai_context_filter temperature must be 0")

    for name in (
        "timeout_seconds",
        "retry_base_delay_seconds",
        "retry_max_delay_seconds",
    ):
        actual = ai_filter.get(name)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or actual <= 0:
            issues.append(f"ai_context_filter {name} must be a positive number")

    max_retries = ai_filter.get("max_retries")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 2:
        issues.append("ai_context_filter max_retries must be an integer from 0 through 2")

    for name in (
        "max_input_characters",
        "max_prompt_characters",
        "max_summary_characters",
        "max_output_tokens",
        "max_provider_calls_per_minute",
        "max_provider_calls_per_run",
        "dedup_cache_max_entries",
    ):
        actual = ai_filter.get(name)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual <= 0:
            issues.append(f"ai_context_filter {name} must be a positive integer")

    retry_base = ai_filter.get("retry_base_delay_seconds")
    retry_max = ai_filter.get("retry_max_delay_seconds")
    if (
        isinstance(retry_base, (int, float))
        and not isinstance(retry_base, bool)
        and isinstance(retry_max, (int, float))
        and not isinstance(retry_max, bool)
        and retry_max < retry_base
    ):
        issues.append(
            "ai_context_filter retry_max_delay_seconds must be at least retry_base_delay_seconds"
        )

    if ai_filter.get("direct_trade_authority") is not False:
        issues.append("AI context filter must not have direct trade authority")


def _check_eia_source(configs: dict[str, dict[str, Any]], issues: list[str]) -> None:
    source = configs["context_sources"]["structured_sources"]["eia"]
    window = configs["calendar_events"]["event_windows"]["eia"]
    if source.get("enabled") is not True and window.get("enabled") is not True:
        return
    if not _non_empty_string(source.get("api_key_env")):
        issues.append("enabled EIA source requires non-empty api_key_env reference")
    if source.get("enabled") is True and window.get("enabled") is not True:
        issues.append("enabled EIA source requires calendar_events.event_windows.eia.enabled true")
    releases = window.get("releases")
    if window.get("enabled") is True and (not isinstance(releases, list) or not releases):
        issues.append("enabled EIA source requires at least one reviewed release")
    try:
        EIAWPSRConfig.from_repository_configs(
            configs["calendar_events"],
            configs["context_sources"],
            configs["symbols"],
        )
    except Exception as exc:  # noqa: BLE001 - validation reports all config failures.
        issues.append(f"EIA WPSR config invalid: {exc}")


def _check_fred_source(context_sources: dict[str, Any], issues: list[str]) -> None:
    fred = context_sources["structured_sources"]["fred"]
    if fred.get("enabled") is not True:
        return
    if not _non_empty_string(fred.get("api_key_env")):
        issues.append("enabled FRED source requires non-empty api_key_env reference")
    series_ids = fred.get("series_ids")
    if not isinstance(series_ids, dict) or not series_ids:
        issues.append("enabled FRED source requires at least one series id")
    try:
        FREDConfig.from_repository_config(context_sources)
    except Exception as exc:  # noqa: BLE001 - validation reports all config failures.
        issues.append(f"FRED config invalid: {exc}")


def _check_usaspending_source(
    context_sources: dict[str, Any],
    issues: list[str],
    *,
    base_dir: Path,
) -> None:
    source = context_sources["structured_sources"]["usaspending"]
    if source.get("enabled") is not True:
        return
    if not _non_empty_string(source.get("recipient_map_path")):
        issues.append("enabled USAspending source requires recipient_map_path")
        return
    health_only_allowed = (
        context_sources.get("validation_modes", {})
        .get("usaspending", {})
        .get("allow_health_only_without_recipient_mapping")
        is True
    )
    try:
        config = USAspendingConfig.from_repository_config(context_sources)
        recipient_path = Path(config.recipient_map_path)
        resolved = recipient_path if recipient_path.is_absolute() else base_dir / recipient_path
        mappings = load_recipient_mappings(resolved)
    except Exception as exc:  # noqa: BLE001 - validation reports all config failures.
        issues.append(f"USAspending config invalid: {exc}")
        return
    active_mappings = [mapping for mapping in mappings if mapping.active]
    if not active_mappings and not health_only_allowed:
        issues.append(
            "enabled USAspending source requires at least one active confirmed recipient mapping "
            "or validation_modes.usaspending.allow_health_only_without_recipient_mapping true"
        )


def _check_macro_calendar_source(
    context_sources: dict[str, Any],
    issues: list[str],
    *,
    base_dir: Path,
) -> None:
    source = context_sources["structured_sources"]["macro_calendar"]
    if source.get("enabled") is not True:
        return
    if not _non_empty_string(source.get("artifact_path")):
        issues.append("enabled macro_calendar source requires artifact_path")
        return
    try:
        config = MacroCalendarConfig.from_repository_config(context_sources)
        artifact_path = Path(config.artifact_path)
        resolved = artifact_path if artifact_path.is_absolute() else base_dir / artifact_path
        if not resolved.is_file():
            issues.append(f"enabled macro_calendar source artifact_path does not exist: {config.artifact_path}")
            return
        load_macro_calendar(config.artifact_path, base_dir=base_dir)
    except Exception as exc:  # noqa: BLE001 - validation reports all config failures.
        issues.append(f"macro_calendar config invalid: {exc}")


def _check_calendar_events(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for event_name, event_config in configs["calendar_events"]["event_windows"].items():
        if not isinstance(event_config.get("enabled"), bool):
            issues.append(f"calendar event {event_name} enabled must be bool")
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
    if broker.get("name") != "alpaca":
        issues.append("broker name must remain alpaca")
    if not isinstance(broker.get("enabled"), bool):
        issues.append("broker enabled must be bool")
    if broker.get("paper_trading_only") is not True:
        issues.append("broker must remain paper_trading_only")
    if broker.get("live_trading_enabled") is not False:
        issues.append("broker live_trading_enabled must be false")
    if safety.get("allow_direct_ai_orders") is not False:
        issues.append("execution safety must reject direct AI orders")
    if safety.get("allow_live_trading_without_manual_config_change") is not False:
        issues.append("live trading must require a manual config change")

    return issues


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_questdb_role(configs: dict[str, dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    questdb = configs["questdb"]
    metadata = questdb["metadata"]
    configured_tables = questdb["ledger_tables"]
    ledger_tables = set(configured_tables)

    if metadata.get("questdb_role") != "bot_ledger_only":
        issues.append("QuestDB role must be bot_ledger_only")
    if metadata.get("not_market_data_warehouse") is not True:
        issues.append("QuestDB must be marked not_market_data_warehouse")
    forbidden_tables = ledger_tables.intersection(FORBIDDEN_V1_TERMS)
    if forbidden_tables:
        issues.append(f"QuestDB ledger tables include forbidden V1 names: {sorted(forbidden_tables)}")
    if configured_tables != list(ALLOWED_LEDGER_TABLES):
        issues.append(
            "QuestDB ledger_tables must exactly match writer ALLOWED_LEDGER_TABLES order"
        )

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

    try:
        registry = build_proxy_registry(symbols)
    except Exception as exc:  # noqa: BLE001 - validation script reports all config failures clearly.
        issues.append(f"PR25 proxy symbol registry invalid: {exc}")
    else:
        expected = {"SPY", "QQQ", "IWM", "GLD", "^VIX", "XLE", "XOP", "OIH", "XLI", "PPA", "ITA"}
        if set(registry) != expected:
            issues.append(f"PR25 proxy registry symbols changed unexpectedly: {sorted(registry)}")

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

    context_issues = _check_context_sources(configs, base_dir=root)
    _record(
        results,
        not context_issues,
        f"Context sources are online-capable and trading-safe: {context_issues or 'ok'}",
    )

    calendar_issues = _check_calendar_events(configs)
    _record(
        results,
        not calendar_issues,
        f"Calendar event windows are configured safely: {calendar_issues or 'ok'}",
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
