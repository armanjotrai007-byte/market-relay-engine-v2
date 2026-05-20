"""Health check for historical/live feature parity helpers."""

from __future__ import annotations

import math
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
from market_relay_engine.market_data.feature_parity import (
    assert_event_time_ordered,
    assert_feature_snapshots_equivalent,
    build_historical_style_snapshot,
    build_live_style_snapshot,
)
from tests.fixtures.market_records import (
    make_market_quote_record,
    make_market_trade_record,
)


TRACE_ID = "TRACE-PR8-FEATURE-PARITY"


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
        records = [
            make_market_trade_record(ticker="XOM", price=100.0, size=10.0, index=1),
            make_market_quote_record(
                ticker="XOM",
                bid_price=100.0,
                ask_price=100.2,
                bid_size=500.0,
                ask_size=400.0,
                index=2,
            ),
            make_market_trade_record(ticker="XOM", price=101.0, size=15.0, index=3),
        ]

        assert_event_time_ordered(records)
        historical = build_historical_style_snapshot(records, trace_id=TRACE_ID)
        live = build_live_style_snapshot(records, trace_id=TRACE_ID)
        assert_feature_snapshots_equivalent(historical, live)

        serialized_historical = from_json_string(to_json_string(historical))
        serialized_live = from_json_string(to_json_string(live))
        if serialized_historical["features"] != serialized_live["features"]:
            raise AssertionError("serialized feature dictionaries differ")
        if serialized_historical["snapshot_time"] != serialized_live["snapshot_time"]:
            raise AssertionError("serialized snapshot_time values differ")

        _assert_json_safe(serialized_historical["features"], "historical.features")
        _assert_json_safe(serialized_live["features"], "live.features")
    except Exception as exc:  # noqa: BLE001 - health check should fail clearly.
        print(f"Feature parity validation FAILED: {exc}")
        return 1

    print("Feature parity validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
