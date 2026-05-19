"""Health check for the canonical feature builder."""

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
from market_relay_engine.market_data.feature_builder import (
    V1_FEATURE_KEYS,
    build_feature_snapshot,
)
from tests.fixtures.market_records import (
    make_market_quote_record,
    make_market_trade_record,
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
        snapshot = build_feature_snapshot(records)
        serialized = from_json_string(to_json_string(snapshot))

        if set(snapshot.features) != V1_FEATURE_KEYS:
            raise AssertionError("snapshot feature keys do not match V1_FEATURE_KEYS")
        if serialized["feature_version"] != "feature_v1":
            raise AssertionError("snapshot feature_version is not feature_v1")
        if not str(serialized["snapshot_time"]).endswith("Z"):
            raise AssertionError("snapshot_time did not serialize as UTC Z")
        if serialized["source_record_count"] != 3:
            raise AssertionError("source_record_count is not 3")
        _assert_json_safe(serialized["features"], "features")
    except Exception as exc:  # noqa: BLE001 - health check should fail clearly.
        print(f"Feature builder validation FAILED: {exc}")
        return 1

    print("Feature builder validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
