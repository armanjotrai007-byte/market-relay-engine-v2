"""Fake feature snapshot fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.features import FeatureSnapshot
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.times import seconds_after_market_open


FEATURE_VERSION = "fixture_feature_v1"


def make_feature_snapshot(
    *,
    ticker: str = "XOM",
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    midprice: float = 118.42,
    spread: float = 0.02,
    spread_bps: float = 1.69,
    return_1m: float = 0.0012,
    volume_1m: float = 12500.0,
    volatility_5m: float = 0.0041,
) -> FeatureSnapshot:
    """Return a fake feature snapshot without calculating features."""
    return FeatureSnapshot(
        snapshot_time=seconds_after_market_open(index + 3),
        ticker=ticker,
        feature_version=FEATURE_VERSION,
        features={
            "midprice": midprice,
            "spread": spread,
            "spread_bps": spread_bps,
            "return_1m": return_1m,
            "volume_1m": volume_1m,
            "volatility_5m": volatility_5m,
        },
        source_record_count=2,
        lookback_window_seconds=300.0,
        feature_snapshot_id=stable_record_id("feature_snapshot", index),
        trace_id=trace_id,
    )


def make_oil_feature_snapshot() -> FeatureSnapshot:
    """Return a fake oil-sector feature snapshot."""
    return make_feature_snapshot(ticker="XOM", index=1)


def make_defense_feature_snapshot() -> FeatureSnapshot:
    """Return a fake defense-sector feature snapshot."""
    return make_feature_snapshot(
        ticker="LMT",
        index=2,
        midprice=472.35,
        spread=0.40,
        spread_bps=8.47,
        return_1m=-0.0008,
        volume_1m=3200.0,
        volatility_5m=0.0065,
    )


def build_feature_snapshot_examples() -> list[FeatureSnapshot]:
    """Return representative fake feature snapshots."""
    return [
        make_oil_feature_snapshot(),
        make_defense_feature_snapshot(),
    ]

