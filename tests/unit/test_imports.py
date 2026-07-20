import importlib


def test_package_imports() -> None:
    assert importlib.import_module("market_relay_engine")


def test_common_utilities_import() -> None:
    assert importlib.import_module("market_relay_engine.common.time")
    assert importlib.import_module("market_relay_engine.common.logging")
    assert importlib.import_module("market_relay_engine.common.config")
    assert importlib.import_module("market_relay_engine.common.ids")
    assert importlib.import_module("market_relay_engine.common.serialization")


def test_questdb_health_import() -> None:
    assert importlib.import_module("market_relay_engine.questdb.health")
    assert importlib.import_module("market_relay_engine.questdb.writer")
    assert importlib.import_module("market_relay_engine.questdb.analysis")
    assert importlib.import_module("market_relay_engine.questdb.jsonl_fallback")


def test_risk_filter_import() -> None:
    assert importlib.import_module("market_relay_engine.risk")
    assert importlib.import_module("market_relay_engine.risk.risk_filter")
    assert importlib.import_module("market_relay_engine.risk.rules")
    assert importlib.import_module("market_relay_engine.risk.decisions")


def test_execution_modules_import() -> None:
    assert importlib.import_module("market_relay_engine.execution.order_manager")
    assert importlib.import_module("market_relay_engine.execution.position_state")
    assert importlib.import_module("market_relay_engine.execution.alpaca_paper")
    assert importlib.import_module("market_relay_engine.execution.execution_metrics")


def test_external_event_modules_and_dependencies_import() -> None:
    for module_name in (
        "market_relay_engine.context.external_classification",
        "market_relay_engine.context.external_event_archive",
        "market_relay_engine.context.external_normalization",
        "market_relay_engine.context.external_source_config",
        "market_relay_engine.context.external_sources",
        "market_relay_engine.context.veritawire",
        "websockets",
        "bs4",
        "pypdf",
    ):
        assert importlib.import_module(module_name)


def test_contract_modules_import() -> None:
    assert importlib.import_module("market_relay_engine.contracts")
    assert importlib.import_module("market_relay_engine.contracts.base")
    assert importlib.import_module("market_relay_engine.contracts.market")
    assert importlib.import_module("market_relay_engine.contracts.features")
    assert importlib.import_module("market_relay_engine.contracts.model")
    assert importlib.import_module("market_relay_engine.contracts.risk")
    assert importlib.import_module("market_relay_engine.contracts.context")
    assert importlib.import_module("market_relay_engine.contracts.execution")
    assert importlib.import_module("market_relay_engine.contracts.ledger")
    assert importlib.import_module("market_relay_engine.contracts.system")


def test_imports_do_not_require_external_service_keys(monkeypatch) -> None:
    for key in (
        "DATABENTO_API_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "EIA_API_KEY",
        "FRED_API_KEY",
        "GEMINI_API_KEY",
        "VERITAWIRE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    assert importlib.import_module("market_relay_engine.common.time")
    assert importlib.import_module("market_relay_engine.contracts")
    assert importlib.import_module("market_relay_engine.execution.alpaca_paper")
    assert importlib.import_module("market_relay_engine.execution.execution_metrics")
    assert importlib.import_module("market_relay_engine.context.external_sources")
    assert importlib.import_module("market_relay_engine.context.veritawire")
