from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
import importlib
import math
from pathlib import Path
from typing import Any

import pytest

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.market import MarketRecord
from market_relay_engine.market_data.feature_builder import (
    FeatureBuilder,
    FeatureBuilderConfig,
    FeatureBuilderError,
    V1_FEATURE_KEYS,
    build_feature_snapshot,
)
from tests.fixtures.market_records import (
    make_market_quote_record,
    make_market_trade_record,
)


BASE_TIME = datetime(2026, 5, 18, 13, 30, tzinfo=UTC)


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
    spread: float | None = None,
    midprice: float | None = None,
) -> MarketRecord:
    return MarketRecord(
        event_time=BASE_TIME + timedelta(seconds=seconds),
        ticker=ticker,
        source="feature_builder_unit_test",
        record_type=record_type,
        price=price,
        size=size,
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=bid_size,
        ask_size=ask_size,
        spread=spread,
        midprice=midprice,
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


def test_feature_builder_module_imports_cleanly() -> None:
    assert importlib.import_module("market_relay_engine.market_data.feature_builder")


def test_empty_input_fails_clearly() -> None:
    with pytest.raises(FeatureBuilderError, match="empty records"):
        build_feature_snapshot([])


def test_feature_snapshot_contract_remains_feature_dict_based() -> None:
    field_names = {field.name for field in fields(FeatureSnapshot)}

    assert "features" in field_names
    assert "midprice" not in field_names
    assert "spread" not in field_names
    assert "record_count_window" not in field_names


def test_v1_feature_dictionary_key_set_is_exact() -> None:
    snapshot = build_feature_snapshot([make_market_trade_record()])

    assert set(snapshot.features) == V1_FEATURE_KEYS


def test_feature_builder_config_default_cap_is_50000() -> None:
    assert FeatureBuilderConfig().max_records_per_ticker == 50000


def test_single_quote_record_computes_midprice_spread_and_bps() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(
                1,
                record_type="quote",
                bid_price=100.0,
                ask_price=102.0,
                bid_size=10.0,
                ask_size=20.0,
            )
        ]
    )

    assert snapshot.features["midprice"] == 101.0
    assert snapshot.features["spread"] == 2.0
    assert snapshot.features["spread_bps"] == pytest.approx(198.01980198019803)
    assert snapshot.features["quote_count_window"] == 1
    assert snapshot.features["last_bid_size"] == 10.0
    assert snapshot.features["last_ask_size"] == 20.0


def test_single_trade_record_produces_trade_features() -> None:
    snapshot = build_feature_snapshot(
        [_record(1, record_type="trade", price=100.5, size=25.0)]
    )

    assert snapshot.features["trade_count_window"] == 1
    assert snapshot.features["last_price"] == 100.5
    assert snapshot.features["last_trade_price"] == 100.5
    assert snapshot.features["last_trade_size"] == 25.0
    assert snapshot.features["volume_window"] == 25.0


def test_last_trade_size_tracks_latest_finite_trade_size_independently() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(1, record_type="trade", price=100.0, size=25.0),
            _record(2, record_type="trade", price=None, size=30.0),
        ]
    )

    assert snapshot.features["last_trade_price"] == 100.0
    assert snapshot.features["last_trade_size"] == 30.0


def test_record_midprice_and_spread_values_take_precedence() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(
                1,
                record_type="quote",
                bid_price=100.0,
                ask_price=102.0,
                midprice=100.25,
                spread=0.5,
            )
        ]
    )

    assert snapshot.features["midprice"] == 100.25
    assert snapshot.features["spread"] == 0.5


def test_spread_bps_is_none_when_midprice_is_zero_or_missing() -> None:
    zero_mid_snapshot = build_feature_snapshot(
        [_record(1, record_type="quote", bid_price=-1.0, ask_price=1.0)]
    )
    missing_mid_snapshot = build_feature_snapshot(
        [_record(1, record_type="quote", bid_price=100.0)]
    )

    assert zero_mid_snapshot.features["midprice"] == 0.0
    assert zero_mid_snapshot.features["spread_bps"] is None
    assert missing_mid_snapshot.features["midprice"] is None
    assert missing_mid_snapshot.features["spread_bps"] is None


def test_crossed_or_locked_quote_flag_works() -> None:
    locked = build_feature_snapshot(
        [_record(1, record_type="quote", bid_price=100.0, ask_price=100.0)]
    )
    crossed = build_feature_snapshot(
        [_record(1, record_type="quote", bid_price=101.0, ask_price=100.0)]
    )
    normal = build_feature_snapshot(
        [_record(1, record_type="quote", bid_price=99.0, ask_price=100.0)]
    )

    assert locked.features["is_crossed_or_locked"] is True
    assert crossed.features["is_crossed_or_locked"] is True
    assert normal.features["is_crossed_or_locked"] is False


def test_rolling_window_prunes_old_records() -> None:
    builder = FeatureBuilder(FeatureBuilderConfig(lookback_window_seconds=10))
    builder.update(_record(0, price=100.0, size=1.0))
    snapshot = builder.update(_record(11, price=101.0, size=2.0))

    assert snapshot.features["record_count_window"] == 1
    assert snapshot.features["volume_window"] == 2.0
    assert snapshot.features["last_trade_price"] == 101.0


def test_max_event_time_seen_prevents_pruning_from_moving_backward() -> None:
    builder = FeatureBuilder(FeatureBuilderConfig(lookback_window_seconds=10))
    builder.update(_record(100, price=100.0, size=1.0))
    delayed_snapshot = builder.update(_record(95, price=95.0, size=1.0))
    stale_snapshot = builder.update(_record(89, price=89.0, size=100.0))

    assert delayed_snapshot.snapshot_time == BASE_TIME + timedelta(seconds=100)
    assert delayed_snapshot.features["record_count_window"] == 2
    assert stale_snapshot.snapshot_time == BASE_TIME + timedelta(seconds=100)
    assert stale_snapshot.features["record_count_window"] == 2
    assert stale_snapshot.features["last_trade_price"] == 95.0
    assert stale_snapshot.features["volume_window"] == 2.0


def test_time_pruning_happens_before_record_cap() -> None:
    builder = FeatureBuilder(
        FeatureBuilderConfig(lookback_window_seconds=10, max_records_per_ticker=2)
    )
    builder.update(_record(100, price=100.0, size=1.0))
    builder.update(_record(95, price=95.0, size=1.0))
    snapshot = builder.update(_record(0, price=0.0, size=100.0))

    assert snapshot.features["record_count_window"] == 2
    assert snapshot.features["volume_window"] == 2.0
    assert snapshot.features["last_trade_price"] == 95.0


def test_cap_applies_after_time_window_with_small_custom_cap() -> None:
    builder = FeatureBuilder(
        FeatureBuilderConfig(lookback_window_seconds=60, max_records_per_ticker=2)
    )
    builder.update(_record(10, price=10.0, size=10.0))
    builder.update(_record(11, price=11.0, size=11.0))
    snapshot = builder.update(_record(12, price=12.0, size=12.0))

    assert snapshot.features["record_count_window"] == 2
    assert snapshot.features["volume_window"] == 23.0
    assert snapshot.features["price_return_window"] == pytest.approx(12.0 / 11.0 - 1)


def test_window_volume_and_price_return_work() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(1, price=100.0, size=10.0),
            _record(2, price=110.0, size=15.0),
        ]
    )

    assert snapshot.features["volume_window"] == 25.0
    assert snapshot.features["price_return_window"] == pytest.approx(0.1)


def test_midprice_return_and_previous_change_work() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(1, record_type="quote", bid_price=99.0, ask_price=101.0),
            _record(2, record_type="quote", bid_price=109.0, ask_price=111.0),
        ]
    )

    assert snapshot.features["midprice_return_window"] == pytest.approx(0.1)
    assert snapshot.features["midprice_change_from_previous"] == 10.0


def test_simple_volatility_is_none_for_insufficient_data() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(1, record_type="quote", bid_price=99.0, ask_price=101.0),
            _record(2, record_type="quote", bid_price=100.0, ask_price=102.0),
        ]
    )

    assert snapshot.features["simple_volatility_window"] is None


def test_simple_volatility_is_finite_when_enough_data_exists() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(1, record_type="quote", bid_price=99.0, ask_price=101.0),
            _record(2, record_type="quote", bid_price=100.0, ask_price=102.0),
            _record(3, record_type="quote", bid_price=102.0, ask_price=104.0),
        ]
    )

    volatility = snapshot.features["simple_volatility_window"]
    assert isinstance(volatility, float)
    assert math.isfinite(volatility)


def test_non_finite_inputs_do_not_produce_non_finite_features() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(1, record_type="trade", price=float("nan"), size=float("inf")),
            _record(
                2,
                record_type="quote",
                bid_price=float("inf"),
                ask_price=float("-inf"),
                midprice=float("nan"),
                spread=float("inf"),
            ),
        ]
    )

    _assert_json_safe(snapshot.features)
    assert snapshot.features["last_trade_price"] is None
    assert snapshot.features["midprice"] is None
    assert snapshot.features["spread"] is None


def test_feature_snapshot_serializes_to_json_safe_string() -> None:
    snapshot = build_feature_snapshot([make_market_quote_record()])
    parsed = from_json_string(to_json_string(snapshot))

    assert parsed["features"]["ticker"] == "XOM"
    assert parsed["snapshot_time"].endswith("Z")


def test_deterministic_output_for_same_ordered_inputs() -> None:
    records = [
        make_market_trade_record(price=100.0, size=10.0, index=1),
        make_market_quote_record(bid_price=100.0, ask_price=101.0, index=2),
    ]

    first = build_feature_snapshot(records)
    second = build_feature_snapshot(records)

    assert first.features == second.features
    assert first.snapshot_time == second.snapshot_time
    assert first.source_record_count == second.source_record_count
    assert first.feature_version == second.feature_version


def test_convenience_function_sorts_records_by_event_time() -> None:
    snapshot = build_feature_snapshot(
        [
            _record(3, price=103.0, size=1.0),
            _record(1, price=101.0, size=1.0),
        ]
    )

    assert snapshot.features["last_trade_price"] == 103.0
    assert snapshot.features["price_return_window"] == pytest.approx(103.0 / 101.0 - 1)


def test_stateful_update_processes_caller_order_without_sorting() -> None:
    builder = FeatureBuilder()
    builder.update(_record(3, price=103.0, size=1.0))
    snapshot = builder.update(_record(1, price=101.0, size=1.0))

    assert snapshot.snapshot_time == BASE_TIME + timedelta(seconds=3)
    assert snapshot.features["last_trade_price"] == 101.0


def test_stateful_update_does_not_require_optional_timestamps() -> None:
    snapshot = FeatureBuilder().update(_record(1, price=100.0, size=1.0))

    assert snapshot.features["last_trade_price"] == 100.0


def test_unrecognized_record_type_only_contributes_to_record_count() -> None:
    builder = FeatureBuilder()
    builder.update(_record(1, record_type="trade", price=100.0, size=5.0))
    snapshot = builder.update(_record(2, record_type="status"))

    assert snapshot.features["record_count_window"] == 2
    assert snapshot.features["trade_count_window"] == 1
    assert snapshot.features["quote_count_window"] == 0
    assert snapshot.features["last_trade_price"] == 100.0
    assert snapshot.features["midprice"] is None
    assert snapshot.features["spread"] is None


def test_multiple_tickers_fail_clearly() -> None:
    with pytest.raises(FeatureBuilderError, match="exactly one ticker"):
        build_feature_snapshot(
            [
                _record(1, ticker="XOM", price=100.0, size=1.0),
                _record(2, ticker="LMT", price=200.0, size=1.0),
            ]
        )


def test_pr8_handoff_note_documents_parity_ordering_requirement() -> None:
    handoff_path = Path(__file__).resolve().parents[2] / "handoff.md"

    assert "batch sorting vs live arrival order" in handoff_path.read_text(encoding="utf-8")
