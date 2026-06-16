"""Validate the in-memory context state cache without external I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.state_cache import (  # noqa: E402
    ContextStateCache,
    ContextStateUpdateStatus,
    make_global_context_entry,
    make_sector_context_entry,
    make_ticker_context_entry,
)
from market_relay_engine.common.serialization import to_json_string  # noqa: E402


def main() -> int:
    now = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    cache = ContextStateCache(max_entries=5)

    global_entry = make_global_context_entry(
        name="market_regime",
        value="risk_off",
        severity="HIGH",
        source="manual",
        updated_at=now,
        details={"risk": {"active": True}},
    )
    written = cache.update(global_entry)
    assert written.status is ContextStateUpdateStatus.WRITTEN
    global_entry.details["risk"]["active"] = False  # type: ignore[index]
    assert cache.get_global("market_regime", now=now).details["risk"]["active"] is True  # type: ignore[union-attr,index]

    duplicate = cache.update(
        make_global_context_entry(
            name="market_regime",
            value="risk_off",
            severity="HIGH",
            source="manual",
            updated_at=now,
            details={"risk": {"active": True}},
        )
    )
    assert duplicate.status is ContextStateUpdateStatus.IGNORED_DUPLICATE

    stale_global = make_global_context_entry(
        name="market_regime",
        value="risk_on",
        severity="LOW",
        source="manual",
        updated_at=now - timedelta(minutes=1),
    )
    stale = cache.update(stale_global)
    assert stale.status is ContextStateUpdateStatus.IGNORED_STALE
    assert cache.get_global("market_regime", now=now).value == "risk_off"  # type: ignore[union-attr]

    ticker_entry = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="active",
        severity="MEDIUM",
        source="manual",
        updated_at=now + timedelta(seconds=1),
        details={"nested": {"x": "original"}},
    )
    ticker_result = cache.update(ticker_entry)
    assert ticker_result.status is ContextStateUpdateStatus.WRITTEN
    cached_ticker = cache.get_ticker("aapl", "earnings_risk", now=now)
    assert cached_ticker is not None
    assert cached_ticker.value == "active"
    cached_ticker.details["nested"]["x"] = "mutated"  # type: ignore[index]
    assert cache.get_ticker("AAPL", "earnings_risk", now=now).details["nested"]["x"] == "original"  # type: ignore[union-attr,index]

    sector_entry = make_sector_context_entry(
        sector="TECH",
        name="sector_proxy",
        value="weak",
        severity="MEDIUM",
        source="manual",
        updated_at=now + timedelta(seconds=2),
        details={"nested": {"x": "sector"}},
    )
    sector_result = cache.update(sector_entry)
    assert sector_result.status is ContextStateUpdateStatus.WRITTEN
    latest_sector = cache.latest_for_sector("tech", now=now)[0]
    latest_sector.details["nested"]["x"] = "mutated"  # type: ignore[index]
    assert cache.get_sector("TECH", "sector_proxy", now=now).details["nested"]["x"] == "sector"  # type: ignore[union-attr,index]

    low_same_time = make_ticker_context_entry(
        ticker="MSFT",
        name="same_time_risk",
        value="active",
        severity="LOW",
        source="manual",
        updated_at=now + timedelta(seconds=3),
        valid_until=now + timedelta(minutes=5),
        details={"reason": "same_event"},
        trace_id="trace_1",
    )
    assert cache.update(low_same_time).status is ContextStateUpdateStatus.WRITTEN
    assert cache.update(
        make_ticker_context_entry(
            ticker="MSFT",
            name="same_time_risk",
            value="active",
            severity="LOW",
            source="manual",
            updated_at=now + timedelta(seconds=3),
            valid_until=now + timedelta(minutes=5),
            details={"reason": "same_event"},
            trace_id="trace_2",
        )
    ).status is ContextStateUpdateStatus.IGNORED_DUPLICATE
    extended_same_time = make_ticker_context_entry(
        ticker="MSFT",
        name="same_time_risk",
        value="active",
        severity="LOW",
        source="manual",
        updated_at=now + timedelta(seconds=3),
        valid_until=now + timedelta(minutes=10),
        details={"reason": "same_event"},
    )
    assert cache.update(extended_same_time).status is ContextStateUpdateStatus.REPLACED
    high_same_time = make_ticker_context_entry(
        ticker="MSFT",
        name="same_time_risk",
        value="active",
        severity="HIGH",
        source="manual",
        updated_at=now + timedelta(seconds=3),
        valid_until=now + timedelta(minutes=10),
        details={"reason": "same_event"},
    )
    assert cache.update(high_same_time).status is ContextStateUpdateStatus.REPLACED
    assert cache.to_context_state_snapshot(ticker="MSFT", now=now).risk_level == "HIGH"

    snapshot = cache.snapshot(now=now)
    json.dumps(snapshot, allow_nan=False, sort_keys=True)
    assert "TECH" in snapshot["sectors"]  # type: ignore[operator]
    snapshot["global"]["market_regime"]["details"]["risk"]["active"] = False  # type: ignore[index]
    assert cache.get_global("market_regime", now=now).details["risk"]["active"] is True  # type: ignore[union-attr,index]

    contract_snapshot = cache.to_context_state_snapshot(
        ticker="aapl",
        sector="tech",
        now=now,
        trace_id="trace_context_state_cache_check",
    )
    assert contract_snapshot.context_snapshot_id
    assert contract_snapshot.ticker == "AAPL"
    assert contract_snapshot.sector == "TECH"
    assert contract_snapshot.risk_level == "HIGH"
    to_json_string(contract_snapshot)

    stale_cache = ContextStateCache(max_entries=20)
    stale_cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="expired_only",
            value="stale",
            severity="HIGH",
            source="manual",
            updated_at=now - timedelta(hours=2),
            valid_until=now - timedelta(hours=1),
        )
    )
    for index in range(12):
        stale_cache.update(
            make_global_context_entry(
                name=f"heartbeat_{index}",
                value="ok",
                severity="INFO",
                source="manual",
                updated_at=now + timedelta(seconds=index),
            )
        )
    stale_snapshot = stale_cache.to_context_state_snapshot(ticker="AAPL", now=now)
    assert stale_snapshot.risk_level == "ELEVATED"
    assert stale_snapshot.highest_severity == "EXPIRED"
    assert stale_snapshot.context_summary["fresh_entry_count"] == 12
    assert stale_snapshot.context_summary["expired_entry_count"] == 1
    assert stale_snapshot.context_summary["expired_context_present"] is True
    assert stale_snapshot.context_summary["stale_context_policy"] == "ELEVATED"
    to_json_string(stale_snapshot)
    assert stale_cache.purge_expired(now=now) == 1
    after_purge = stale_cache.to_context_state_snapshot(ticker="AAPL", now=now)
    assert after_purge.risk_level is None
    assert "expired_context_present" not in after_purge.context_summary

    boundary_cache = ContextStateCache(max_entries=5)
    boundary_cache.update(
        make_ticker_context_entry(
            ticker="MSFT",
            name="boundary",
            value="active",
            severity="LOW",
            source="manual",
            updated_at=now + timedelta(seconds=1),
            valid_until=now,
        )
    )
    assert boundary_cache.get_ticker("MSFT", "boundary", now=now) is not None
    assert boundary_cache.get_ticker(
        "MSFT",
        "boundary",
        now=now + timedelta(microseconds=1),
    ) is None

    print("Context state cache check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
