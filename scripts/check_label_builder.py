"""Health check for the cost-aware supervised label builder."""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.serialization import from_json_string, to_json_string
from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.market_data.label_builder import (
    ForwardPriceObservation,
    LabelBuilderConfig,
    LabelBuilderError,
    build_label_for_snapshot,
    build_labels_for_snapshots,
)


NY = ZoneInfo("America/New_York")


def _snapshot(
    local_time: datetime,
    *,
    ticker: str = "XOM",
    midprice: float = 100.0,
    spread_bps: float = 2.0,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_time=local_time,
        ticker=ticker,
        feature_version="feature_v1",
        features={
            "ticker": ticker,
            "midprice": midprice,
            "spread": midprice * spread_bps / 10000.0,
            "spread_bps": spread_bps,
            "is_crossed_or_locked": False,
        },
        source_record_count=1,
        lookback_window_seconds=60.0,
        feature_snapshot_id="feature_snapshot_check_label_builder",
        trace_id="TRACE-CHECK-LABEL-BUILDER",
    )


def _forward(
    local_time: datetime,
    *,
    ticker: str = "XOM",
    midprice: float,
) -> ForwardPriceObservation:
    return ForwardPriceObservation(
        event_time=local_time,
        ticker=ticker,
        midprice=midprice,
    )


def _assert_json_safe(value: Any, path: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
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
        base_time = datetime(2026, 5, 18, 10, 0, tzinfo=NY)
        snapshot = _snapshot(base_time)
        forward_prices = [
            _forward(base_time + timedelta(minutes=1), midprice=100.20),
            _forward(base_time + timedelta(minutes=5), midprice=100.40),
            _forward(base_time + timedelta(minutes=15), midprice=100.80),
        ]

        labels = build_labels_for_snapshots([snapshot], forward_prices)
        if len(labels) != 6:
            raise AssertionError("expected BUY/SELL labels for 3 horizons")
        if not any(label.profitable_after_costs for label in labels):
            raise AssertionError("controlled labels should include a profitable example")
        if not any(not label.profitable_after_costs for label in labels):
            raise AssertionError("controlled labels should include an unprofitable example")
        for label in labels:
            parsed = from_json_string(to_json_string(label))
            _assert_json_safe(parsed)
            if parsed["feature_snapshot_id"] != snapshot.feature_snapshot_id:
                raise AssertionError("label did not preserve feature_snapshot_id")
            if parsed["cost_assumptions_version"] != "cost_model_v1":
                raise AssertionError("label did not preserve cost assumptions version")

        buy_label = build_label_for_snapshot(
            snapshot=snapshot,
            forward_prices=forward_prices,
            side=SignalSide.BUY,
            horizon="1m",
        )
        sell_label = build_label_for_snapshot(
            snapshot=snapshot,
            forward_prices=forward_prices,
            side=SignalSide.SELL,
            horizon="1m",
        )
        if buy_label.forward_midprice != 100.20:
            raise AssertionError("BUY label did not use the 1m forward midprice")
        if sell_label.expected_gross_move_bps >= 0:
            raise AssertionError("SELL label should see an upward move as unfavorable")

        try:
            build_label_for_snapshot(
                snapshot=snapshot,
                forward_prices=[],
                side=SignalSide.BUY,
                horizon="1m",
            )
        except LabelBuilderError:
            pass
        else:
            raise AssertionError("missing forward price should fail clearly")

        near_close = _snapshot(datetime(2026, 5, 18, 15, 58, tzinfo=NY))
        after_close_forward = [
            _forward(datetime(2026, 5, 18, 16, 3, tzinfo=NY), midprice=101.0)
        ]
        try:
            build_label_for_snapshot(
                snapshot=near_close,
                forward_prices=after_close_forward,
                side=SignalSide.BUY,
                horizon="5m",
            )
        except LabelBuilderError:
            pass
        else:
            raise AssertionError("after-close horizon should fail clearly")

        skipped = build_labels_for_snapshots(
            [near_close],
            after_close_forward,
            config=LabelBuilderConfig(
                horizons=("5m",),
                allow_missing_forward_price=True,
            ),
        )
        if skipped != []:
            raise AssertionError("regular-hours-invalid labels should be skippable")
    except Exception as exc:  # noqa: BLE001 - health check should fail clearly.
        print(f"Label builder validation FAILED: {exc}")
        return 1

    print("Label builder validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
