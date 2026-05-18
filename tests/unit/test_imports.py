import importlib


def test_package_imports() -> None:
    assert importlib.import_module("market_relay_engine")


def test_common_utilities_import() -> None:
    assert importlib.import_module("market_relay_engine.common.time")
    assert importlib.import_module("market_relay_engine.common.logging")
    assert importlib.import_module("market_relay_engine.common.config")
    assert importlib.import_module("market_relay_engine.common.ids")
    assert importlib.import_module("market_relay_engine.common.serialization")


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
    ):
        monkeypatch.delenv(key, raising=False)

    assert importlib.import_module("market_relay_engine.common.time")
    assert importlib.import_module("market_relay_engine.contracts")
