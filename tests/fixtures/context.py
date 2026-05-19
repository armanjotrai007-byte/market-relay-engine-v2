"""Fake context record fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextFlag,
    ContextIndicatorSnapshot,
)
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.times import minutes_after_market_open, seconds_after_market_open


def make_context_indicator(
    *,
    source: str,
    ticker_or_sector: str,
    indicator_name: str,
    value: object,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    window: str | None = "intraday",
    units: str | None = None,
    freshness_seconds: float | None = 60.0,
    stale: bool = False,
) -> ContextIndicatorSnapshot:
    """Return a fake structured context indicator."""
    snapshot_time = seconds_after_market_open(index + 6)
    source_event_time = minutes_after_market_open(-45) if stale else snapshot_time
    return ContextIndicatorSnapshot(
        snapshot_time=snapshot_time,
        source=source,
        ticker_or_sector=ticker_or_sector,
        indicator_name=indicator_name,
        value=value,
        window=window,
        units=units,
        freshness_seconds=7200.0 if stale else freshness_seconds,
        source_event_time=source_event_time,
        trace_id=trace_id,
    )


def make_eia_window_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake EIA window indicator."""
    return make_context_indicator(
        source="fake_eia_calendar_fixture",
        ticker_or_sector="oil",
        indicator_name="eia_window",
        value=True,
        units="boolean",
        **overrides,
    )


def make_sector_proxy_move_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake sector proxy move indicator."""
    return make_context_indicator(
        source="fake_sector_proxy_fixture",
        ticker_or_sector="XLE",
        indicator_name="sector_proxy_move",
        value={"proxy": "XLE", "return_5m": 0.012},
        units="return",
        **overrides,
    )


def make_fred_rate_context_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake FRED/rate context indicator."""
    return make_context_indicator(
        source="fake_fred_fixture",
        ticker_or_sector="rates",
        indicator_name="rate_context",
        value={"ten_year_yield_change_bps": 4.2},
        units="basis_points",
        **overrides,
    )


def make_usaspending_defense_award_indicator(**overrides: object) -> ContextIndicatorSnapshot:
    """Return a fake USAspending defense award context indicator."""
    return make_context_indicator(
        source="fake_usaspending_fixture",
        ticker_or_sector="defense",
        indicator_name="defense_award_context",
        value={"award_ticker": "LMT", "award_size_usd": 125000000},
        units="usd",
        **overrides,
    )


def make_context_ai_event(
    *,
    source: str = "fake_ai_context_fixture",
    source_id: str = "fixture-ai-news-0001",
    affected_tickers: list[str] | None = None,
    affected_sector: str | None = "oil",
    event_type: str = "news_context",
    risk_level: str = "normal",
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    expired: bool = False,
) -> ContextAIEvent:
    """Return a fake AI context event without making AI calls."""
    event_time = minutes_after_market_open(-30) if expired else seconds_after_market_open(index + 7)
    valid_from = event_time
    valid_until = minutes_after_market_open(-1) if expired else minutes_after_market_open(20)
    return ContextAIEvent(
        event_time=event_time,
        source=source,
        source_id=source_id,
        affected_tickers=affected_tickers or ["XOM"],
        event_type=event_type,
        context_event_id=stable_record_id("context_event", index),
        affected_sector=affected_sector,
        sentiment="neutral",
        urgency="medium",
        risk_level=risk_level,
        confidence=0.68,
        valid_from=valid_from,
        valid_until=valid_until,
        summary="Fake context event for fixture tests.",
        prompt_version="fixture_prompt_v1",
        model_version="fixture_context_model_v1",
        raw_input_hash=stable_record_id("raw_input_hash", index),
        trace_id=trace_id,
    )


def make_context_flag(
    *,
    source: str = "fake_ai_context_fixture",
    flag_type: str = "ai_context_high_risk",
    severity: str = "warning",
    ticker: str | None = "XOM",
    sector: str | None = "oil",
    confidence: float | None = 0.72,
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    expired: bool = False,
) -> ContextFlag:
    """Return a fake context flag for risk fixtures."""
    return ContextFlag(
        event_time=minutes_after_market_open(-20) if expired else seconds_after_market_open(index + 8),
        source=source,
        flag_type=flag_type,
        severity=severity,
        context_flag_id=stable_record_id("context_flag", index),
        ticker=ticker,
        sector=sector,
        confidence=confidence,
        valid_until=minutes_after_market_open(-1) if expired else minutes_after_market_open(15),
        trace_id=trace_id,
    )


def make_ai_news_context_flag(**overrides: object) -> ContextFlag:
    """Return a fake AI news context flag."""
    return make_context_flag(
        source="fake_ai_news_fixture",
        flag_type="ai_news_context",
        **overrides,
    )


def make_sec_context_flag(**overrides: object) -> ContextFlag:
    """Return a fake SEC context flag."""
    return make_context_flag(
        source="fake_sec_fixture",
        flag_type="sec_context",
        **overrides,
    )


def make_social_context_flag(**overrides: object) -> ContextFlag:
    """Return a fake social context flag."""
    return make_context_flag(
        source="fake_social_fixture",
        flag_type="social_context",
        **overrides,
    )


def build_context_examples() -> list[object]:
    """Return representative fake context records."""
    return [
        make_eia_window_indicator(),
        make_sector_proxy_move_indicator(index=2),
        make_fred_rate_context_indicator(index=3),
        make_usaspending_defense_award_indicator(index=4),
        make_context_ai_event(index=1),
        make_ai_news_context_flag(index=1),
        make_sec_context_flag(index=2),
        make_social_context_flag(index=3),
    ]

