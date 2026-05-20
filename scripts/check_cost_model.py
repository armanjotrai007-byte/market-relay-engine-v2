"""Health check for the pure V1 cost model."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.market_data.cost_model import (
    CostModelConfig,
    CostModelError,
    OrderStyle,
    estimate_cost_from_expected_move,
    estimate_cost_from_mid_prices,
    exceeds_min_edge_threshold,
)


def _assert_json_safe(value: Any, path: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise AssertionError(f"{path} contains non-finite float")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AssertionError(f"{path} contains non-string key")
            _assert_json_safe(item, f"{path}.{key}")
        return
    raise AssertionError(f"{path} contains non-JSON-safe type {type(value).__name__}")


def main() -> int:
    try:
        buy_market = estimate_cost_from_mid_prices(
            ticker="XOM",
            side=SignalSide.BUY,
            entry_midprice=100.0,
            exit_midprice=100.20,
            horizon="1m",
            spread_bps=2.0,
            order_style=OrderStyle.MARKET.value,
            quantity=100.0,
        )
        if buy_market.estimated_slippage_bps != 2.0:
            raise AssertionError("round-trip slippage should be 2 bps at $100")
        if buy_market.missed_fill_probability != 0:
            raise AssertionError("MARKET order should have zero missed-fill probability")

        sell_market = estimate_cost_from_mid_prices(
            ticker="LMT",
            side=SignalSide.SELL,
            entry_midprice=100.0,
            exit_midprice=99.80,
            horizon="5m",
            spread_bps=2.0,
            order_style=OrderStyle.MARKET.value,
            quantity=100.0,
        )
        if sell_market.expected_gross_move_bps <= 0:
            raise AssertionError("SELL favorable move should be positive")

        config = CostModelConfig(
            round_trip_slippage_per_share=0.00,
            limit_order_missed_fill_probability_by_horizon={
                "1m": 0.50,
                "5m": 0.25,
                "15m": 0.10,
            },
        )
        limit_mid = estimate_cost_from_expected_move(
            ticker="XOM",
            side=SignalSide.BUY,
            expected_gross_move_bps=10.0,
            horizon="1m",
            midprice=100.0,
            spread_bps=8.0,
            order_style=OrderStyle.LIMIT_AT_MID.value,
            quantity=100.0,
            config=config,
        )
        if limit_mid.missed_fill_probability != 0.50:
            raise AssertionError("LIMIT_AT_MID should use horizon-specific probability")
        if limit_mid.missed_fill_penalty_bps != 5.0:
            raise AssertionError("LIMIT_AT_MID missed-fill penalty did not apply")

        fallback = estimate_cost_from_expected_move(
            ticker="XOM",
            side=SignalSide.BUY,
            expected_gross_move_bps=5.0,
            horizon="1m",
            midprice=100.0,
            spread_bps=0.0,
        )
        if not fallback.fallback_spread_applied or fallback.spread_bps != 1.0:
            raise AssertionError("zero spread should apply fallback spread")

        equal_threshold = estimate_cost_from_expected_move(
            ticker="XOM",
            side=SignalSide.BUY,
            expected_gross_move_bps=5.0,
            horizon="1m",
            midprice=100.0,
            spread_bps=2.0,
            config=CostModelConfig(min_edge_bps=1.0, round_trip_slippage_per_share=0.02),
        )
        if equal_threshold.net_expected_edge_bps != 1.0:
            raise AssertionError("threshold fixture should produce exactly 1 bps net edge")
        if exceeds_min_edge_threshold(equal_threshold):
            raise AssertionError("strict threshold should reject equality")

        try:
            estimate_cost_from_expected_move(
                ticker="XOM",
                side=SignalSide.BUY,
                expected_gross_move_bps=5.0,
                horizon="1m",
                midprice=100.0,
                spread_bps=2.0,
                is_crossed_or_locked=True,
            )
        except CostModelError:
            pass
        else:
            raise AssertionError("crossed/locked book should fail")

        serialized = from_json_string(to_json_string(buy_market))
        required_fields = {
            "ticker",
            "side",
            "horizon",
            "order_style",
            "quantity",
            "midprice",
            "spread_bps",
            "expected_gross_move_bps",
            "base_cost_bps",
            "missed_fill_probability",
            "pre_missed_fill_net_edge_bps",
            "missed_fill_penalty_bps",
            "total_cost_bps",
            "exceeds_min_edge_threshold",
            "profitable_after_costs",
            "fallback_spread_applied",
        }
        missing_fields = required_fields - set(serialized)
        if missing_fields:
            raise AssertionError(f"serialized estimate missing fields: {missing_fields}")
        _assert_json_safe(serialized)
    except Exception as exc:  # noqa: BLE001 - health check should fail clearly.
        print(f"Cost model validation FAILED: {exc}")
        return 1

    print("Cost model validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
