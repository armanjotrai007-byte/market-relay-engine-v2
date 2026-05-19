"""Fake model signal fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from tests.fixtures.feature_snapshots import FEATURE_VERSION, make_feature_snapshot
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.times import seconds_after_market_open


MODEL_VERSION = "fixture_model_v1"
CALIBRATION_VERSION = "fixture_calibration_v1"


def make_model_signal(
    *,
    signal: SignalSide = SignalSide.BUY,
    ticker: str = "XOM",
    index: int = 1,
    feature_snapshot: FeatureSnapshot | None = None,
    confidence: float = 0.72,
    raw_score: float | None = 0.34,
    trace_id: str = TRACE_ID_APPROVED_OIL,
) -> ModelSignal:
    """Return a fake model signal without running model inference."""
    feature_snapshot = feature_snapshot or make_feature_snapshot(
        ticker=ticker,
        index=index,
        trace_id=trace_id,
    )
    return ModelSignal(
        signal_time=seconds_after_market_open(index + 4),
        ticker=ticker,
        signal=signal,
        confidence=confidence,
        raw_score=raw_score,
        model_version=MODEL_VERSION,
        calibration_version=CALIBRATION_VERSION,
        feature_version=FEATURE_VERSION,
        feature_snapshot_id=feature_snapshot.feature_snapshot_id,
        signal_id=stable_record_id("signal", index),
        trace_id=trace_id,
    )


def make_buy_model_signal(**overrides: object) -> ModelSignal:
    """Return a fake BUY signal."""
    return make_model_signal(signal=SignalSide.BUY, **overrides)


def make_sell_model_signal(**overrides: object) -> ModelSignal:
    """Return a fake SELL signal."""
    return make_model_signal(signal=SignalSide.SELL, **overrides)


def make_hold_model_signal(**overrides: object) -> ModelSignal:
    """Return a fake HOLD signal."""
    return make_model_signal(
        signal=SignalSide.HOLD,
        confidence=0.51,
        raw_score=0.02,
        **overrides,
    )


def make_do_nothing_model_signal(**overrides: object) -> ModelSignal:
    """Return a fake DO_NOTHING signal."""
    return make_model_signal(
        signal=SignalSide.DO_NOTHING,
        confidence=0.44,
        raw_score=None,
        **overrides,
    )


def build_model_signal_examples() -> list[ModelSignal]:
    """Return representative fake model signals."""
    return [
        make_buy_model_signal(index=1),
        make_sell_model_signal(ticker="LMT", index=2),
        make_hold_model_signal(ticker="SPY", index=3),
        make_do_nothing_model_signal(ticker="XLE", index=4),
    ]

