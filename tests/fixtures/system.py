"""Fake system health event fixtures for tests."""

from __future__ import annotations

from market_relay_engine.contracts.system import SystemHealthEvent
from tests.fixtures.ids import TRACE_ID_APPROVED_OIL, stable_record_id
from tests.fixtures.times import seconds_after_market_open


def make_system_health_event(
    *,
    component: str = "fixture_validation",
    status: str = "healthy",
    message: str | None = "Fixture system health is healthy.",
    index: int = 1,
    trace_id: str = TRACE_ID_APPROVED_OIL,
    feed_delay_ms: float | None = 15.0,
) -> SystemHealthEvent:
    """Return a fake system health event without a monitoring loop."""
    return SystemHealthEvent(
        event_time=seconds_after_market_open(index + 12),
        component=component,
        status=status,
        health_event_id=stable_record_id("health_event", index),
        message=message,
        cpu_percent=22.5,
        memory_percent=48.0,
        clock_offset_ms=1.5,
        feed_delay_ms=feed_delay_ms,
        reconnect_count=0,
        trace_id=trace_id,
    )


def make_healthy_system_health_event(**overrides: object) -> SystemHealthEvent:
    """Return a fake healthy system health event."""
    return make_system_health_event(status="healthy", **overrides)


def make_warning_system_health_event(**overrides: object) -> SystemHealthEvent:
    """Return a fake warning system health event."""
    return make_system_health_event(
        status="warning",
        message="Fixture warning for latency or feed delay.",
        feed_delay_ms=420.0,
        **overrides,
    )


def build_system_examples() -> list[SystemHealthEvent]:
    """Return representative fake system health events."""
    return [
        make_healthy_system_health_event(index=1),
        make_warning_system_health_event(index=2),
    ]

