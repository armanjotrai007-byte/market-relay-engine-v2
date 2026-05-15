from pathlib import Path

from market_relay_engine.common.config import EXPECTED_CONFIG_FILES, load_all_configs
from scripts.check_config import (
    FORBIDDEN_V1_TERMS,
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
    assert "model_signals" in questdb["ledger_tables"]


def test_execution_defaults_to_paper_only_disabled_broker() -> None:
    execution = load_all_configs(base_dir=REPO_ROOT)["execution"]

    assert execution["broker"]["name"] == "alpaca"
    assert execution["broker"]["enabled"] is False
    assert execution["broker"]["paper_trading_only"] is True
    assert execution["broker"]["live_trading_enabled"] is False
    assert execution["safety"]["allow_direct_ai_orders"] is False
    assert execution["safety"]["allow_live_trading_without_manual_config_change"] is False


def test_context_sources_are_disabled_or_development_safe_by_default() -> None:
    context_sources = load_all_configs(base_dir=REPO_ROOT)["context_sources"]

    for source in context_sources["structured_sources"].values():
        assert source["enabled"] is False
        assert source["used_in_per_tick_loop"] is False

    yfinance = context_sources["structured_sources"]["yfinance_dev_only"]
    assert yfinance["development_only"] is True
    assert yfinance["production_critical"] is False

    for source in context_sources["unstructured_sources"].values():
        assert source["enabled"] is False
        assert source["direct_trade_authority"] is False

    assert context_sources["ai_context_filter"]["enabled"] is False
    assert context_sources["ai_context_filter"]["direct_trade_authority"] is False


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

    assert {"XOM", "CVX", "LMT", "RTX", "NOC", "GD"}.issubset(tradable_tickers)
    assert {"SPY", "QQQ", "IWM", "XLE", "PPA", "VIX_PROXY"}.issubset(context_tickers)
    assert tradable_tickers.isdisjoint(context_tickers)

    for sector in symbols["tradable_universe"].values():
        for symbol in sector["symbols"]:
            assert symbol["approved_for_live"] is False


def test_v1_raw_market_data_table_names_are_not_present() -> None:
    assert _find_forbidden_v1_terms(REPO_ROOT) == []

    for file_name in EXPECTED_CONFIG_FILES:
        text = (REPO_ROOT / "config" / file_name).read_text(encoding="utf-8")
        assert not any(term in text for term in FORBIDDEN_V1_TERMS)
