from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from market_relay_engine.ai_context.settings import load_ai_context_filter_settings
from market_relay_engine.common.config import (
    EXPECTED_CONFIG_FILES,
    ConfigValidationError,
    load_all_configs,
)
from market_relay_engine.questdb.writer import ALLOWED_LEDGER_TABLES
from scripts.check_config import (
    FORBIDDEN_V1_TERMS,
    _check_ai_context_filter,
    _check_context_sources,
    _check_questdb_role,
    _check_trading_defaults,
    _find_forbidden_v1_terms,
    _find_secret_issues,
    run_config_checks,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_config_validation_script_checks_pass() -> None:
    results = run_config_checks(REPO_ROOT)
    failures = [result.message for result in results if not result.ok]

    assert failures == []


def test_no_obvious_secrets_are_committed() -> None:
    configs = load_all_configs(base_dir=REPO_ROOT)

    assert _find_secret_issues(configs) == []


def test_questdb_config_is_ledger_only_not_market_warehouse() -> None:
    questdb = load_all_configs(base_dir=REPO_ROOT)["questdb"]

    assert questdb["metadata"]["questdb_role"] == "bot_ledger_only"
    assert questdb["metadata"]["not_market_data_warehouse"] is True
    assert "historical_databento_market_warehouse" in questdb["forbidden_uses"]
    assert questdb["ledger_tables"] == list(ALLOWED_LEDGER_TABLES)
    assert "context_classification_attempts" in questdb["ledger_tables"]
    assert "shadow_context_policy_evaluations" in questdb["ledger_tables"]


def test_questdb_config_table_order_must_match_writer_exactly() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["questdb"]["ledger_tables"] = list(
        reversed(configs["questdb"]["ledger_tables"])
    )

    issues = _check_questdb_role(configs)

    assert (
        "QuestDB ledger_tables must exactly match writer ALLOWED_LEDGER_TABLES order"
        in issues
    )


def test_execution_defaults_to_paper_only_broker_without_live_trading() -> None:
    execution = load_all_configs(base_dir=REPO_ROOT)["execution"]

    assert execution["broker"]["name"] == "alpaca"
    assert execution["broker"]["enabled"] is True
    assert execution["broker"]["paper_trading_only"] is True
    assert execution["broker"]["live_trading_enabled"] is False
    assert execution["safety"]["allow_direct_ai_orders"] is False
    assert execution["safety"]["allow_live_trading_without_manual_config_change"] is False


def test_structured_context_sources_are_enabled_and_decision_loop_safe() -> None:
    context_sources = load_all_configs(base_dir=REPO_ROOT)["context_sources"]

    expected_enabled = {
        "eia",
        "fred",
        "macro_calendar",
        "usaspending",
        "yfinance_dev_only",
    }
    assert set(context_sources["structured_sources"]) == expected_enabled
    for source_name, source in context_sources["structured_sources"].items():
        assert source["enabled"] is True, source_name
        assert source["used_in_per_tick_loop"] is False

    yfinance = context_sources["structured_sources"]["yfinance_dev_only"]
    assert yfinance["development_only"] is True
    assert yfinance["production_critical"] is False

    for source in context_sources["unstructured_sources"].values():
        assert source["enabled"] is False
        assert source["direct_trade_authority"] is False

    ai_filter = context_sources["ai_context_filter"]
    assert ai_filter == {
        "enabled": False,
        "provider": "gemini",
        "model": "gemini-3.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "prompt_version": "context_filter_v1",
        "response_schema_version": "context_classification_response_v1",
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "retry_base_delay_seconds": 0.5,
        "retry_max_delay_seconds": 4.0,
        "max_input_characters": 12000,
        "max_prompt_characters": 30000,
        "max_summary_characters": 500,
        "max_output_tokens": 256,
        "max_provider_calls_per_minute": 6,
        "max_provider_calls_per_run": 20,
        "dedup_cache_max_entries": 256,
        "temperature": 0,
        "direct_trade_authority": False,
    }


def test_ai_context_filter_direct_trade_authority_true_is_rejected() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    ai_filter = configs["context_sources"]["ai_context_filter"]
    ai_filter["direct_trade_authority"] = True

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert "AI context filter must not have direct trade authority" in issues


def test_ai_context_filter_false_direct_trade_authority_passes() -> None:
    ai_filter = deepcopy(
        load_all_configs(base_dir=REPO_ROOT)["context_sources"]["ai_context_filter"]
    )
    issues: list[str] = []

    _check_ai_context_filter(ai_filter, issues)

    assert issues == []


def test_ai_context_filter_typed_settings_load_safe_defaults() -> None:
    settings = load_ai_context_filter_settings(base_dir=REPO_ROOT)

    assert settings.enabled is False
    assert settings.provider == "gemini"
    assert settings.model == "gemini-3.5-flash"
    assert settings.api_key_env == "GEMINI_API_KEY"
    assert settings.max_retries == 2
    assert settings.max_provider_calls_per_minute == 6
    assert settings.max_provider_calls_per_run == 20
    assert settings.dedup_cache_max_entries == 256
    assert settings.direct_trade_authority is False


def test_ai_context_filter_typed_settings_reject_trade_authority() -> None:
    settings = load_ai_context_filter_settings(base_dir=REPO_ROOT)

    with pytest.raises(
        ConfigValidationError,
        match="direct_trade_authority must be false",
    ):
        replace(settings, direct_trade_authority=True)


def test_ai_context_filter_missing_or_unsafe_resource_setting_is_rejected() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    ai_filter = configs["context_sources"]["ai_context_filter"]
    ai_filter["max_provider_calls_per_run"] = 0
    del ai_filter["max_output_tokens"]

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert "ai_context_filter max_provider_calls_per_run must be a positive integer" in issues
    assert "ai_context_filter max_output_tokens must be a positive integer" in issues


def test_enabled_structured_sources_pass_validation_when_complete() -> None:
    configs = load_all_configs(base_dir=REPO_ROOT)

    assert _check_context_sources(configs, base_dir=REPO_ROOT) == []


def test_enabled_source_with_per_tick_loop_fails_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["context_sources"]["structured_sources"]["fred"]["used_in_per_tick_loop"] = True

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert "structured source fred must not run in per-tick loop" in issues


def test_enabled_eia_without_releases_fails_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["calendar_events"]["event_windows"]["eia"]["releases"] = []

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert any("enabled EIA source requires at least one reviewed release" in issue for issue in issues)


def test_enabled_fred_without_series_ids_fails_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["context_sources"]["structured_sources"]["fred"]["series_ids"] = {}

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert any("enabled FRED source requires at least one series id" in issue for issue in issues)


def test_enabled_macro_calendar_missing_artifact_fails_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["context_sources"]["structured_sources"]["macro_calendar"]["artifact_path"] = (
        "config/missing_macro_calendar.yaml"
    )

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert any("enabled macro_calendar source artifact_path does not exist" in issue for issue in issues)


def test_enabled_yfinance_production_critical_fails_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["context_sources"]["structured_sources"]["yfinance_dev_only"]["production_critical"] = True

    issues = _check_context_sources(configs, base_dir=REPO_ROOT)

    assert "yfinance_dev_only must not be production critical" in issues


def test_live_trading_enabled_by_default_fails_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["execution"]["broker"]["live_trading_enabled"] = True

    issues = _check_trading_defaults(configs)

    assert "broker live_trading_enabled must be false" in issues


def test_committed_secret_like_values_still_fail_validation() -> None:
    configs = deepcopy(load_all_configs(base_dir=REPO_ROOT))
    configs["context_sources"]["structured_sources"]["fred"]["api_key"] = "sk_live_123456789012"

    issues = _find_secret_issues(configs)

    assert any("stores a secret-like value" in issue for issue in issues)


def test_symbol_config_separates_tradable_and_context_symbols() -> None:
    symbols = load_all_configs(base_dir=REPO_ROOT)["symbols"]
    tradable_tickers = {
        symbol["ticker"]
        for sector in symbols["tradable_universe"].values()
        for symbol in sector["symbols"]
    }
    context_tickers = {
        ticker
        for group_symbols in symbols["context_symbols"].values()
        for ticker in group_symbols
    }

    assert tradable_tickers == {
        "PLTR",
        "LMT",
        "GD",
        "RTX",
        "AVAV",
        "XOM",
        "OXY",
        "SLB",
        "COP",
        "VLO",
    }
    assert {"SPY", "QQQ", "IWM", "XLE", "PPA", "^VIX"}.issubset(context_tickers)
    assert "VIX_PROXY" not in context_tickers
    assert tradable_tickers.isdisjoint(context_tickers)

    for sector in symbols["tradable_universe"].values():
        for symbol in sector["symbols"]:
            assert symbol["approved_for_live"] is False


def test_v1_raw_market_data_table_names_are_not_present() -> None:
    assert _find_forbidden_v1_terms(REPO_ROOT) == []

    for file_name in EXPECTED_CONFIG_FILES:
        text = (REPO_ROOT / "config" / file_name).read_text(encoding="utf-8")
        assert not any(term in text for term in FORBIDDEN_V1_TERMS)
