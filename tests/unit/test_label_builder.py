from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.market_data.cost_model import (
    CostModelConfig,
    OrderStyle,
    estimate_cost_from_mid_prices,
)
from market_relay_engine.market_data.label_builder import (
    ForwardPriceObservation,
    LabelBuilderConfig,
    LabelBuilderError,
    LabelExample,
    LabelHorizon,
    build_label_for_snapshot,
    build_labels_for_snapshots,
    find_forward_price,
    horizon_to_timedelta,
    normalize_horizon,
)


NY = ZoneInfo("America/New_York")
BASE_TIME = datetime(2026, 5, 18, 10, 0, tzinfo=NY)


def _snapshot(
    local_time: datetime = BASE_TIME,
    *,
    ticker: str = "XOM",
    midprice: float | None = 100.0,
    spread: float | None = 0.02,
    spread_bps: float | None = 2.0,
    is_crossed_or_locked: bool = False,
    feature_snapshot_id: str = "feature_snapshot_label_unit_test",
    feature_version: str = "feature_v1",
) -> FeatureSnapshot:
    features = {
        "ticker": ticker,
        "spread": spread,
        "spread_bps": spread_bps,
        "is_crossed_or_locked": is_crossed_or_locked,
    }
    if midprice is not None:
        features["midprice"] = midprice
    return FeatureSnapshot(
        snapshot_time=local_time,
        ticker=ticker,
        feature_version=feature_version,
        features=features,
        source_record_count=1,
        lookback_window_seconds=60.0,
        feature_snapshot_id=feature_snapshot_id,
        trace_id="TRACE-LABEL-UNIT",
    )


def _forward(
    minutes: float,
    *,
    ticker: str = "XOM",
    midprice: float = 100.10,
    base_time: datetime = BASE_TIME,
) -> ForwardPriceObservation:
    return ForwardPriceObservation(
        event_time=base_time + timedelta(minutes=minutes),
        ticker=ticker,
        midprice=midprice,
    )


def _forward_at(
    local_time: datetime,
    *,
    ticker: str = "XOM",
    midprice: float = 100.10,
) -> ForwardPriceObservation:
    return ForwardPriceObservation(
        event_time=local_time,
        ticker=ticker,
        midprice=midprice,
    )


def _assert_json_safe(value: object) -> None:
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
    raise AssertionError(f"Unexpected non-JSON-safe type: {type(value).__name__}")


def test_label_builder_module_imports_cleanly() -> None:
    assert importlib.import_module("market_relay_engine.market_data.label_builder")


def test_default_config_values() -> None:
    config = LabelBuilderConfig()

    assert config.horizons == ("1m", "5m", "15m")
    assert config.label_version == "labels_v1"
    assert config.default_quantity == 1.0
    assert config.default_order_style == OrderStyle.MARKET.value
    assert config.max_forward_price_tolerance_seconds == 5.0
    assert config.allow_missing_forward_price is False
    assert config.market_timezone == "America/New_York"
    assert config.regular_market_open == "09:30"
    assert config.regular_market_close == "16:00"
    assert config.enforce_regular_market_hours is True


@pytest.mark.parametrize("horizon", ["1m", LabelHorizon.FIVE_MINUTES, "15m"])
def test_normalize_horizon_accepts_supported_values(
    horizon: str | LabelHorizon,
) -> None:
    assert normalize_horizon(horizon) in {"1m", "5m", "15m"}


def test_horizon_to_timedelta_maps_explicitly() -> None:
    assert horizon_to_timedelta("1m") == timedelta(minutes=1)
    assert horizon_to_timedelta(LabelHorizon.FIVE_MINUTES) == timedelta(minutes=5)
    assert horizon_to_timedelta("15m") == timedelta(minutes=15)


def test_unsupported_horizon_fails() -> None:
    with pytest.raises(LabelBuilderError, match="horizon"):
        LabelBuilderConfig(horizons=("1m", "30m"))

    with pytest.raises(LabelBuilderError, match="horizon"):
        horizon_to_timedelta("30m")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"default_quantity": 0.0}, "default_quantity"),
        ({"default_quantity": float("nan")}, "default_quantity"),
        ({"max_forward_price_tolerance_seconds": -1.0}, "tolerance"),
        ({"min_forward_price_age_seconds": -1.0}, "min_forward"),
        ({"label_version": ""}, "label_version"),
        ({"allow_missing_forward_price": "yes"}, "allow_missing_forward_price"),
        ({"enforce_regular_market_hours": "yes"}, "enforce_regular_market_hours"),
        ({"default_order_style": "STOP"}, "order_style"),
        ({"market_timezone": "Not/AZone"}, "market_timezone"),
        ({"regular_market_open": "9:30"}, "regular_market_open"),
        ({"regular_market_close": "bad"}, "regular_market_close"),
        ({"regular_market_open": "16:00", "regular_market_close": "09:30"}, "before"),
    ],
)
def test_config_rejects_invalid_values(
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(LabelBuilderError, match=match):
        LabelBuilderConfig(**kwargs)


def test_forward_price_observation_normalizes_aware_time_to_utc() -> None:
    observation = ForwardPriceObservation(
        event_time=BASE_TIME,
        ticker=" XOM ",
        midprice=100.0,
    )

    assert observation.event_time.tzinfo is UTC
    assert observation.ticker == "XOM"
    assert observation.midprice == 100.0


def test_forward_price_observation_requires_timezone_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ForwardPriceObservation(
            event_time=datetime(2026, 5, 18, 10, 0),
            ticker="XOM",
            midprice=100.0,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"ticker": ""}, "ticker"),
        ({"midprice": 0.0}, "midprice"),
        ({"midprice": float("inf")}, "midprice"),
        ({"midprice": True}, "midprice"),
    ],
)
def test_forward_price_observation_rejects_invalid_values(
    kwargs: dict[str, object],
    match: str,
) -> None:
    base_kwargs = {
        "event_time": BASE_TIME,
        "ticker": "XOM",
        "midprice": 100.0,
    }
    base_kwargs.update(kwargs)
    with pytest.raises(LabelBuilderError, match=match):
        ForwardPriceObservation(**base_kwargs)


def test_find_forward_price_exact_target_match_works() -> None:
    observation = _forward(1, midprice=100.1)

    found = find_forward_price(BASE_TIME, "XOM", [observation], "1m", 5.0)

    assert found is observation


def test_find_forward_price_uses_first_observation_after_target_within_tolerance() -> None:
    late = _forward(1 + 4 / 60, midprice=100.4)
    earliest = _forward(1 + 2 / 60, midprice=100.2)

    found = find_forward_price(BASE_TIME, "XOM", [late, earliest], "1m", 5.0)

    assert found is earliest


def test_find_forward_price_rejects_observation_before_target() -> None:
    before = ForwardPriceObservation(
        event_time=BASE_TIME + timedelta(seconds=59),
        ticker="XOM",
        midprice=100.1,
    )

    with pytest.raises(LabelBuilderError, match="No forward price"):
        find_forward_price(BASE_TIME, "XOM", [before], "1m", 5.0)


def test_find_forward_price_rejects_observation_after_tolerance() -> None:
    beyond = ForwardPriceObservation(
        event_time=BASE_TIME + timedelta(minutes=1, seconds=6),
        ticker="XOM",
        midprice=100.1,
    )

    with pytest.raises(LabelBuilderError, match="No forward price"):
        find_forward_price(BASE_TIME, "XOM", [beyond], "1m", 5.0)


def test_find_forward_price_ignores_wrong_ticker() -> None:
    wrong_ticker = _forward(1, ticker="LMT", midprice=100.1)
    right_ticker = _forward(1, ticker="XOM", midprice=100.2)

    found = find_forward_price(
        BASE_TIME,
        "XOM",
        [wrong_ticker, right_ticker],
        "1m",
        5.0,
    )

    assert found is right_ticker


def test_find_forward_price_missing_fails_clearly() -> None:
    with pytest.raises(LabelBuilderError, match="No forward price"):
        find_forward_price(BASE_TIME, "XOM", [], "1m", 5.0)


def test_near_close_target_after_regular_hours_fails() -> None:
    snapshot_time = datetime(2026, 5, 18, 15, 58, tzinfo=NY)

    with pytest.raises(LabelBuilderError, match="target_time"):
        find_forward_price(
            snapshot_time,
            "XOM",
            [_forward_at(datetime(2026, 5, 18, 16, 3, tzinfo=NY))],
            "5m",
            5.0,
        )


def test_fifteen_forty_five_snapshot_can_use_exact_close_observation() -> None:
    snapshot_time = datetime(2026, 5, 18, 15, 45, tzinfo=NY)
    close_observation = _forward_at(
        datetime(2026, 5, 18, 16, 0, tzinfo=NY),
        midprice=100.5,
    )

    found = find_forward_price(
        snapshot_time,
        "XOM",
        [close_observation],
        "15m",
        5.0,
    )

    assert found is close_observation


def test_forward_observation_after_close_is_rejected() -> None:
    snapshot_time = datetime(2026, 5, 18, 15, 59, tzinfo=NY)
    after_close = _forward_at(
        datetime(2026, 5, 18, 16, 0, 1, tzinfo=NY),
        midprice=100.5,
    )

    with pytest.raises(LabelBuilderError, match="No forward price"):
        find_forward_price(snapshot_time, "XOM", [after_close], "1m", 5.0)


def test_snapshot_before_open_is_rejected() -> None:
    snapshot_time = datetime(2026, 5, 18, 9, 29, tzinfo=NY)

    with pytest.raises(LabelBuilderError, match="snapshot_time"):
        find_forward_price(
            snapshot_time,
            "XOM",
            [_forward_at(datetime(2026, 5, 18, 9, 30, tzinfo=NY))],
            "1m",
            5.0,
        )


def test_regular_hours_enforcement_can_be_disabled() -> None:
    snapshot = _snapshot(datetime(2026, 5, 18, 15, 58, tzinfo=NY))
    forward = _forward_at(datetime(2026, 5, 18, 16, 3, tzinfo=NY), midprice=101.0)

    label = build_label_for_snapshot(
        snapshot=snapshot,
        forward_prices=[forward],
        side=SignalSide.BUY,
        horizon="5m",
        config=LabelBuilderConfig(enforce_regular_market_hours=False),
    )

    assert label.forward_event_time == forward.event_time


def test_buy_profitable_example() -> None:
    label = build_label_for_snapshot(
        _snapshot(),
        [_forward(1, midprice=100.10)],
        SignalSide.BUY,
        "1m",
    )

    assert label.expected_gross_move_bps == pytest.approx(10.0)
    assert label.profitable_after_costs is True


def test_buy_unprofitable_example() -> None:
    label = build_label_for_snapshot(
        _snapshot(),
        [_forward(1, midprice=100.04)],
        SignalSide.BUY,
        "1m",
    )

    assert label.profitable_after_costs is False


def test_sell_profitable_example() -> None:
    label = build_label_for_snapshot(
        _snapshot(),
        [_forward(1, midprice=99.90)],
        SignalSide.SELL,
        "1m",
    )

    assert label.expected_gross_move_bps > 0
    assert label.profitable_after_costs is True


def test_sell_unprofitable_example() -> None:
    label = build_label_for_snapshot(
        _snapshot(),
        [_forward(1, midprice=100.04)],
        SignalSide.SELL,
        "1m",
    )

    assert label.expected_gross_move_bps < 0
    assert label.profitable_after_costs is False


def test_label_uses_snapshot_and_forward_midprices() -> None:
    snapshot = _snapshot(midprice=123.45)
    forward = _forward(1, midprice=123.55)

    label = build_label_for_snapshot(snapshot, [forward], SignalSide.BUY, "1m")

    assert label.entry_midprice == 123.45
    assert label.forward_midprice == 123.55
    assert label.forward_event_time == forward.event_time


def test_label_includes_feature_and_cost_versions() -> None:
    snapshot = _snapshot(
        feature_snapshot_id="feature_snapshot_specific",
        feature_version="feature_v9",
    )

    label = build_label_for_snapshot(
        snapshot,
        [_forward(1, midprice=100.1)],
        SignalSide.BUY,
        "1m",
    )

    assert label.feature_snapshot_id == "feature_snapshot_specific"
    assert label.feature_version == "feature_v9"
    assert label.cost_assumptions_version == "cost_model_v1"
    assert label.label_version == "labels_v1"


def test_label_uses_pr9_cost_model_output() -> None:
    snapshot = _snapshot()
    forward = _forward(1, midprice=100.10)
    cost_config = CostModelConfig(min_edge_bps=2.0)

    label = build_label_for_snapshot(
        snapshot,
        [forward],
        SignalSide.BUY,
        "1m",
        cost_config=cost_config,
    )
    estimate = estimate_cost_from_mid_prices(
        ticker="XOM",
        side=SignalSide.BUY,
        entry_midprice=100.0,
        exit_midprice=100.10,
        horizon="1m",
        spread=0.02,
        spread_bps=2.0,
        config=cost_config,
        trace_id="TRACE-LABEL-UNIT",
    )

    assert label.expected_gross_move_bps == estimate.expected_gross_move_bps
    assert label.total_cost_bps == estimate.total_cost_bps
    assert label.net_expected_edge_bps == estimate.net_expected_edge_bps
    assert label.profitable_after_costs is estimate.profitable_after_costs


def test_non_entry_sides_are_rejected() -> None:
    with pytest.raises(LabelBuilderError, match="BUY and SELL"):
        build_label_for_snapshot(
            _snapshot(),
            [_forward(1)],
            SignalSide.HOLD,
            "1m",
        )


def test_unsupported_horizon_rejected_in_single_label() -> None:
    with pytest.raises(LabelBuilderError, match="horizon"):
        build_label_for_snapshot(
            _snapshot(),
            [_forward(1)],
            SignalSide.BUY,
            "30m",
        )


def test_missing_midprice_fails() -> None:
    with pytest.raises(LabelBuilderError, match="midprice"):
        build_label_for_snapshot(
            _snapshot(midprice=None),
            [_forward(1)],
            SignalSide.BUY,
            "1m",
        )


def test_invalid_midprice_fails() -> None:
    with pytest.raises(LabelBuilderError, match="midprice"):
        build_label_for_snapshot(
            _snapshot(midprice=0.0),
            [_forward(1)],
            SignalSide.BUY,
            "1m",
        )


def test_crossed_or_locked_snapshot_fails() -> None:
    with pytest.raises(LabelBuilderError, match="crossed or locked"):
        build_label_for_snapshot(
            _snapshot(is_crossed_or_locked=True),
            [_forward(1)],
            SignalSide.BUY,
            "1m",
        )


def test_missing_spread_uses_pr9_fallback() -> None:
    label = build_label_for_snapshot(
        _snapshot(spread=None, spread_bps=None),
        [_forward(1, midprice=100.10)],
        SignalSide.BUY,
        "1m",
    )

    assert label.reason == "fallback_minimum_spread_bps_applied"
    assert label.total_cost_bps == pytest.approx(3.0)


def test_cost_model_threshold_controls_profitability_with_strict_equality() -> None:
    label = build_label_for_snapshot(
        _snapshot(),
        [_forward(1, midprice=100.05)],
        SignalSide.BUY,
        "1m",
    )

    assert label.net_expected_edge_bps == pytest.approx(label.min_edge_bps)
    assert label.profitable_after_costs is False


def test_multiple_labels_create_all_configured_horizons_and_sides() -> None:
    snapshot = _snapshot()
    forward_prices = [
        _forward(1, midprice=100.10),
        _forward(5, midprice=100.20),
        _forward(15, midprice=100.30),
    ]

    labels = build_labels_for_snapshots([snapshot], forward_prices)

    assert len(labels) == 6
    assert {label.horizon for label in labels} == {"1m", "5m", "15m"}
    assert {label.side for label in labels} == {SignalSide.BUY, SignalSide.SELL}


def test_missing_forward_price_fails_by_default_in_multiple_labels() -> None:
    with pytest.raises(LabelBuilderError, match="No forward price"):
        build_labels_for_snapshots([_snapshot()], [])


def test_missing_forward_price_skips_when_allowed() -> None:
    labels = build_labels_for_snapshots(
        [_snapshot()],
        [],
        config=LabelBuilderConfig(allow_missing_forward_price=True),
    )

    assert labels == []


def test_regular_hours_invalid_label_skips_when_allowed_in_multiple_labels() -> None:
    labels = build_labels_for_snapshots(
        [_snapshot(datetime(2026, 5, 18, 15, 58, tzinfo=NY))],
        [_forward_at(datetime(2026, 5, 18, 16, 3, tzinfo=NY))],
        config=LabelBuilderConfig(
            horizons=("5m",),
            allow_missing_forward_price=True,
        ),
    )

    assert labels == []


def test_forward_selection_does_not_use_observation_before_target_horizon() -> None:
    before_target = ForwardPriceObservation(
        event_time=BASE_TIME + timedelta(seconds=59),
        ticker="XOM",
        midprice=999.0,
    )
    valid_target = ForwardPriceObservation(
        event_time=BASE_TIME + timedelta(seconds=60),
        ticker="XOM",
        midprice=100.1,
    )

    label = build_label_for_snapshot(
        _snapshot(),
        [before_target, valid_target],
        SignalSide.BUY,
        "1m",
    )

    assert label.forward_midprice == 100.1


def test_label_builder_does_not_modify_feature_snapshot_features() -> None:
    snapshot = _snapshot()
    original_features = dict(snapshot.features)

    build_label_for_snapshot(snapshot, [_forward(1)], SignalSide.BUY, "1m")

    assert snapshot.features == original_features


def test_label_example_serializes_to_json_safe_values() -> None:
    label = build_label_for_snapshot(
        _snapshot(),
        [_forward(1, midprice=100.10)],
        SignalSide.BUY,
        "1m",
    )

    parsed = from_json_string(to_json_string(label))

    assert parsed["side"] == "BUY"
    assert parsed["snapshot_time"].endswith("Z")
    assert parsed["forward_event_time"].endswith("Z")
    _assert_json_safe(parsed)


def test_label_example_rejects_non_finite_values() -> None:
    with pytest.raises(LabelBuilderError, match="expected_gross_move_bps"):
        LabelExample(
            snapshot_time=BASE_TIME,
            ticker="XOM",
            horizon="1m",
            side=SignalSide.BUY,
            entry_midprice=100.0,
            forward_event_time=BASE_TIME + timedelta(minutes=1),
            forward_midprice=100.1,
            expected_gross_move_bps=float("nan"),
            net_expected_edge_bps=1.0,
            total_cost_bps=1.0,
            min_edge_bps=1.0,
            profitable_after_costs=False,
            cost_assumptions_version="cost_model_v1",
            label_version="labels_v1",
            feature_snapshot_id="feature_snapshot_id",
            feature_version="feature_v1",
        )


def test_label_builder_module_avoids_forbidden_imports() -> None:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "market_relay_engine"
        / "market_data"
        / "label_builder.py"
    )
    source = module_path.read_text(encoding="utf-8").lower()

    for forbidden in (
        "databento",
        "questdb",
        "alpaca",
        "pandas",
        "numpy",
        "sklearn",
        "torch",
        "requests",
        "httpx",
    ):
        assert forbidden not in source
