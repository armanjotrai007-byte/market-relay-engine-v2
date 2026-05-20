from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import importlib
import math
from pathlib import Path
from typing import Any

import pytest

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.market import MarketRecord
from market_relay_engine.market_data.feature_builder import FeatureBuilderConfig
from market_relay_engine.market_data.feature_parity import (
    FeatureParityError,
    assert_event_time_ordered,
    assert_feature_snapshots_equivalent,
    build_historical_style_snapshot,
    build_live_style_snapshot,
    feature_snapshot_semantic_dict,
)


BASE_TIME = datetime(2026, 5, 18, 13, 30, tzinfo=UTC)
TRACE_ID = "TRACE-PR8-STABLE"


def _record(
    seconds: int,
    *,
    ticker: str = "XOM",
    record_type: str = "trade",
    price: float | None = None,
    size: float | None = None,
    bid_price: float | None = None,
    ask_price: float | None = None,
    bid_size: float | None = None,
    ask_size: float | None = None,
    trace_id: str | None = TRACE_ID,
) -> MarketRecord:
    return MarketRecord(
        event_time=BASE_TIME + timedelta(seconds=seconds),
        ticker=ticker,
        source="feature_parity_unit_test",
        record_type=record_type,
        price=price,
        size=size,
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=bid_size,
        ask_size=ask_size,
        trace_id=trace_id,
    )


def _trade(seconds: int, price: float, size: float = 10.0) -> MarketRecord:
    return _record(seconds, record_type="trade", price=price, size=size)


def _quote(seconds: int, bid_price: float, ask_price: float) -> MarketRecord:
    return _record(
        seconds,
        record_type="quote",
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=500.0,
        ask_size=400.0,
    )


def _assert_json_safe(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        assert math.isfinite(value)
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            _assert_json_safe(item)
        return
    raise AssertionError(f"Unexpected non-JSON-safe value: {type(value).__name__}")


def _assert_ordered_parity(
    records: list[MarketRecord],
    config: FeatureBuilderConfig | None = None,
) -> None:
    assert_event_time_ordered(records)
    historical = build_historical_style_snapshot(records, config=config, trace_id=TRACE_ID)
    live = build_live_style_snapshot(records, config=config, trace_id=TRACE_ID)
    assert_feature_snapshots_equivalent(historical, live)


def test_feature_parity_module_imports_cleanly() -> None:
    assert importlib.import_module("market_relay_engine.market_data.feature_parity")


def test_historical_style_helper_rejects_empty_input() -> None:
    with pytest.raises(FeatureParityError, match="at least one MarketRecord"):
        build_historical_style_snapshot([])


def test_live_style_helper_rejects_empty_input() -> None:
    with pytest.raises(FeatureParityError, match="at least one MarketRecord"):
        build_live_style_snapshot([])


def test_historical_style_helper_rejects_multiple_tickers() -> None:
    with pytest.raises(FeatureParityError, match="exactly one ticker"):
        build_historical_style_snapshot(
            [
                _trade(1, 100.0),
                _record(2, ticker="LMT", record_type="trade", price=200.0, size=1.0),
            ]
        )


def test_live_style_helper_rejects_multiple_tickers() -> None:
    with pytest.raises(FeatureParityError, match="exactly one ticker"):
        build_live_style_snapshot(
            [
                _trade(1, 100.0),
                _record(2, ticker="LMT", record_type="trade", price=200.0, size=1.0),
            ]
        )


def test_historical_style_helper_sorts_by_event_time() -> None:
    snapshot = build_historical_style_snapshot(
        [
            _trade(3, 103.0),
            _trade(1, 101.0),
        ]
    )

    assert snapshot.features["last_trade_price"] == 103.0
    assert snapshot.features["price_return_window"] == pytest.approx(103.0 / 101.0 - 1)


def test_historical_sorting_is_stable_for_equal_timestamps() -> None:
    records = [
        _trade(10, 100.0),
        _trade(10, 105.0),
    ]

    snapshot = build_historical_style_snapshot(records)

    assert snapshot.features["last_trade_price"] == 105.0
    assert snapshot.features["price_return_window"] == pytest.approx(0.05)


def test_live_style_helper_accepts_out_of_order_inputs_and_processes_caller_order() -> None:
    snapshot = build_live_style_snapshot(
        [
            _trade(2, 102.0),
            _trade(1, 101.0),
        ]
    )

    assert snapshot.snapshot_time == BASE_TIME + timedelta(seconds=2)
    assert snapshot.features["last_trade_price"] == 101.0


def test_out_of_order_live_input_is_not_a_formal_parity_case() -> None:
    records = [
        _trade(2, 102.0),
        _trade(1, 101.0),
    ]

    with pytest.raises(FeatureParityError, match="non-decreasing event_time order"):
        assert_event_time_ordered(records)

    historical = build_historical_style_snapshot(records)
    live = build_live_style_snapshot(records)
    assert historical.features["last_trade_price"] == 102.0
    assert live.features["last_trade_price"] == 101.0


def test_event_time_ordered_trade_sequence_produces_parity() -> None:
    _assert_ordered_parity(
        [
            _trade(1, 100.0, size=10.0),
            _trade(2, 101.0, size=15.0),
            _trade(3, 102.0, size=5.0),
        ]
    )


def test_quote_only_sequence_produces_parity() -> None:
    _assert_ordered_parity(
        [
            _quote(1, 99.0, 101.0),
            _quote(2, 100.0, 102.0),
            _quote(3, 101.0, 103.0),
        ]
    )


def test_trade_only_sequence_produces_parity() -> None:
    _assert_ordered_parity(
        [
            _trade(1, 100.0, size=1.0),
            _trade(2, 100.5, size=2.0),
            _trade(3, 101.0, size=3.0),
        ]
    )


def test_mixed_trade_quote_sequence_produces_parity() -> None:
    _assert_ordered_parity(
        [
            _trade(1, 100.0, size=10.0),
            _quote(2, 99.8, 100.2),
            _trade(3, 100.5, size=15.0),
            _quote(4, 100.3, 100.7),
        ]
    )


def test_rolling_window_pruning_case_produces_parity() -> None:
    config = FeatureBuilderConfig(lookback_window_seconds=10)
    records = [
        _trade(0, 100.0, size=10.0),
        _trade(5, 101.0, size=15.0),
        _trade(11, 102.0, size=20.0),
    ]

    _assert_ordered_parity(records, config=config)

    historical = build_historical_style_snapshot(records, config=config)
    assert historical.source_record_count == 2
    assert historical.features["volume_window"] == 35.0


def test_same_timestamp_quote_and_trade_produce_parity_when_input_order_matches() -> None:
    records = [
        _quote(10, 99.5, 100.5),
        _trade(10, 100.25, size=25.0),
    ]

    _assert_ordered_parity(records)

    historical = build_historical_style_snapshot(records)
    live = build_live_style_snapshot(records)
    assert historical.snapshot_time == BASE_TIME + timedelta(seconds=10)
    assert_feature_snapshots_equivalent(historical, live)


def test_source_record_count_and_feature_keys_match_for_ordered_parity() -> None:
    records = [_trade(1, 100.0), _quote(2, 99.5, 100.5)]

    historical = build_historical_style_snapshot(records)
    live = build_live_style_snapshot(records)

    assert historical.source_record_count == live.source_record_count == 2
    assert set(historical.features) == set(live.features)
    assert_feature_snapshots_equivalent(historical, live)


def test_float_comparison_accepts_tiny_numeric_differences() -> None:
    snapshot = build_historical_style_snapshot([_quote(1, 99.0, 101.0)])
    features = dict(snapshot.features)
    features["spread_bps"] = features["spread_bps"] + 1e-13
    slightly_different = replace(snapshot, features=features)

    assert_feature_snapshots_equivalent(snapshot, slightly_different)


def test_semantic_comparison_ignores_generated_feature_snapshot_id() -> None:
    left = build_historical_style_snapshot([_trade(1, 100.0)])
    right = replace(left, feature_snapshot_id="feature_snapshot_different")

    assert left.feature_snapshot_id != right.feature_snapshot_id
    assert_feature_snapshots_equivalent(left, right)


def test_semantic_comparison_catches_feature_value_differences() -> None:
    snapshot = build_historical_style_snapshot([_trade(1, 100.0)])
    features = dict(snapshot.features)
    features["last_trade_price"] = 101.0
    changed = replace(snapshot, features=features)

    with pytest.raises(FeatureParityError, match="features.last_trade_price differs"):
        assert_feature_snapshots_equivalent(snapshot, changed)


def test_semantic_comparison_catches_missing_feature_keys() -> None:
    snapshot = build_historical_style_snapshot([_trade(1, 100.0)])
    features = dict(snapshot.features)
    features.pop("last_trade_price")
    changed = replace(snapshot, features=features)

    with pytest.raises(FeatureParityError, match="Feature keys differ"):
        assert_feature_snapshots_equivalent(snapshot, changed)


def test_snapshot_time_is_compared_for_deterministic_inputs() -> None:
    snapshot = build_historical_style_snapshot([_trade(1, 100.0)])
    changed = replace(snapshot, snapshot_time=snapshot.snapshot_time + timedelta(seconds=1))

    with pytest.raises(FeatureParityError, match="snapshot_time differs"):
        assert_feature_snapshots_equivalent(snapshot, changed)


def test_semantic_comparison_rejects_non_finite_float_values() -> None:
    snapshot = build_historical_style_snapshot([_quote(1, 99.0, 101.0)])
    features = dict(snapshot.features)
    features["spread_bps"] = float("inf")
    changed = replace(snapshot, features=features)

    with pytest.raises(FeatureParityError, match="NaN or Infinity"):
        assert_feature_snapshots_equivalent(snapshot, changed)


def test_snapshots_serialize_through_json_helpers() -> None:
    snapshot = build_historical_style_snapshot([_quote(1, 99.0, 101.0)])
    parsed = from_json_string(to_json_string(snapshot))

    assert parsed["ticker"] == "XOM"
    assert parsed["snapshot_time"].endswith("Z")


def test_no_nan_or_infinity_in_features() -> None:
    snapshot = build_live_style_snapshot(
        [
            _trade(1, float("nan"), size=float("inf")),
            _quote(2, bid_price=float("inf"), ask_price=float("-inf")),
        ]
    )

    _assert_json_safe(snapshot.features)


def test_feature_snapshot_semantic_dict_is_testing_support_and_excludes_ids() -> None:
    snapshot = build_historical_style_snapshot([_trade(1, 100.0)])
    semantic = feature_snapshot_semantic_dict(snapshot)

    assert "feature_snapshot_id" not in semantic
    assert semantic["snapshot_time"] == snapshot.snapshot_time

    docs_path = Path(__file__).resolve().parents[2] / "docs" / "feature_parity.md"
    docs_text = docs_path.read_text(encoding="utf-8")
    assert "testing support, not a stable production API" in docs_text


def test_feature_parity_module_avoids_external_service_imports() -> None:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "market_relay_engine"
        / "market_data"
        / "feature_parity.py"
    )
    source = module_path.read_text(encoding="utf-8").lower()

    for forbidden in ("databento", "questdb", "alpaca", "requests", "httpx"):
        assert forbidden not in source
