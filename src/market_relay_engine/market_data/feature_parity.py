"""Historical/live feature parity helpers for the canonical feature builder."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, Iterable

from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.market import MarketRecord
from market_relay_engine.market_data.feature_builder import (
    FeatureBuilder,
    FeatureBuilderConfig,
)


FLOAT_TOLERANCE = 1e-12


class FeatureParityError(ValueError):
    """Raised when feature parity inputs or snapshots are not comparable."""


def build_historical_style_snapshot(
    records: Iterable[MarketRecord],
    config: FeatureBuilderConfig | None = None,
    feature_version: str | None = None,
    trace_id: str | None = None,
) -> FeatureSnapshot:
    """Sort records by event time and return the final canonical feature snapshot."""
    record_list = _single_ticker_records(records, "build_historical_style_snapshot")
    builder = FeatureBuilder(_resolve_config(config, feature_version))
    snapshot: FeatureSnapshot | None = None

    for record in sorted(record_list, key=lambda item: item.event_time):
        snapshot = builder.update(record)

    if snapshot is None:
        raise FeatureParityError("No historical-style feature snapshot was produced")
    if trace_id is not None:
        return builder.build_snapshot(snapshot.ticker, trace_id=trace_id)
    return snapshot


def build_live_style_snapshot(
    records: Iterable[MarketRecord],
    config: FeatureBuilderConfig | None = None,
    feature_version: str | None = None,
    trace_id: str | None = None,
) -> FeatureSnapshot:
    """Process records in exact caller order and return the final snapshot."""
    record_list = _single_ticker_records(records, "build_live_style_snapshot")
    builder = FeatureBuilder(_resolve_config(config, feature_version))
    snapshot: FeatureSnapshot | None = None

    for record in record_list:
        snapshot = builder.update(record)

    if snapshot is None:
        raise FeatureParityError("No live-style feature snapshot was produced")
    if trace_id is not None:
        return builder.build_snapshot(snapshot.ticker, trace_id=trace_id)
    return snapshot


def assert_event_time_ordered(records: Iterable[MarketRecord]) -> None:
    """Assert that records are in non-decreasing event-time order."""
    record_list = list(records)
    for index, record in enumerate(record_list):
        if not isinstance(record, MarketRecord):
            raise FeatureParityError("assert_event_time_ordered requires MarketRecord inputs")
        if index == 0:
            continue
        previous = record_list[index - 1]
        if record.event_time < previous.event_time:
            raise FeatureParityError(
                "Records must be in non-decreasing event_time order for formal parity"
            )


def feature_snapshot_semantic_dict(snapshot: FeatureSnapshot) -> dict[str, Any]:
    """Return testing-only deterministic fields used by validation scripts."""
    _require_snapshot(snapshot, "snapshot")
    semantic = {
        "ticker": snapshot.ticker,
        "snapshot_time": snapshot.snapshot_time,
        "feature_version": snapshot.feature_version,
        "features": dict(snapshot.features),
        "source_record_count": snapshot.source_record_count,
        "lookback_window_seconds": snapshot.lookback_window_seconds,
        "schema_version": snapshot.schema_version,
    }
    if snapshot.trace_id is not None:
        semantic["trace_id"] = snapshot.trace_id
    return semantic


def assert_feature_snapshots_equivalent(
    left: FeatureSnapshot,
    right: FeatureSnapshot,
) -> None:
    """Assert semantic equivalence while ignoring generated snapshot IDs."""
    _require_snapshot(left, "left")
    _require_snapshot(right, "right")

    _assert_equal("ticker", left.ticker, right.ticker)
    _assert_equal("snapshot_time", left.snapshot_time, right.snapshot_time)
    _assert_equal("feature_version", left.feature_version, right.feature_version)
    _assert_equal("source_record_count", left.source_record_count, right.source_record_count)
    _assert_value_equivalent(
        "lookback_window_seconds",
        left.lookback_window_seconds,
        right.lookback_window_seconds,
    )
    _assert_equal("schema_version", left.schema_version, right.schema_version)
    if left.trace_id is not None or right.trace_id is not None:
        _assert_equal("trace_id", left.trace_id, right.trace_id)

    left_keys = set(left.features)
    right_keys = set(right.features)
    if left_keys != right_keys:
        missing_from_right = sorted(left_keys - right_keys)
        missing_from_left = sorted(right_keys - left_keys)
        raise FeatureParityError(
            "Feature keys differ: "
            f"missing_from_right={missing_from_right}, "
            f"missing_from_left={missing_from_left}"
        )

    for key in sorted(left_keys):
        _assert_value_equivalent(
            f"features.{key}",
            left.features[key],
            right.features[key],
        )


def _single_ticker_records(
    records: Iterable[MarketRecord],
    helper_name: str,
) -> list[MarketRecord]:
    record_list = list(records)
    if not record_list:
        raise FeatureParityError(f"{helper_name} requires at least one MarketRecord")

    for record in record_list:
        if not isinstance(record, MarketRecord):
            raise FeatureParityError(f"{helper_name} requires MarketRecord inputs")

    tickers = {record.ticker for record in record_list}
    if len(tickers) != 1:
        raise FeatureParityError(f"{helper_name} supports exactly one ticker")
    return record_list


def _resolve_config(
    config: FeatureBuilderConfig | None,
    feature_version: str | None,
) -> FeatureBuilderConfig:
    resolved_config = config or FeatureBuilderConfig()
    if feature_version is not None:
        return replace(resolved_config, feature_version=feature_version)
    return resolved_config


def _require_snapshot(snapshot: FeatureSnapshot, name: str) -> None:
    if not isinstance(snapshot, FeatureSnapshot):
        raise FeatureParityError(f"{name} must be a FeatureSnapshot")


def _assert_equal(path: str, left: Any, right: Any) -> None:
    if left != right:
        raise FeatureParityError(f"{path} differs: {left!r} != {right!r}")


def _assert_value_equivalent(path: str, left: Any, right: Any) -> None:
    if left is None or right is None:
        _assert_equal(path, left, right)
        return

    if isinstance(left, bool) or isinstance(right, bool):
        _assert_equal(path, left, right)
        return

    if _is_float_comparable(left, right):
        left_float = float(left)
        right_float = float(right)
        if not math.isfinite(left_float) or not math.isfinite(right_float):
            raise FeatureParityError(f"{path} contains NaN or Infinity")
        if not math.isclose(
            left_float,
            right_float,
            rel_tol=FLOAT_TOLERANCE,
            abs_tol=FLOAT_TOLERANCE,
        ):
            raise FeatureParityError(f"{path} differs: {left!r} != {right!r}")
        return

    _assert_equal(path, left, right)


def _is_float_comparable(left: Any, right: Any) -> bool:
    numeric_types = (int, float)
    return (
        isinstance(left, numeric_types)
        and isinstance(right, numeric_types)
        and (isinstance(left, float) or isinstance(right, float))
    )
