from __future__ import annotations

from datetime import UTC, datetime, timedelta
import builtins
import inspect
import json
from pathlib import Path
import socket

import pytest

from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.common.time import to_utc_iso
from market_relay_engine.context.decision_context import (
    DEFAULT_POLICY_VERSION,
    KNOWN_SOURCE_CLASSIFICATION,
    SUPPORTED_REFRESH_SOURCE_IDS,
    DecisionContextAssembler,
    DecisionContextError,
    DecisionContextPolicy,
)
from market_relay_engine.context.provenance import attach_provenance
from market_relay_engine.context.refresh_coordinator import (
    ContextRefreshRuntimeState,
    ContextRefreshSourceState,
    ContextRefreshStatus,
)
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    make_global_context_entry,
    make_sector_context_entry,
    make_ticker_context_entry,
)


BASE_TIME = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
SOURCE_TIME = BASE_TIME - timedelta(minutes=10)
VALID_UNTIL = BASE_TIME + timedelta(minutes=20)


def _provenance_details(
    *,
    eligible: bool = True,
    available_at: datetime | None = BASE_TIME - timedelta(minutes=5),
) -> dict[str, object]:
    return attach_provenance(
        {"source_detail": {"kept": True}},
        {
            "source_event_time": SOURCE_TIME,
            "source_observed_at": None,
            "available_at": available_at,
            "collected_at": BASE_TIME - timedelta(minutes=4),
            "effective_from": SOURCE_TIME,
            "valid_until": VALID_UNTIL,
            "availability_basis": "fixture",
            "research_asof_eligible": eligible,
            "revision_id": None,
            "vintage_id": None,
            "source_record_id": "record-1",
        },
    )


def _cache_with_basic_entries() -> ContextStateCache:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="macro_watch",
            value="active",
            source="macro_calendar_v1",
            severity="LOW",
            updated_at=BASE_TIME - timedelta(minutes=1),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            confidence=0.8,
            details=_provenance_details(),
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="XOM",
            name="ticker_award",
            value=True,
            source="usaspending_awards_v1",
            severity="MEDIUM",
            updated_at=BASE_TIME - timedelta(minutes=2),
            details={"event_tier": "TIER_1", "flag_type": "award"},
        )
    )
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="unrelated_ticker",
            value=True,
            source="manual",
            updated_at=BASE_TIME - timedelta(minutes=2),
        )
    )
    cache.update(
        make_sector_context_entry(
            sector="OIL",
            name="sector_wpsr",
            value=1.5,
            source="eia_wpsr_v1",
            severity="HIGH",
            updated_at=BASE_TIME - timedelta(minutes=3),
        )
    )
    cache.update(
        make_sector_context_entry(
            sector="TECH",
            name="unrelated_sector",
            value=1.0,
            source="manual",
            updated_at=BASE_TIME - timedelta(minutes=3),
        )
    )
    return cache


def _assemble(
    cache: ContextStateCache,
    *,
    ticker: str = "XOM",
    ticker_sector: str | None = "OIL",
    mapping: dict[str, str] | None = None,
    state: ContextRefreshRuntimeState | None = None,
    trace_id: str = "trace_1",
    evaluation_time: datetime = BASE_TIME,
    policy: DecisionContextPolicy | None = None,
):
    return DecisionContextAssembler(
        cache=cache,
        policy=policy,
        ticker_sector_by_ticker=mapping,
    ).build_for_decision(
        ticker,
        evaluation_time,
        trace_id,
        state,
        ticker_sector=ticker_sector,
    )


class CountingCache(ContextStateCache):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_calls = 0

    def snapshot(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.snapshot_calls += 1
        return super().snapshot(*args, **kwargs)

    def get(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")

    def get_global(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")

    def get_ticker(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")

    def get_sector(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")

    def latest_global(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")

    def latest_for_ticker(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")

    def latest_for_sector(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache getter must not be called")


class SnapshotOnlyCache(ContextStateCache):
    def __init__(self, snapshot: dict[str, object]) -> None:
        super().__init__()
        self._snapshot = snapshot
        self.snapshot_calls = 0

    def snapshot(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.snapshot_calls += 1
        return json.loads(json.dumps(self._snapshot, allow_nan=False, sort_keys=True))


class UnsafeApprovingPolicy(DecisionContextPolicy):
    def approves(self, *, source: str, cache_scope: str, cache_name: str) -> bool:
        return source == "yfinance_dev_raw_v1" and cache_scope == "GLOBAL" and cache_name == "yfinance_dev"


class UnsafeUnknownApprovingPolicy(DecisionContextPolicy):
    def approves(self, *, source: str, cache_scope: str, cache_name: str) -> bool:
        return source == "fixture_unknown" and cache_scope == "GLOBAL" and cache_name == "unknown"


def _snapshot_entry(
    *,
    scope: str = "GLOBAL",
    name: str = "entry",
    source: str = "manual",
    value: object = "ok",
    updated_at: datetime = BASE_TIME - timedelta(minutes=1),
    ticker: str | None = None,
    sector: str | None = None,
    details: dict[str, object] | None = None,
    expired: bool = False,
) -> dict[str, object]:
    return {
        "scope": scope,
        "ticker": ticker,
        "sector": sector,
        "name": name,
        "value": value,
        "severity": "INFO",
        "source": source,
        "updated_at": to_utc_iso(updated_at),
        "source_event_time": None,
        "valid_until": None,
        "confidence": None,
        "details": {} if details is None else details,
        "trace_id": None,
        "expired": expired,
    }


def _snapshot_with_entries(*entries: dict[str, object]) -> dict[str, object]:
    snapshot: dict[str, object] = {"global": {}, "tickers": {}, "sectors": {}, "entry_count": len(entries)}
    for entry in entries:
        scope = entry["scope"]
        name = entry["name"]
        if scope == "GLOBAL":
            snapshot["global"][name] = entry  # type: ignore[index]
        elif scope == "TICKER":
            snapshot["tickers"].setdefault(entry["ticker"], {})[name] = entry  # type: ignore[index,union-attr]
        elif scope == "SECTOR":
            snapshot["sectors"].setdefault(entry["sector"], {})[name] = entry  # type: ignore[index,union-attr]
    return snapshot


def _runtime_state(*, status: ContextRefreshStatus = ContextRefreshStatus.SUCCESS) -> ContextRefreshRuntimeState:
    return ContextRefreshRuntimeState(
        sources={
            source_id: ContextRefreshSourceState(
                last_status=status,
                last_attempted_at=BASE_TIME - timedelta(minutes=3),
                last_completed_at=BASE_TIME - timedelta(minutes=2),
                last_usable_at=BASE_TIME - timedelta(minutes=2),
                last_full_success_at=BASE_TIME - timedelta(minutes=2),
                last_status_observed_at=BASE_TIME - timedelta(minutes=2),
                next_due_at=BASE_TIME + timedelta(minutes=5),
                consecutive_failure_count=1 if status is ContextRefreshStatus.FAILED else 0,
                consecutive_non_usable_count=0,
            )
            for source_id in SUPPORTED_REFRESH_SOURCE_IDS
        }
    )


def _cache_with_yfinance_dev_entry() -> ContextStateCache:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="yfinance_dev",
            value=101.25,
            source="yfinance_dev_raw_v1",
            updated_at=BASE_TIME,
        )
    )
    return cache


def _cache_with_unknown_entry() -> ContextStateCache:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="unknown",
            value="ok",
            source="fixture_unknown",
            updated_at=BASE_TIME,
        )
    )
    return cache


def _future_runtime_state(
    *,
    status: ContextRefreshStatus | None = ContextRefreshStatus.FAILED,
) -> ContextRefreshRuntimeState:
    return ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_status=status,
                last_attempted_at=BASE_TIME + timedelta(minutes=1),
                last_completed_at=BASE_TIME + timedelta(minutes=2),
                last_usable_at=BASE_TIME + timedelta(minutes=3),
                last_full_success_at=BASE_TIME + timedelta(minutes=4),
                last_status_observed_at=BASE_TIME + timedelta(minutes=5),
                next_due_at=BASE_TIME + timedelta(hours=1),
                consecutive_failure_count=7,
                consecutive_non_usable_count=8,
                last_error_type="AdapterFailed",
                last_error_message="future native error",
            )
        }
    )


def test_naive_evaluation_time_is_rejected() -> None:
    with pytest.raises(DecisionContextError, match="timezone-aware"):
        _assemble(_cache_with_basic_entries(), evaluation_time=datetime(2026, 1, 2, 14, 30))


def test_absent_refresh_state_creates_unknown_readiness_records() -> None:
    context = _assemble(_cache_with_basic_entries(), state=None)

    assert [item.source_id for item in context.source_readiness] == list(SUPPORTED_REFRESH_SOURCE_IDS)
    assert {item.refresh_status for item in context.source_readiness} == {"UNKNOWN_NOT_REFRESHED"}


def test_global_ticker_and_matching_sector_entries_are_selected() -> None:
    context = _assemble(_cache_with_basic_entries())
    names = {entry.cache_name for entry in context.all_structured_context}

    assert {"macro_watch", "ticker_award", "sector_wpsr"}.issubset(names)
    assert "unrelated_ticker" not in names
    assert "unrelated_sector" not in names


def test_explicit_and_injected_sector_resolution() -> None:
    explicit = _assemble(_cache_with_basic_entries(), ticker_sector="oil", mapping={"XOM": "TECH"})
    injected = _assemble(_cache_with_basic_entries(), ticker_sector=None, mapping={"xom": "oil"})

    assert explicit.ticker_sector == "OIL"
    assert explicit.sector_resolution_status == "EXPLICIT"
    assert injected.ticker_sector == "OIL"
    assert injected.sector_resolution_status == "INJECTED_MAPPING"
    assert "sector_wpsr" in {entry.cache_name for entry in injected.all_structured_context}


def test_unresolved_sector_excludes_sector_entries_visibly() -> None:
    context = _assemble(_cache_with_basic_entries(), ticker_sector=None, mapping=None)

    assert context.ticker_sector is None
    assert context.sector_resolution_status == "UNRESOLVED"
    assert "sector_wpsr" not in {entry.cache_name for entry in context.all_structured_context}


def test_expired_entries_are_absent() -> None:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="expired",
            value="old",
            updated_at=BASE_TIME - timedelta(hours=2),
            valid_until=BASE_TIME - timedelta(hours=1),
        )
    )

    context = _assemble(cache)

    assert context.all_structured_context == ()


def test_future_updated_entry_is_excluded_from_context_and_audit() -> None:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="after_eval_entry",
            value="not_yet_available",
            updated_at=BASE_TIME + timedelta(minutes=1),
        )
    )

    context = _assemble(cache)
    audit = context.to_audit_payload()

    assert context.all_structured_context == ()
    assert context.future_entry_exclusion_count == 1
    assert audit.future_entry_exclusion_count == 1
    encoded = to_json_string(audit)
    assert "after_eval_entry" not in encoded
    assert "not_yet_available" not in encoded


def test_unrelated_future_ticker_entry_does_not_increase_exclusion_count() -> None:
    cache = ContextStateCache()
    cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="future_other_ticker",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        )
    )

    context = _assemble(cache)

    assert context.all_structured_context == ()
    assert context.future_entry_exclusion_count == 0


def test_unrelated_future_sector_entry_does_not_increase_exclusion_count() -> None:
    cache = ContextStateCache()
    cache.update(
        make_sector_context_entry(
            sector="TECH",
            name="future_other_sector",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        )
    )

    context = _assemble(cache)

    assert context.all_structured_context == ()
    assert context.future_entry_exclusion_count == 0


def test_unrelated_future_entries_do_not_change_context_identity() -> None:
    base_cache = _cache_with_basic_entries()
    expanded_cache = _cache_with_basic_entries()
    expanded_cache.update(
        make_ticker_context_entry(
            ticker="AAPL",
            name="future_other_ticker",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        )
    )
    expanded_cache.update(
        make_sector_context_entry(
            sector="TECH",
            name="future_other_sector",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        )
    )

    base = _assemble(base_cache, state=_runtime_state())
    expanded = _assemble(expanded_cache, state=_runtime_state())

    assert [entry.to_json_dict() for entry in expanded.all_structured_context] == [
        entry.to_json_dict() for entry in base.all_structured_context
    ]
    assert expanded.future_entry_exclusion_count == base.future_entry_exclusion_count
    assert expanded.context_fingerprint == base.context_fingerprint
    assert expanded.context_snapshot_id == base.context_snapshot_id


def test_relevant_future_entries_increase_exclusion_count() -> None:
    cases = (
        make_global_context_entry(
            name="future_global",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        ),
        make_ticker_context_entry(
            ticker="XOM",
            name="future_ticker",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        ),
        make_sector_context_entry(
            sector="OIL",
            name="future_sector",
            value=True,
            updated_at=BASE_TIME + timedelta(minutes=1),
        ),
    )
    for entry in cases:
        cache = ContextStateCache()
        cache.update(entry)

        context = _assemble(cache)

        assert context.all_structured_context == ()
        assert context.future_entry_exclusion_count == 1


def test_assembly_uses_one_snapshot_and_no_getters() -> None:
    cache = CountingCache()
    cache.update(make_global_context_entry(name="global", value="ok", updated_at=BASE_TIME))

    context = _assemble(cache)

    assert cache.snapshot_calls == 1
    assert [entry.cache_name for entry in context.all_structured_context] == ["global"]


def test_stable_ordering_is_independent_of_insertion_order() -> None:
    def build(order: tuple[str, ...]) -> list[str]:
        cache = ContextStateCache()
        entries = {
            "ticker": make_ticker_context_entry(ticker="XOM", name="ticker", value="ok", updated_at=BASE_TIME),
            "sector": make_sector_context_entry(sector="OIL", name="sector", value="ok", updated_at=BASE_TIME),
            "global": make_global_context_entry(name="global", value="ok", updated_at=BASE_TIME),
        }
        for name in order:
            cache.update(entries[name])
        return [entry.selection_scope + ":" + entry.cache_name for entry in _assemble(cache).all_structured_context]

    assert build(("ticker", "sector", "global")) == build(("global", "sector", "ticker"))


def test_deterministic_fingerprint_and_context_snapshot_id() -> None:
    first = _assemble(_cache_with_basic_entries(), state=_runtime_state())
    second = _assemble(_cache_with_basic_entries(), state=_runtime_state())
    changed_trace = _assemble(_cache_with_basic_entries(), state=_runtime_state(), trace_id="trace_2")

    assert first.context_fingerprint == second.context_fingerprint
    assert first.context_snapshot_id == second.context_snapshot_id
    assert changed_trace.context_fingerprint == first.context_fingerprint
    assert changed_trace.context_snapshot_id != first.context_snapshot_id


def test_semantic_entry_change_changes_fingerprint() -> None:
    first_cache = ContextStateCache()
    second_cache = ContextStateCache()
    first_cache.update(make_global_context_entry(name="global", value="one", updated_at=BASE_TIME))
    second_cache.update(make_global_context_entry(name="global", value="two", updated_at=BASE_TIME))

    assert _assemble(first_cache).context_fingerprint != _assemble(second_cache).context_fingerprint


def test_known_sources_classify_correctly_and_yfinance_is_development_only() -> None:
    cache = ContextStateCache()
    for source in KNOWN_SOURCE_CLASSIFICATION:
        cache.update(make_global_context_entry(name=source, value="ok", source=source, updated_at=BASE_TIME))

    context = _assemble(cache, state=_runtime_state())
    by_source = {entry.source: entry for entry in context.all_structured_context}

    assert by_source["macro_calendar_v1"].resource_family == "MACRO_CALENDAR"
    assert by_source["eia_wpsr_v1"].resource_family == "EIA_WPSR"
    assert by_source["fred_rates_v1"].resource_family == "FRED"
    assert by_source["usaspending_awards_v1"].resource_family == "USASPENDING"
    assert by_source["yfinance_dev_raw_v1"].resource_family == "YFINANCE_DEV"
    assert by_source["yfinance_dev_raw_v1"].source_mode == "DEVELOPMENT_ONLY"
    assert by_source["yfinance_dev_raw_v1"].authority_class == "DEVELOPMENT_ONLY"


def test_yfinance_dev_context_is_visible_but_not_risk_approved_by_default() -> None:
    context = _assemble(_cache_with_yfinance_dev_entry())
    entry = context.all_structured_context[0]

    assert entry.source == "yfinance_dev_raw_v1"
    assert entry.source_mode == "DEVELOPMENT_ONLY"
    assert entry.authority_class == "DEVELOPMENT_ONLY"
    assert context.approved_risk_context == ()


def test_development_only_policy_rule_is_rejected() -> None:
    with pytest.raises(DecisionContextError, match="development-only source cannot be approved"):
        DecisionContextPolicy(
            policy_version="unsafe_policy",
            approved_entry_rules=(
                {
                    "source": "yfinance_dev_raw_v1",
                    "cache_scope": "GLOBAL",
                    "cache_name": "yfinance_dev",
                },
            ),
        )


def test_unsafe_policy_cannot_promote_yfinance_dev_context() -> None:
    context = _assemble(_cache_with_yfinance_dev_entry(), policy=UnsafeApprovingPolicy())
    entry = context.all_structured_context[0]

    assert entry.authority_class == "DEVELOPMENT_ONLY"
    assert context.approved_risk_context == ()


def test_yfinance_development_only_label_is_preserved_in_audit_and_fingerprint() -> None:
    default_context = _assemble(_cache_with_yfinance_dev_entry())
    unsafe_context = _assemble(_cache_with_yfinance_dev_entry(), policy=UnsafeApprovingPolicy())
    audit_json = json.dumps(
        default_context.to_audit_payload().to_json_dict(),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert default_context.context_fingerprint == unsafe_context.context_fingerprint
    assert '"authority_class":"DEVELOPMENT_ONLY"' in audit_json
    assert '"source_mode":"DEVELOPMENT_ONLY"' in audit_json


def test_unknown_source_remains_visible_with_unknown_labels() -> None:
    context = _assemble(_cache_with_unknown_entry())
    entry = context.all_structured_context[0]

    assert entry.resource_family == "UNKNOWN"
    assert entry.source_mode == "UNKNOWN"
    assert entry.authority_class == "RESEARCH_ONLY"
    assert entry.refresh_status == "UNKNOWN_NOT_REFRESHED"
    assert context.approved_risk_context == ()


@pytest.mark.parametrize("source", ["manual", "fixture_unknown", "fred_rate_v1", "future_unregistered_source"])
def test_unknown_policy_rule_is_rejected(source: str) -> None:
    with pytest.raises(DecisionContextError, match="unknown source cannot be approved"):
        DecisionContextPolicy(
            policy_version="unsafe_policy",
            approved_entry_rules=(
                {
                    "source": source,
                    "cache_scope": "GLOBAL",
                    "cache_name": "unknown",
                },
            ),
        )


def test_unsafe_policy_cannot_promote_unknown_source() -> None:
    context = _assemble(_cache_with_unknown_entry(), policy=UnsafeUnknownApprovingPolicy())
    entry = context.all_structured_context[0]

    assert entry.source_mode == "UNKNOWN"
    assert entry.authority_class == "RESEARCH_ONLY"
    assert context.approved_risk_context == ()


def test_unknown_research_only_label_is_preserved_in_audit_and_fingerprint() -> None:
    default_context = _assemble(_cache_with_unknown_entry())
    unsafe_context = _assemble(_cache_with_unknown_entry(), policy=UnsafeUnknownApprovingPolicy())
    audit_json = json.dumps(
        default_context.to_audit_payload().to_json_dict(),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert default_context.context_fingerprint == unsafe_context.context_fingerprint
    assert '"authority_class":"RESEARCH_ONLY"' in audit_json
    assert '"source_mode":"UNKNOWN"' in audit_json


def test_provenance_states_are_safe() -> None:
    snapshot = _snapshot_with_entries(
        _snapshot_entry(
            name="eligible",
            details=_provenance_details(eligible=True, available_at=BASE_TIME - timedelta(minutes=1)),
        ),
        _snapshot_entry(
            name="ineligible",
            details=_provenance_details(eligible=False, available_at=BASE_TIME - timedelta(minutes=1)),
        ),
        _snapshot_entry(name="missing", details={}),
        _snapshot_entry(name="malformed", details={"provenance": {"available_at": "bad"}}),
    )
    context = _assemble(SnapshotOnlyCache(snapshot))
    states = {entry.cache_name: entry.provenance_state for entry in context.all_structured_context}

    assert states == {
        "eligible": "ASOF_ELIGIBLE",
        "ineligible": "ASOF_INELIGIBLE",
        "missing": "MISSING_OR_MALFORMED",
        "malformed": "MISSING_OR_MALFORMED",
    }


@pytest.mark.parametrize(
    "status",
    [
        ContextRefreshStatus.SUCCESS,
        ContextRefreshStatus.PARTIAL,
        ContextRefreshStatus.STALE,
        ContextRefreshStatus.NO_FRESH_DATA,
        ContextRefreshStatus.DATA_DELAYED,
        ContextRefreshStatus.NO_ACTIVE_EVENTS,
        ContextRefreshStatus.SUPERSEDED,
        ContextRefreshStatus.FAILED,
        ContextRefreshStatus.DISABLED,
        ContextRefreshStatus.SKIPPED_NOT_DUE,
    ],
)
def test_pr31_refresh_statuses_remain_distinct(status: ContextRefreshStatus) -> None:
    context = _assemble(_cache_with_basic_entries(), state=_runtime_state(status=status))

    readiness = {item.source_id: item.refresh_status for item in context.source_readiness}
    assert set(readiness.values()) == {status.value}


def test_future_dated_refresh_state_is_masked() -> None:
    readiness = _assemble(_cache_with_basic_entries(), state=_future_runtime_state()).source_readiness[0]
    encoded = json.dumps(readiness.to_json_dict(), allow_nan=False, sort_keys=True)

    assert readiness.source_id == "macro_calendar"
    assert readiness.refresh_status == "UNKNOWN_NOT_REFRESHED"
    assert readiness.last_attempted_at is None
    assert readiness.last_completed_at is None
    assert readiness.last_usable_at is None
    assert readiness.last_full_success_at is None
    assert readiness.next_due_at is None
    assert readiness.last_status_observed_at is None
    assert readiness.consecutive_failure_count is None
    assert readiness.consecutive_non_usable_count is None
    assert readiness.readiness_age_seconds is None
    assert "AdapterFailed" not in encoded
    assert "future native error" not in encoded


@pytest.mark.parametrize(
    "status",
    [
        ContextRefreshStatus.FAILED,
        ContextRefreshStatus.DISABLED,
        ContextRefreshStatus.SKIPPED_NOT_DUE,
    ],
)
def test_future_refresh_state_does_not_expose_native_status(status: ContextRefreshStatus) -> None:
    readiness = _assemble(_cache_with_basic_entries(), state=_future_runtime_state(status=status)).source_readiness[0]
    encoded = json.dumps(readiness.to_json_dict(), allow_nan=False, sort_keys=True)

    assert readiness.refresh_status == "UNKNOWN_NOT_REFRESHED"
    assert status.value not in encoded


def test_unanchored_refresh_state_does_not_expose_last_status() -> None:
    state = ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.FAILED,
                next_due_at=BASE_TIME + timedelta(hours=1),
                consecutive_failure_count=3,
            )
        }
    )

    readiness = _assemble(_cache_with_basic_entries(), state=state).source_readiness[0]

    assert readiness.refresh_status == "UNKNOWN_NOT_REFRESHED"
    assert readiness.last_attempted_at is None
    assert readiness.last_status_observed_at is None
    assert readiness.consecutive_failure_count is None


def test_asof_anchored_refresh_state_preserves_real_status_and_fields() -> None:
    state = ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.FAILED,
                last_attempted_at=BASE_TIME - timedelta(minutes=4),
                last_completed_at=BASE_TIME - timedelta(minutes=3),
                last_usable_at=BASE_TIME - timedelta(minutes=2),
                last_full_success_at=BASE_TIME - timedelta(minutes=1),
                last_status_observed_at=BASE_TIME - timedelta(minutes=1),
                next_due_at=BASE_TIME + timedelta(hours=1),
                consecutive_failure_count=3,
                consecutive_non_usable_count=4,
            )
        }
    )

    readiness = _assemble(_cache_with_basic_entries(), state=state).source_readiness[0]

    assert readiness.refresh_status == ContextRefreshStatus.FAILED.value
    assert readiness.last_status_observed_at == BASE_TIME - timedelta(minutes=1)
    assert readiness.last_attempted_at == BASE_TIME - timedelta(minutes=4)
    assert readiness.last_completed_at == BASE_TIME - timedelta(minutes=3)
    assert readiness.next_due_at == BASE_TIME + timedelta(hours=1)
    assert readiness.consecutive_failure_count == 3
    assert readiness.consecutive_non_usable_count == 4


def test_later_future_status_updates_do_not_change_historical_context_identity() -> None:
    failed = _assemble(_cache_with_basic_entries(), state=_future_runtime_state(status=ContextRefreshStatus.FAILED))
    disabled = _assemble(_cache_with_basic_entries(), state=_future_runtime_state(status=ContextRefreshStatus.DISABLED))
    skipped = _assemble(
        _cache_with_basic_entries(),
        state=_future_runtime_state(status=ContextRefreshStatus.SKIPPED_NOT_DUE),
    )
    legacy = _assemble(
        _cache_with_basic_entries(),
        state=ContextRefreshRuntimeState(
            sources={
                "macro_calendar": ContextRefreshSourceState(
                    last_status=ContextRefreshStatus.FAILED,
                    last_attempted_at=BASE_TIME - timedelta(minutes=4),
                    last_completed_at=BASE_TIME - timedelta(minutes=3),
                    next_due_at=BASE_TIME + timedelta(hours=1),
                    consecutive_failure_count=9,
                )
            }
        ),
    )
    missing = _assemble(_cache_with_basic_entries(), state=None)

    assert failed.source_readiness == disabled.source_readiness
    assert failed.source_readiness == skipped.source_readiness
    assert failed.source_readiness == legacy.source_readiness
    assert failed.source_readiness == missing.source_readiness
    fingerprints = {
        failed.context_fingerprint,
        disabled.context_fingerprint,
        skipped.context_fingerprint,
        legacy.context_fingerprint,
        missing.context_fingerprint,
    }
    snapshot_ids = {
        failed.context_snapshot_id,
        disabled.context_snapshot_id,
        skipped.context_snapshot_id,
        legacy.context_snapshot_id,
        missing.context_snapshot_id,
    }
    assert fingerprints == {
        missing.context_fingerprint
    }
    assert snapshot_ids == {
        missing.context_snapshot_id
    }


def test_future_refresh_state_matches_equivalent_unobserved_future_state() -> None:
    future_failed = _assemble(_cache_with_basic_entries(), state=_future_runtime_state(status=ContextRefreshStatus.FAILED))
    future_unobserved = _assemble(_cache_with_basic_entries(), state=_future_runtime_state(status=None))

    assert future_failed.source_readiness == future_unobserved.source_readiness
    assert future_failed.context_fingerprint == future_unobserved.context_fingerprint


def test_asof_status_observation_changes_fingerprint_through_readiness() -> None:
    unknown = _assemble(_cache_with_basic_entries(), state=None)
    observed = _assemble(_cache_with_basic_entries(), state=_runtime_state(status=ContextRefreshStatus.PARTIAL))

    macro_readiness = observed.source_readiness[0]

    assert macro_readiness.refresh_status == ContextRefreshStatus.PARTIAL.value
    assert macro_readiness.last_status_observed_at == BASE_TIME - timedelta(minutes=2)
    assert observed.context_fingerprint != unknown.context_fingerprint


def test_readiness_age_uses_only_asof_compatible_completion_time() -> None:
    completed_at = BASE_TIME - timedelta(seconds=90)
    state = ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.SUCCESS,
                last_completed_at=completed_at,
                last_status_observed_at=completed_at,
                next_due_at=BASE_TIME + timedelta(hours=1),
            )
        }
    )

    readiness = _assemble(_cache_with_basic_entries(), state=state).source_readiness[0]

    assert readiness.readiness_age_seconds == 90.0


def test_default_policy_preserves_all_entries_and_approves_none() -> None:
    context = _assemble(_cache_with_basic_entries())

    assert context.policy_version == DEFAULT_POLICY_VERSION
    assert context.all_structured_context
    assert context.approved_risk_context == ()


def test_exact_policy_approval_selects_only_exact_entry() -> None:
    policy = DecisionContextPolicy(
        policy_version="test_policy",
        approved_entry_rules=(
            {
                "source": "usaspending_awards_v1",
                "cache_scope": "TICKER",
                "cache_name": "ticker_award",
            },
        ),
    )
    context = _assemble(_cache_with_basic_entries(), policy=policy)

    assert [entry.cache_name for entry in context.approved_risk_context] == ["ticker_award"]
    assert context.approved_risk_context[0].authority_class == "APPROVED_RISK_CONTEXT"
    assert len(context.all_structured_context) > len(context.approved_risk_context)


def test_severity_tier_substring_flag_type_and_provenance_do_not_approve() -> None:
    with pytest.raises(DecisionContextError, match="unknown source cannot be approved"):
        DecisionContextPolicy(
            policy_version="test_policy",
            approved_entry_rules=(
                {
                    "source": "usaspending",
                    "cache_scope": "TICKER",
                    "cache_name": "ticker_award",
                },
            ),
        )

    policy = DecisionContextPolicy(
        policy_version="test_policy",
        approved_entry_rules=(
            {
                "source": "usaspending_awards_v1",
                "cache_scope": "TICKER",
                "cache_name": "award",
            },
        ),
    )
    context = _assemble(_cache_with_basic_entries(), policy=policy)

    assert any(entry.severity == "MEDIUM" for entry in context.all_structured_context)
    assert any(entry.provenance_state == "ASOF_ELIGIBLE" for entry in context.all_structured_context)
    assert context.approved_risk_context == ()


def test_input_detail_mutation_cannot_mutate_assembled_context() -> None:
    details = {"nested": {"value": "original"}}
    cache = ContextStateCache()
    cache.update(make_global_context_entry(name="mutable", value="ok", details=details, updated_at=BASE_TIME))
    context = _assemble(cache)

    details["nested"]["value"] = "changed"  # type: ignore[index]

    assert context.all_structured_context[0].details["nested"]["value"] == "original"  # type: ignore[index]


def test_no_cache_mutation_or_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_with_basic_entries()
    before = cache.snapshot(now=BASE_TIME)

    def blocked_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("assembly must not open files")

    def blocked_socket(*args: object, **kwargs: object) -> object:
        raise AssertionError("assembly must not open sockets")

    monkeypatch.setattr(builtins, "open", blocked_open)
    monkeypatch.setattr(socket, "socket", blocked_socket)

    _assemble(cache)

    assert cache.snapshot(now=BASE_TIME) == before


def test_audit_payload_is_json_safe_and_omits_native_collector_internals() -> None:
    context = _assemble(_cache_with_basic_entries(), state=_runtime_state())
    payload = context.to_audit_payload()
    encoded = json.dumps(payload.to_json_dict(), allow_nan=False, sort_keys=True)

    assert context.context_snapshot_id in encoded
    assert "native_result" not in encoded


def test_raw_context_flag_and_ai_event_inputs_are_not_accepted() -> None:
    signature = inspect.signature(DecisionContextAssembler.build_for_decision)

    assert "context_flags" not in signature.parameters
    assert "context_ai_events" not in signature.parameters
    with pytest.raises(TypeError):
        DecisionContextAssembler(cache=ContextStateCache()).build_for_decision(
            "XOM",
            BASE_TIME,
            "trace",
            None,
            context_flags=(),  # type: ignore[call-arg]
        )


def test_malformed_snapshot_records_are_rejected() -> None:
    snapshot = _snapshot_with_entries(_snapshot_entry(name="expired", expired=True))

    with pytest.raises(DecisionContextError, match="expired"):
        _assemble(SnapshotOnlyCache(snapshot))


def test_decision_context_source_avoids_forbidden_runtime_imports() -> None:
    source = Path("src/market_relay_engine/context/decision_context.py").read_text(encoding="utf-8")
    forbidden = (
        "import requests",
        "urllib",
        "questdb",
        "risk_filter",
        "market_relay_engine.risk",
        "market_relay_engine.execution",
        "market_relay_engine.ai_context",
        "market_relay_engine.model",
    )

    assert not [item for item in forbidden if item in source]


def test_checker_succeeds() -> None:
    from scripts.check_decision_context import run_checks

    assert [result.message for result in run_checks() if not result.ok] == []
