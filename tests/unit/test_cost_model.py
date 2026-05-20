from __future__ import annotations

import importlib
import math
from pathlib import Path

import pytest

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.market_data.cost_model import (
    CostEstimate,
    CostModelConfig,
    CostModelError,
    OrderStyle,
    estimate_cost_from_expected_move,
    estimate_cost_from_mid_prices,
    exceeds_min_edge_threshold,
)


def test_cost_model_module_imports_cleanly() -> None:
    assert importlib.import_module("market_relay_engine.market_data.cost_model")


def test_default_config_values() -> None:
    config = CostModelConfig()

    assert config.min_edge_bps == 1.0
    assert config.round_trip_slippage_per_share == 0.02
    assert config.market_order_spread_multiplier == 1.0
    assert config.limit_order_spread_multiplier == 0.0
    assert config.limit_order_missed_fill_probability_by_horizon == {
        "1m": 0.30,
        "5m": 0.20,
        "15m": 0.10,
    }
    assert config.size_penalty_bps_per_1000_shares == 0.25
    assert config.size_penalty_free_quantity == 100.0
    assert config.fallback_minimum_spread_bps == 1.0
    assert config.assumptions_version == "cost_model_v1"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("min_edge_bps", -1.0),
        ("round_trip_slippage_per_share", -0.01),
        ("market_order_spread_multiplier", -1.0),
        ("limit_order_spread_multiplier", -1.0),
        ("size_penalty_bps_per_1000_shares", -0.25),
        ("size_penalty_free_quantity", -100.0),
        ("fallback_minimum_spread_bps", -1.0),
    ],
)
def test_config_rejects_negative_numbers(field_name: str, value: float) -> None:
    with pytest.raises(CostModelError, match=field_name):
        CostModelConfig(**{field_name: value})


def test_config_rejects_invalid_probability_mapping() -> None:
    with pytest.raises(CostModelError, match="1m, 5m, and 15m"):
        CostModelConfig(limit_order_missed_fill_probability_by_horizon={"1m": 0.3})

    with pytest.raises(CostModelError, match="between 0 and 1"):
        CostModelConfig(
            limit_order_missed_fill_probability_by_horizon={
                "1m": 0.30,
                "5m": 1.20,
                "15m": 0.10,
            }
        )


def test_config_rejects_non_finite_and_empty_version() -> None:
    with pytest.raises(CostModelError, match="round_trip_slippage_per_share"):
        CostModelConfig(round_trip_slippage_per_share=float("nan"))

    with pytest.raises(CostModelError, match="assumptions_version"):
        CostModelConfig(assumptions_version="")


@pytest.mark.parametrize("horizon", ["1m", "5m", "15m"])
def test_supported_horizons_are_accepted(horizon: str) -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=5.0,
        horizon=horizon,
        midprice=100.0,
        spread_bps=1.0,
    )

    assert estimate.horizon == horizon


def test_unsupported_horizon_is_rejected() -> None:
    with pytest.raises(CostModelError, match="horizon"):
        estimate_cost_from_expected_move(
            ticker="XOM",
            side=SignalSide.BUY,
            expected_gross_move_bps=5.0,
            horizon="30m",
            midprice=100.0,
            spread_bps=1.0,
        )


@pytest.mark.parametrize("side", [SignalSide.BUY, SignalSide.SELL])
def test_buy_and_sell_sides_are_supported(side: SignalSide) -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=side,
        expected_gross_move_bps=5.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=1.0,
    )

    assert estimate.side is side


@pytest.mark.parametrize(
    "side",
    [SignalSide.HOLD, SignalSide.EXIT, SignalSide.DO_NOTHING],
)
def test_non_entry_sides_are_rejected(side: SignalSide) -> None:
    with pytest.raises(CostModelError, match="BUY and SELL"):
        estimate_cost_from_expected_move(
            ticker="XOM",
            side=side,
            expected_gross_move_bps=5.0,
            horizon="1m",
            midprice=100.0,
            spread_bps=1.0,
        )


def test_market_order_applies_spread_and_round_trip_slippage() -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        order_style=OrderStyle.MARKET.value,
        quantity=100.0,
    )

    assert estimate.spread_cost_bps == 2.0
    assert estimate.estimated_slippage_bps == pytest.approx(2.0)
    assert estimate.size_penalty_bps == 0.0
    assert estimate.base_cost_bps == pytest.approx(4.0)
    assert estimate.missed_fill_probability == 0.0
    assert estimate.missed_fill_penalty_bps == 0.0
    assert estimate.total_cost_bps == pytest.approx(4.0)
    assert estimate.net_expected_edge_bps == pytest.approx(6.0)
    assert estimate.exceeds_min_edge_threshold is True
    assert estimate.profitable_after_costs is True


def test_limit_at_mid_uses_horizon_probability_and_zero_spread_multiplier() -> None:
    estimate_1m = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        order_style=OrderStyle.LIMIT_AT_MID.value,
        quantity=100.0,
    )
    estimate_15m = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="15m",
        midprice=100.0,
        spread_bps=2.0,
        order_style=OrderStyle.LIMIT_AT_MID.value,
        quantity=100.0,
    )

    assert estimate_1m.spread_cost_bps == 0.0
    assert estimate_1m.missed_fill_probability == 0.30
    assert estimate_15m.missed_fill_probability == 0.10
    assert estimate_1m.missed_fill_penalty_bps > estimate_15m.missed_fill_penalty_bps


def test_missed_fill_penalty_uses_pre_missed_fill_net_edge() -> None:
    config = CostModelConfig(
        round_trip_slippage_per_share=0.08,
        limit_order_missed_fill_probability_by_horizon={
            "1m": 0.50,
            "5m": 0.20,
            "15m": 0.10,
        },
    )

    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        order_style=OrderStyle.LIMIT_AT_MID.value,
        config=config,
    )

    assert estimate.base_cost_bps == pytest.approx(8.0)
    assert estimate.pre_missed_fill_net_edge_bps == pytest.approx(2.0)
    assert estimate.missed_fill_penalty_bps == pytest.approx(1.0)


def test_missed_fill_penalty_is_zero_when_pre_missed_fill_edge_is_negative() -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=1.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        order_style=OrderStyle.LIMIT_AT_MID.value,
    )

    assert estimate.pre_missed_fill_net_edge_bps < 0
    assert estimate.missed_fill_penalty_bps == 0.0


def test_size_penalty_is_zero_below_free_quantity_and_applies_above() -> None:
    small = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        quantity=100.0,
    )
    large = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        quantity=1100.0,
    )

    assert small.size_penalty_bps == 0.0
    assert large.size_penalty_bps == pytest.approx(0.25)


def test_mid_price_buy_and_sell_expected_move_math() -> None:
    buy = estimate_cost_from_mid_prices(
        ticker="XOM",
        side=SignalSide.BUY,
        entry_midprice=100.0,
        exit_midprice=101.0,
        horizon="1m",
        spread_bps=1.0,
    )
    sell = estimate_cost_from_mid_prices(
        ticker="XOM",
        side=SignalSide.SELL,
        entry_midprice=100.0,
        exit_midprice=99.0,
        horizon="1m",
        spread_bps=1.0,
    )

    assert buy.expected_gross_move_bps == pytest.approx(100.0)
    assert sell.expected_gross_move_bps == pytest.approx(10000.0 / 99.0)


def test_mid_price_function_derives_spread_from_bid_ask_without_fill_price_basis() -> None:
    estimate = estimate_cost_from_mid_prices(
        ticker="XOM",
        side=SignalSide.BUY,
        entry_midprice=100.0,
        exit_midprice=101.0,
        horizon="1m",
        bid_price=99.99,
        ask_price=100.01,
    )

    assert estimate.midprice == 100.0
    assert estimate.expected_gross_move_bps == pytest.approx(100.0)
    assert estimate.spread_bps == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("field_name", "kwargs"),
    [
        ("entry_midprice", {"entry_midprice": 0.0, "exit_midprice": 101.0}),
        ("exit_midprice", {"entry_midprice": 100.0, "exit_midprice": 0.0}),
        ("midprice", {"entry_midprice": 100.0, "exit_midprice": 101.0, "midprice": 0.0}),
    ],
)
def test_invalid_prices_fail(field_name: str, kwargs: dict[str, float]) -> None:
    if field_name == "midprice":
        with pytest.raises(CostModelError, match="midprice"):
            estimate_cost_from_expected_move(
                ticker="XOM",
                side=SignalSide.BUY,
                expected_gross_move_bps=5.0,
                horizon="1m",
                midprice=kwargs["midprice"],
                spread_bps=1.0,
            )
        return

    with pytest.raises(CostModelError, match=field_name):
        estimate_cost_from_mid_prices(
            ticker="XOM",
            side=SignalSide.BUY,
            horizon="1m",
            spread_bps=1.0,
            **kwargs,
        )


def test_crossed_or_locked_books_fail() -> None:
    with pytest.raises(CostModelError, match="crossed or locked"):
        estimate_cost_from_expected_move(
            ticker="XOM",
            side=SignalSide.BUY,
            expected_gross_move_bps=5.0,
            horizon="1m",
            midprice=100.0,
            spread_bps=1.0,
            is_crossed_or_locked=True,
        )


@pytest.mark.parametrize(
    ("spread", "spread_bps", "match"),
    [
        (-0.01, None, "spread"),
        (None, -1.0, "spread_bps"),
    ],
)
def test_negative_spread_inputs_fail(
    spread: float | None,
    spread_bps: float | None,
    match: str,
) -> None:
    with pytest.raises(CostModelError, match=match):
        estimate_cost_from_expected_move(
            ticker="XOM",
            side=SignalSide.BUY,
            expected_gross_move_bps=5.0,
            horizon="1m",
            midprice=100.0,
            spread=spread,
            spread_bps=spread_bps,
        )


def test_zero_or_missing_spread_applies_fallback() -> None:
    zero = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=5.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=0.0,
    )
    missing = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=5.0,
        horizon="1m",
        midprice=100.0,
    )

    assert zero.fallback_spread_applied is True
    assert missing.fallback_spread_applied is True
    assert zero.spread_bps == 1.0
    assert missing.spread_bps == 1.0
    assert zero.spread_cost_bps == 1.0
    assert zero.reason == "fallback_minimum_spread_bps_applied"


def test_fallback_prevents_phantom_zero_cost_market_edge() -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=3.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=0.0,
    )

    assert estimate.total_cost_bps == pytest.approx(3.0)
    assert estimate.net_expected_edge_bps == pytest.approx(0.0)
    assert estimate.exceeds_min_edge_threshold is False


def test_threshold_uses_strict_greater_than() -> None:
    greater = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=5.01,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
    )
    equal = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=5.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
    )
    below = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=4.99,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
    )

    assert greater.net_expected_edge_bps > greater.min_edge_bps
    assert greater.exceeds_min_edge_threshold is True
    assert exceeds_min_edge_threshold(greater) is True
    assert equal.net_expected_edge_bps == pytest.approx(equal.min_edge_bps)
    assert equal.exceeds_min_edge_threshold is False
    assert exceeds_min_edge_threshold(equal) is False
    assert below.exceeds_min_edge_threshold is False


def test_cost_estimate_serializes_to_json_safe_values() -> None:
    estimate = estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=10.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=2.0,
        trace_id="TRACE-COST-MODEL-TEST",
    )

    parsed = from_json_string(to_json_string(estimate))

    assert parsed["side"] == SignalSide.BUY.value
    assert parsed["trace_id"] == "TRACE-COST-MODEL-TEST"
    for value in parsed.values():
        if isinstance(value, float):
            assert math.isfinite(value)


def test_cost_estimate_rejects_non_finite_numbers() -> None:
    with pytest.raises(CostModelError, match="midprice"):
        CostEstimate(
            ticker="XOM",
            side=SignalSide.BUY,
            horizon="1m",
            order_style=OrderStyle.MARKET.value,
            quantity=1.0,
            midprice=float("inf"),
            spread_bps=1.0,
            expected_gross_move_bps=1.0,
            spread_cost_bps=1.0,
            estimated_slippage_bps=1.0,
            size_penalty_bps=0.0,
            base_cost_bps=2.0,
            missed_fill_probability=0.0,
            pre_missed_fill_net_edge_bps=-1.0,
            missed_fill_penalty_bps=0.0,
            total_cost_bps=2.0,
            min_edge_bps=1.0,
            net_expected_edge_bps=-1.0,
            exceeds_min_edge_threshold=False,
            profitable_after_costs=False,
            assumptions_version="cost_model_v1",
            fallback_spread_applied=False,
        )


def test_cost_model_module_is_pure_and_avoids_forbidden_imports() -> None:
    module_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "market_relay_engine"
        / "market_data"
        / "cost_model.py"
    )
    source = module_path.read_text(encoding="utf-8").lower()

    assert "featuresnapshot" not in source
    assert "feature_builder" not in source
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
