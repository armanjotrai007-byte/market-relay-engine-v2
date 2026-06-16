from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
import inspect
import json

import pytest

from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.context import state_cache
from market_relay_engine.context.state_cache import (
    ContextScope,
    ContextStateCache,
    ContextStateCacheError,
    ContextStateEntry,
    ContextStateKey,
    ContextStateUpdateStatus,
    make_global_context_entry,
    make_sector_context_entry,
    make_ticker_context_entry,
)


BASE_TIME = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def test_key_validation_and_scope_normalization() -> None:
    global_key = ContextStateKey(scope="GLOBAL", name=" market_regime ")
    assert global_key.scope is ContextScope.GLOBAL
    assert global_key.name == "market_regime"

    ticker_key = ContextStateKey(scope=ContextScope.TICKER, ticker="aapl", name="earnings")
    assert ticker_key.ticker == "AAPL"
    assert ticker_key.sector is None

    sector_key = ContextStateKey(scope=ContextScope.SECTOR, sector="tech", name="proxy")
    assert sector_key.sector == "TECH"
    assert sector_key.ticker is None

    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.GLOBAL, ticker="AAPL", name="bad")
    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.GLOBAL, sector="TECH", name="bad")
    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.TICKER, name="missing")
    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.TICKER, ticker="AAPL", sector="TECH", name="bad")
    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.SECTOR, name="missing")
    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.SECTOR, sector="TECH", ticker="AAPL", name="bad")
    with pytest.raises(ContextStateCacheError):
        ContextStateKey(scope=ContextScope.GLOBAL, name="")


def test_entry_validation_and_source_event_time_normalization() -> None:
    source_time = datetime(2026, 1, 2, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    entry = make_global_context_entry(
        name="macro_event_risk",
        value="active",
        severity="high",
        source=" calendar ",
        updated_at=BASE_TIME,
        source_event_time=source_time,
        valid_until=BASE_TIME + timedelta(minutes=10),
        confidence=0.75,
        details={"nested": {"ok": True}},
        trace_id="trace_entry",
    )

    assert entry.severity == "HIGH"
    assert entry.source == "calendar"
    assert entry.source_event_time == datetime(2026, 1, 2, 14, 0, tzinfo=UTC)
    assert entry.updated_at == BASE_TIME
    assert entry.details == {"nested": {"ok": True}}

    key = ContextStateKey(scope=ContextScope.GLOBAL, name="market_regime")
    invalid_kwargs = [
        {"value": ""},
        {"severity": "urgent"},
        {"source": ""},
        {"updated_at": datetime(2026, 1, 2, 14, 30)},
        {"source_event_time": datetime(2026, 1, 2, 14, 30)},
        {"valid_until": datetime(2026, 1, 2, 14, 30)},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"details": {"bad": object()}},
        {"details": {"bad": float("nan")}},
        {"trace_id": ""},
    ]
    for kwargs in invalid_kwargs:
        values = {
            "key": key,
            "value": "risk_off",
            "updated_at": BASE_TIME,
        }
        values.update(kwargs)
        with pytest.raises(ContextStateCacheError):
            ContextStateEntry(**values)


def test_valid_until_is_independent_deadline_and_expiry_boundary() -> None:
    before_deadline = BASE_TIME - timedelta(minutes=5)
    equal_deadline = BASE_TIME
    after_deadline = BASE_TIME + timedelta(minutes=5)

    before_entry = make_global_context_entry(
        name="deadline_before",
        value="accepted",
        updated_at=BASE_TIME,
        valid_until=before_deadline,
    )
    equal_entry = make_global_context_entry(
        name="deadline_equal",
        value="accepted",
        updated_at=BASE_TIME,
        valid_until=equal_deadline,
    )
    after_entry = make_global_context_entry(
        name="deadline_after",
        value="accepted",
        updated_at=BASE_TIME,
        valid_until=after_deadline,
    )

    assert before_entry.updated_at == BASE_TIME
    assert before_entry.valid_until == before_deadline
    assert equal_entry.valid_until == equal_deadline
    assert after_entry.valid_until == after_deadline

    cache = ContextStateCache()
    already_expired = make_ticker_context_entry(
        ticker="AAPL",
        name="delayed_expired_risk",
        value="stale",
        severity="HIGH",
        updated_at=BASE_TIME,
        valid_until=before_deadline,
    )
    assert cache.update(already_expired).status is ContextStateUpdateStatus.WRITTEN
    assert cache.get_ticker("AAPL", "delayed_expired_risk", now=BASE_TIME) is None
    included = cache.get_ticker(
        "AAPL",
        "delayed_expired_risk",
        now=BASE_TIME,
        include_expired=True,
    )
    assert included is not None
    assert included.updated_at == BASE_TIME
    assert included.valid_until == before_deadline

    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)
    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.highest_severity == "EXPIRED"
    assert snapshot.context_summary["expired_entry_count"] == 1

    boundary_cache = ContextStateCache()
    boundary_cache.update(
        make_ticker_context_entry(
            ticker="MSFT",
            name="boundary_risk",
            value="active",
            severity="LOW",
            updated_at=BASE_TIME + timedelta(seconds=1),
            valid_until=BASE_TIME,
        )
    )
    assert boundary_cache.get_ticker("MSFT", "boundary_risk", now=BASE_TIME) is not None
    assert boundary_cache.get_ticker(
        "MSFT",
        "boundary_risk",
        now=BASE_TIME + timedelta(microseconds=1),
    ) is None


def test_cache_config_validation() -> None:
    with pytest.raises(ContextStateCacheError):
        ContextStateCache(max_entries=0)
    with pytest.raises(ContextStateCacheError):
        ContextStateCache(max_entries=True)
    with pytest.raises(ContextStateCacheError):
        ContextStateCache(purge_every_updates=0)
    with pytest.raises(ContextStateCacheError):
        ContextStateCache(purge_every_updates=True)


def test_update_statuses_do_not_raise_for_stale_or_duplicate_updates() -> None:
    cache = ContextStateCache(max_entries=10, purge_every_updates=100)
    first = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="active",
        updated_at=BASE_TIME,
        details={"reason": "earnings"},
    )
    assert cache.update(first).status is ContextStateUpdateStatus.WRITTEN

    newer = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="cooling",
        updated_at=BASE_TIME + timedelta(seconds=1),
        details={"reason": "post_call"},
    )
    assert cache.update(newer).status is ContextStateUpdateStatus.REPLACED

    stale = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="stale",
        updated_at=BASE_TIME - timedelta(seconds=1),
    )
    stale_result = cache.update(stale)
    assert stale_result.status is ContextStateUpdateStatus.IGNORED_STALE
    assert cache.get_ticker("AAPL", "earnings_risk", now=BASE_TIME).value == "cooling"  # type: ignore[union-attr]

    duplicate = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="cooling",
        updated_at=BASE_TIME + timedelta(seconds=1),
        details={"reason": "post_call"},
    )
    assert cache.update(duplicate).status is ContextStateUpdateStatus.IGNORED_DUPLICATE

    metadata_only = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="cooling",
        updated_at=BASE_TIME + timedelta(seconds=1),
        confidence=0.25,
        valid_until=BASE_TIME + timedelta(minutes=5),
        details={"reason": "post_call"},
        trace_id="trace_changed",
    )
    assert cache.update(metadata_only).status is ContextStateUpdateStatus.IGNORED_DUPLICATE

    same_time_changed = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="active_again",
        updated_at=BASE_TIME + timedelta(seconds=1),
        details={"reason": "new_value"},
    )
    assert cache.update(same_time_changed).status is ContextStateUpdateStatus.REPLACED
    assert cache.get_ticker("AAPL", "earnings_risk", now=BASE_TIME).value == "active_again"  # type: ignore[union-attr]


def test_same_timestamp_severity_change_replaces_and_updates_risk_level() -> None:
    cache = ContextStateCache()
    low_entry = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="active",
        severity="LOW",
        updated_at=BASE_TIME,
        details={"reason": "same_event"},
    )
    assert cache.update(low_entry).status is ContextStateUpdateStatus.WRITTEN

    identical = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="active",
        severity="LOW",
        updated_at=BASE_TIME,
        details={"reason": "same_event"},
    )
    assert cache.update(identical).status is ContextStateUpdateStatus.IGNORED_DUPLICATE

    high_entry = make_ticker_context_entry(
        ticker="AAPL",
        name="earnings_risk",
        value="active",
        severity="HIGH",
        updated_at=BASE_TIME,
        details={"reason": "same_event"},
    )
    assert cache.update(high_entry).status is ContextStateUpdateStatus.REPLACED
    assert cache.get_ticker("AAPL", "earnings_risk", now=BASE_TIME).severity == "HIGH"  # type: ignore[union-attr]
    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)
    assert snapshot.highest_severity == "HIGH"
    assert snapshot.risk_level == "HIGH"


def test_periodic_purge_removes_expired_entries() -> None:
    cache = ContextStateCache(max_entries=10, purge_every_updates=2)
    expired = make_global_context_entry(
        name="macro_event_risk",
        value="expired",
        updated_at=BASE_TIME - timedelta(hours=2),
        valid_until=BASE_TIME - timedelta(hours=1),
    )
    assert cache.update(expired).status is ContextStateUpdateStatus.WRITTEN
    assert cache.get_global("macro_event_risk", now=BASE_TIME) is None
    assert cache.get_global("macro_event_risk", now=BASE_TIME, include_expired=True) is not None

    fresh = make_global_context_entry(
        name="market_regime",
        value="risk_off",
        updated_at=BASE_TIME,
    )
    result = cache.update(fresh)
    assert result.purged_expired_count == 1
    assert cache.get_global("macro_event_risk", now=BASE_TIME, include_expired=True) is None


def test_max_entries_eviction_removes_oldest_with_key_tie_break() -> None:
    cache = ContextStateCache(max_entries=2, purge_every_updates=100)
    for name in ("beta", "alpha", "gamma"):
        result = cache.update(
            make_global_context_entry(name=name, value=name, updated_at=BASE_TIME)
        )

    assert result.evicted_count == 1
    assert cache.get_global("alpha", now=BASE_TIME, include_expired=True) is None
    assert cache.get_global("beta", now=BASE_TIME) is not None
    assert cache.get_global("gamma", now=BASE_TIME) is not None


def test_details_are_deep_copied_on_entry_cache_and_snapshot_extraction() -> None:
    details: dict[str, object] = {"nested": {"values": [1]}}
    entry = make_global_context_entry(
        name="market_regime",
        value="risk_off",
        updated_at=BASE_TIME,
        details=details,
    )
    details["nested"]["values"].append(2)  # type: ignore[index,union-attr]
    assert entry.details == {"nested": {"values": [1]}}

    cache = ContextStateCache()
    cache.update(entry)
    entry.details["nested"]["values"].append(3)  # type: ignore[index,union-attr]
    assert cache.get_global("market_regime", now=BASE_TIME).details == {"nested": {"values": [1]}}  # type: ignore[union-attr]

    extracted = cache.get_global("market_regime", now=BASE_TIME)
    assert extracted is not None
    extracted.details["nested"]["values"].append(4)  # type: ignore[index,union-attr]
    assert cache.get_global("market_regime", now=BASE_TIME).details == {"nested": {"values": [1]}}  # type: ignore[union-attr]

    snapshot = cache.snapshot(now=BASE_TIME)
    snapshot["global"]["market_regime"]["details"]["nested"]["values"].append(5)  # type: ignore[index,union-attr]
    assert cache.get_global("market_regime", now=BASE_TIME).details == {"nested": {"values": [1]}}  # type: ignore[union-attr]


def test_latest_read_methods_return_defensive_entry_copies() -> None:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="market_regime",
            value="risk_off",
            updated_at=BASE_TIME,
            details={"nested": {"x": "global"}},
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="earnings_risk",
            value="active",
            updated_at=BASE_TIME,
            details={"nested": {"x": "ticker"}},
        )
    )
    cache.update(
        make_sector_context_entry(
            sector="TECH",
            name="sector_proxy",
            value="weak",
            updated_at=BASE_TIME,
            details={"nested": {"x": "sector"}},
        )
    )

    latest_global = cache.latest_global(now=BASE_TIME)[0]
    latest_global.details["nested"]["x"] = "mutated"  # type: ignore[index]
    assert cache.latest_global(now=BASE_TIME)[0].details["nested"]["x"] == "global"  # type: ignore[index]

    latest_ticker = cache.latest_for_ticker("AAPL", now=BASE_TIME)[0]
    latest_ticker.details["nested"]["x"] = "mutated"  # type: ignore[index]
    assert cache.latest_for_ticker("AAPL", now=BASE_TIME)[0].details["nested"]["x"] == "ticker"  # type: ignore[index]

    latest_sector = cache.latest_for_sector("TECH", now=BASE_TIME)[0]
    latest_sector.details["nested"]["x"] = "mutated"  # type: ignore[index]
    assert cache.latest_for_sector("TECH", now=BASE_TIME)[0].details["nested"]["x"] == "sector"  # type: ignore[index]


def test_sector_scope_lookups_and_snapshot() -> None:
    cache = ContextStateCache()
    cache.update(
        make_sector_context_entry(
            sector="tech",
            name="sector_proxy",
            value="weak",
            severity="MEDIUM",
            updated_at=BASE_TIME,
        )
    )
    cache.update(
        make_sector_context_entry(
            sector="ENERGY",
            name="sector_proxy",
            value="stable",
            severity="LOW",
            updated_at=BASE_TIME,
        )
    )

    tech_entry = cache.get_sector("TECH", "sector_proxy", now=BASE_TIME)
    assert tech_entry is not None
    assert tech_entry.key.sector == "TECH"
    assert tech_entry.value == "weak"
    assert [entry.key.sector for entry in cache.latest_for_sector("tech", now=BASE_TIME)] == ["TECH"]

    snapshot = cache.snapshot(now=BASE_TIME)
    assert snapshot["sectors"]["TECH"]["sector_proxy"]["value"] == "weak"  # type: ignore[index]
    assert snapshot["sectors"]["ENERGY"]["sector_proxy"]["value"] == "stable"  # type: ignore[index]
    to_json_string(snapshot)


def test_context_state_snapshot_risk_level_mapping() -> None:
    cases = [
        ("CRITICAL", "HIGH"),
        ("HIGH", "HIGH"),
        ("MEDIUM", "ELEVATED"),
        ("LOW", "LOW"),
        ("INFO", None),
    ]
    for severity, expected_risk_level in cases:
        cache = ContextStateCache()
        cache.update(
            make_global_context_entry(
                name="market_regime",
                value=severity.lower(),
                severity=severity,
                updated_at=BASE_TIME,
            )
        )
        snapshot = cache.to_context_state_snapshot(ticker="aapl", now=BASE_TIME)
        assert snapshot.context_snapshot_id
        assert snapshot.risk_level == expected_risk_level

    empty_snapshot = ContextStateCache().to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)
    assert empty_snapshot.risk_level is None
    assert empty_snapshot.highest_severity is None
    assert empty_snapshot.context_summary["entry_count"] == 0
    assert "expired_context_present" not in empty_snapshot.context_summary


def test_context_state_snapshot_aggregates_global_sector_and_ticker_entries() -> None:
    cache = ContextStateCache()
    global_valid_until = BASE_TIME + timedelta(minutes=10)
    ticker_valid_until = BASE_TIME + timedelta(minutes=5)
    cache.update(
        make_global_context_entry(
            name="macro_event_risk",
            value="watch",
            severity="LOW",
            updated_at=BASE_TIME,
            valid_until=global_valid_until,
        )
    )
    cache.update(
        make_sector_context_entry(
            sector="tech",
            name="sector_proxy",
            value="weak",
            severity="MEDIUM",
            updated_at=BASE_TIME + timedelta(seconds=1),
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="aapl",
            name="earnings_risk",
            value="active",
            severity="HIGH",
            updated_at=BASE_TIME + timedelta(seconds=2),
            valid_until=ticker_valid_until,
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="expired_risk",
            value="old",
            severity="CRITICAL",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(
        ticker="aapl",
        sector="tech",
        now=BASE_TIME,
        trace_id="trace_aggregate",
    )

    assert snapshot.ticker == "AAPL"
    assert snapshot.sector == "TECH"
    assert snapshot.trace_id == "trace_aggregate"
    assert snapshot.highest_severity == "HIGH"
    assert snapshot.risk_level == "HIGH"
    assert snapshot.valid_until == ticker_valid_until
    assert snapshot.context_summary["entry_count"] == 3
    assert snapshot.context_summary["fresh_entry_count"] == 3
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_context_present"] is True
    assert snapshot.context_summary["stale_context_policy"] == "ELEVATED"
    assert snapshot.context_summary["expired_entries"][0]["name"] == "expired_risk"
    assert "macro_event_risk" in snapshot.context_summary["global"]
    assert "TECH" in snapshot.context_summary["sectors"]
    assert "AAPL" in snapshot.context_summary["tickers"]
    assert "expired_risk" not in snapshot.context_summary["tickers"]["AAPL"]
    json.dumps(snapshot.context_summary, allow_nan=False, sort_keys=True)


def test_context_state_snapshot_only_expired_ticker_context_is_elevated() -> None:
    cache = ContextStateCache()
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="earnings_risk",
            value="expired",
            severity="HIGH",
            updated_at=BASE_TIME,
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)

    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.highest_severity == "EXPIRED"
    assert snapshot.context_summary["entry_count"] == 0
    assert snapshot.context_summary["fresh_entry_count"] == 0
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_context_present"] is True
    assert snapshot.context_summary["stale_context_policy"] == "ELEVATED"
    assert snapshot.context_summary["expired_entries"][0]["name"] == "earnings_risk"
    assert snapshot.context_summary["expired_entries"][0]["expired"] is True


def test_context_state_snapshot_expired_ticker_with_fresh_global_info_is_elevated() -> None:
    cache = ContextStateCache()
    heartbeat_valid_until = BASE_TIME + timedelta(minutes=10)
    cache.update(
        make_global_context_entry(
            name="heartbeat",
            value="ok",
            severity="INFO",
            updated_at=BASE_TIME,
            valid_until=heartbeat_valid_until,
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="earnings_risk",
            value="stale_high",
            severity="HIGH",
            updated_at=BASE_TIME,
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)

    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.highest_severity == "EXPIRED"
    assert snapshot.valid_until == heartbeat_valid_until
    assert snapshot.context_summary["entry_count"] == 1
    assert snapshot.context_summary["fresh_entry_count"] == 1
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_context_present"] is True
    assert snapshot.context_summary["global"]["heartbeat"]["expired"] is False
    assert "AAPL" not in snapshot.context_summary["tickers"]
    assert snapshot.context_summary["expired_entries"][0]["name"] == "earnings_risk"


def test_context_state_snapshot_expired_sector_with_fresh_ticker_low_is_elevated() -> None:
    cache = ContextStateCache()
    fresh_valid_until = BASE_TIME + timedelta(minutes=8)
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="position_quality",
            value="acceptable",
            severity="LOW",
            updated_at=BASE_TIME,
            valid_until=fresh_valid_until,
        )
    )
    cache.update(
        make_sector_context_entry(
            sector="TECH",
            name="sector_proxy",
            value="stale_high",
            severity="HIGH",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(
        ticker="AAPL",
        sector="tech",
        now=BASE_TIME,
    )

    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.highest_severity == "EXPIRED"
    assert snapshot.valid_until == fresh_valid_until
    assert snapshot.context_summary["entry_count"] == 1
    assert snapshot.context_summary["fresh_entry_count"] == 1
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_entries"][0]["sector"] == "TECH"
    assert "TECH" not in snapshot.context_summary["sectors"]


def test_context_state_snapshot_expired_ticker_with_fresh_medium_keeps_real_severity() -> None:
    cache = ContextStateCache()
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="fresh_risk",
            value="watch",
            severity="MEDIUM",
            updated_at=BASE_TIME,
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="expired_risk",
            value="old_high",
            severity="HIGH",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)

    assert snapshot.highest_severity == "MEDIUM"
    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.context_summary["fresh_entry_count"] == 1
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_context_present"] is True
    assert "expired_risk" not in snapshot.context_summary["tickers"]["AAPL"]


def test_context_state_snapshot_expired_ticker_with_fresh_high_remains_high() -> None:
    cache = ContextStateCache()
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="fresh_risk",
            value="active",
            severity="HIGH",
            updated_at=BASE_TIME,
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="expired_risk",
            value="old_high",
            severity="HIGH",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)

    assert snapshot.highest_severity == "HIGH"
    assert snapshot.risk_level == "HIGH"
    assert snapshot.context_summary["fresh_entry_count"] == 1
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_entries"][0]["name"] == "expired_risk"


def test_context_state_snapshot_expired_global_only_context_is_elevated() -> None:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="market_regime",
            value="expired",
            severity="HIGH",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(ticker="AAPL", now=BASE_TIME)

    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.highest_severity == "EXPIRED"
    assert snapshot.context_summary["expired_context_present"] is True
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_entries"][0]["scope"] == "GLOBAL"


def test_context_state_snapshot_expired_sector_context_is_elevated() -> None:
    cache = ContextStateCache()
    cache.update(
        make_sector_context_entry(
            sector="TECH",
            name="sector_proxy",
            value="expired",
            severity="MEDIUM",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    snapshot = cache.to_context_state_snapshot(
        ticker="AAPL",
        sector="tech",
        now=BASE_TIME,
    )

    assert snapshot.risk_level == "ELEVATED"
    assert snapshot.highest_severity == "EXPIRED"
    assert snapshot.context_summary["expired_context_present"] is True
    assert snapshot.context_summary["expired_entry_count"] == 1
    assert snapshot.context_summary["expired_entries"][0]["sector"] == "TECH"


def test_clear_and_purge_expired() -> None:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="market_regime",
            value="risk_off",
            updated_at=BASE_TIME,
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="stale",
            value="old",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )
    assert cache.purge_expired(now=BASE_TIME) == 1
    assert cache.snapshot(now=BASE_TIME)["entry_count"] == 1
    cache.clear()
    assert cache.snapshot(now=BASE_TIME)["entry_count"] == 0


def test_basic_concurrent_read_write_smoke() -> None:
    cache = ContextStateCache(max_entries=20, purge_every_updates=5)

    def write_entry(index: int) -> None:
        cache.update(
            make_ticker_context_entry(
                ticker="AAPL",
                name=f"risk_{index % 5}",
                value=str(index),
                updated_at=BASE_TIME + timedelta(seconds=index),
            )
        )

    def read_snapshot(_index: int) -> dict[str, object]:
        return cache.snapshot(now=BASE_TIME + timedelta(minutes=1))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_entry, range(40)))
        snapshots = list(executor.map(read_snapshot, range(10)))

    assert snapshots
    assert cache.snapshot(now=BASE_TIME + timedelta(minutes=1))["entry_count"] <= 5


def test_state_cache_source_has_no_external_integration_terms() -> None:
    source = inspect.getsource(state_cache).lower()
    for forbidden in (
        "questdb",
        "alpaca",
        "requests",
        "urllib",
        "yfinance",
        "openai",
        "fred",
        "usaspend",
        "scheduler",
        "background",
    ):
        assert forbidden not in source
