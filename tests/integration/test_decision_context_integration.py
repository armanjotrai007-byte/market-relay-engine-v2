from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from market_relay_engine.context.decision_context import (
    DecisionContextAssembler,
    DecisionContextPolicy,
    UNKNOWN_NOT_REFRESHED,
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


DECISION_TIME = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
SOURCE_TIME = DECISION_TIME - timedelta(minutes=20)
VALID_UNTIL = DECISION_TIME + timedelta(hours=6)
TRACE_ID = "trace_pr33_context_chain"


def test_decision_context_chain_selects_classifies_orders_and_fingerprints() -> None:
    base_cache = _decision_cache(include_unrelated_future=False)
    expanded_cache = _decision_cache(include_unrelated_future=True)
    policy = _approval_policy()
    runtime_state = _runtime_state()

    base = _assemble(base_cache, policy=policy, runtime_state=runtime_state)
    expanded = _assemble(expanded_cache, policy=policy, runtime_state=runtime_state)

    assert base.ticker == "XOM"
    assert base.ticker_sector == "OIL"
    assert base.sector_resolution_status == "EXPLICIT"
    assert base.future_entry_exclusion_count == 1

    assert _ordered_semantics(base) == [
        ("GLOBAL", None, "fred:us_treasury_10y_yield", "fred_rates_v1"),
        ("GLOBAL", None, "macro:active_event", "macro_calendar_v1"),
        ("GLOBAL", None, "manual:operator_note", "manual_research_note_v1"),
        ("SECTOR", "OIL", "eia_wpsr_v1:commercial_crude_inventory:weekly", "eia_wpsr_v1"),
        ("SECTOR", "OIL", "yfinance:XLE:return_5m:5m", "yfinance_dev_raw_v1"),
        ("TICKER", "XOM", "usaspending:contract_award:XOM:CONT_AWD_1", "usaspending_awards_v1"),
    ]
    assert _ordered_semantics(base) == _ordered_semantics(expanded)
    assert base.to_audit_payload().to_json_dict() == expanded.to_audit_payload().to_json_dict()
    assert base.context_fingerprint == expanded.context_fingerprint
    assert base.context_snapshot_id == expanded.context_snapshot_id

    by_name = {entry.cache_name: entry for entry in base.all_structured_context}
    approved_names = [entry.cache_name for entry in base.approved_risk_context]
    assert approved_names == ["fred:us_treasury_10y_yield"]
    assert by_name["fred:us_treasury_10y_yield"] in base.approved_risk_context
    assert by_name["fred:us_treasury_10y_yield"].authority_class == "APPROVED_RISK_CONTEXT"

    yfinance = by_name["yfinance:XLE:return_5m:5m"]
    assert yfinance.source_mode == "DEVELOPMENT_ONLY"
    assert yfinance.authority_class == "DEVELOPMENT_ONLY"
    assert yfinance not in base.approved_risk_context

    unknown = by_name["manual:operator_note"]
    assert unknown.resource_family == "UNKNOWN"
    assert unknown.source_mode == "UNKNOWN"
    assert unknown.authority_class == "RESEARCH_ONLY"
    assert unknown not in base.approved_risk_context

    assert _readiness(base, "fred").refresh_status == ContextRefreshStatus.SUCCESS.value
    _assert_unknown_readiness(_readiness(base, "macro_calendar"))
    _assert_unknown_readiness(_readiness(base, "yfinance_dev_only"))

    reordered = _assemble(
        _decision_cache(include_unrelated_future=False, reverse_order=True),
        policy=policy,
        runtime_state=runtime_state,
    )
    assert _ordered_semantics(reordered) == _ordered_semantics(base)
    assert reordered.context_fingerprint == base.context_fingerprint
    assert reordered.context_snapshot_id == base.context_snapshot_id

    changed_trace = _assemble(
        _decision_cache(include_unrelated_future=False),
        policy=policy,
        runtime_state=runtime_state,
        trace_id="trace_pr33_context_chain_changed",
    )
    assert changed_trace.context_fingerprint == base.context_fingerprint
    assert changed_trace.context_snapshot_id != base.context_snapshot_id

    changed_evidence = _assemble(
        _decision_cache(include_unrelated_future=False, fred_value=4.42),
        policy=policy,
        runtime_state=runtime_state,
    )
    assert changed_evidence.context_fingerprint != base.context_fingerprint

    json.dumps(base.to_audit_payload().to_json_dict(), allow_nan=False, sort_keys=True)


def test_explicit_sector_filtering_is_not_automatic_sector_resolution() -> None:
    cache = ContextStateCache()
    cache.update(
        make_sector_context_entry(
            sector="OIL",
            name="eia_wpsr_v1:commercial_crude_inventory:weekly",
            value=425000.0,
            source="eia_wpsr_v1",
            updated_at=DECISION_TIME - timedelta(minutes=1),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            details=_provenance("eia-sector"),
        )
    )

    unresolved = DecisionContextAssembler(cache=cache).build_for_decision(
        "XOM",
        DECISION_TIME,
        TRACE_ID,
        None,
        ticker_sector=None,
    )
    explicit = DecisionContextAssembler(cache=cache).build_for_decision(
        "XOM",
        DECISION_TIME,
        TRACE_ID,
        None,
        ticker_sector="OIL",
    )

    assert unresolved.sector_resolution_status == "UNRESOLVED"
    assert unresolved.ticker_sector is None
    assert unresolved.all_structured_context == ()
    assert explicit.sector_resolution_status == "EXPLICIT"
    assert explicit.ticker_sector == "OIL"
    assert [entry.cache_name for entry in explicit.all_structured_context] == [
        "eia_wpsr_v1:commercial_crude_inventory:weekly"
    ]


def _assemble(
    cache: ContextStateCache,
    *,
    policy: DecisionContextPolicy,
    runtime_state: ContextRefreshRuntimeState,
    trace_id: str = TRACE_ID,
):
    return DecisionContextAssembler(cache=cache, policy=policy).build_for_decision(
        "XOM",
        DECISION_TIME,
        trace_id,
        runtime_state,
        ticker_sector="OIL",
    )


def _decision_cache(
    *,
    include_unrelated_future: bool,
    reverse_order: bool = False,
    fred_value: float = 4.35,
) -> ContextStateCache:
    entries = [
        make_global_context_entry(
            name="macro:active_event",
            value=True,
            source="macro_calendar_v1",
            updated_at=DECISION_TIME - timedelta(minutes=7),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            details=_provenance("macro-active"),
        ),
        make_global_context_entry(
            name="fred:us_treasury_10y_yield",
            value=fred_value,
            source="fred_rates_v1",
            updated_at=DECISION_TIME - timedelta(minutes=6),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            details=_provenance("fred-10y"),
        ),
        make_sector_context_entry(
            sector="OIL",
            name="eia_wpsr_v1:commercial_crude_inventory:weekly",
            value=425000.0,
            source="eia_wpsr_v1",
            updated_at=DECISION_TIME - timedelta(minutes=5),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            details=_provenance("eia-crude"),
        ),
        make_ticker_context_entry(
            ticker="XOM",
            name="usaspending:contract_award:XOM:CONT_AWD_1",
            value="NEW_AWARD_DISCOVERED",
            source="usaspending_awards_v1",
            updated_at=DECISION_TIME - timedelta(minutes=4),
            source_event_time=SOURCE_TIME,
            valid_until=None,
            details=_provenance("usaspending-award", valid_until=None),
        ),
        make_sector_context_entry(
            sector="OIL",
            name="yfinance:XLE:return_5m:5m",
            value=0.012,
            source="yfinance_dev_raw_v1",
            updated_at=DECISION_TIME - timedelta(minutes=3),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            details=_provenance("yfinance-dev"),
        ),
        make_global_context_entry(
            name="manual:operator_note",
            value="visible",
            source="manual_research_note_v1",
            updated_at=DECISION_TIME - timedelta(minutes=2),
            source_event_time=SOURCE_TIME,
            valid_until=VALID_UNTIL,
            details={"note": "unknown source remains research-only"},
        ),
        make_global_context_entry(
            name="future:relevant_macro",
            value=True,
            source="macro_calendar_v1",
            updated_at=DECISION_TIME + timedelta(minutes=1),
            source_event_time=DECISION_TIME + timedelta(minutes=1),
            valid_until=VALID_UNTIL,
            details=_provenance(
                "future-relevant",
                available_at=DECISION_TIME + timedelta(minutes=1),
                source_event_time=DECISION_TIME + timedelta(minutes=1),
            ),
        ),
    ]
    if include_unrelated_future:
        entries.extend(
            [
                make_ticker_context_entry(
                    ticker="AAPL",
                    name="future:unrelated_ticker",
                    value=True,
                    source="usaspending_awards_v1",
                    updated_at=DECISION_TIME + timedelta(minutes=2),
                    source_event_time=DECISION_TIME + timedelta(minutes=2),
                    details=_provenance(
                        "future-other-ticker",
                        available_at=DECISION_TIME + timedelta(minutes=2),
                        source_event_time=DECISION_TIME + timedelta(minutes=2),
                        valid_until=None,
                    ),
                ),
                make_sector_context_entry(
                    sector="TECH",
                    name="future:unrelated_sector",
                    value=True,
                    source="yfinance_dev_raw_v1",
                    updated_at=DECISION_TIME + timedelta(minutes=2),
                    source_event_time=DECISION_TIME + timedelta(minutes=2),
                    details=_provenance(
                        "future-other-sector",
                        available_at=DECISION_TIME + timedelta(minutes=2),
                        source_event_time=DECISION_TIME + timedelta(minutes=2),
                        valid_until=None,
                    ),
                ),
            ]
        )
    if reverse_order:
        entries = list(reversed(entries))
    cache = ContextStateCache()
    for entry in entries:
        cache.update(entry)
    return cache


def _approval_policy() -> DecisionContextPolicy:
    return DecisionContextPolicy(
        policy_version="pr33_test_policy",
        approved_entry_rules=(
            {
                "source": "fred_rates_v1",
                "cache_scope": "GLOBAL",
                "cache_name": "fred:us_treasury_10y_yield",
            },
        ),
    )


def _runtime_state() -> ContextRefreshRuntimeState:
    return ContextRefreshRuntimeState(
        sources={
            "fred": ContextRefreshSourceState(
                last_attempted_at=DECISION_TIME - timedelta(minutes=10),
                last_completed_at=DECISION_TIME - timedelta(minutes=9),
                last_usable_at=DECISION_TIME - timedelta(minutes=9),
                last_full_success_at=DECISION_TIME - timedelta(minutes=9),
                last_status=ContextRefreshStatus.SUCCESS,
                last_status_observed_at=DECISION_TIME - timedelta(minutes=9),
                next_due_at=DECISION_TIME + timedelta(hours=1),
            ),
            "macro_calendar": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.SUCCESS,
                last_attempted_at=DECISION_TIME - timedelta(minutes=5),
                next_due_at=DECISION_TIME + timedelta(hours=1),
                consecutive_failure_count=2,
            ),
            "yfinance_dev_only": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.FAILED,
                last_attempted_at=DECISION_TIME + timedelta(minutes=1),
                last_status_observed_at=DECISION_TIME + timedelta(minutes=1),
                next_due_at=DECISION_TIME + timedelta(hours=1),
                consecutive_failure_count=3,
                consecutive_non_usable_count=4,
                last_error_type="FutureError",
                last_error_message="future state must be hidden",
            ),
        }
    )


def _provenance(
    record_id: str,
    *,
    available_at: datetime = DECISION_TIME - timedelta(minutes=30),
    source_event_time: datetime = SOURCE_TIME,
    valid_until: datetime | None = VALID_UNTIL,
) -> dict[str, object]:
    return attach_provenance(
        {"record_id": record_id},
        {
            "source_event_time": source_event_time,
            "source_observed_at": None,
            "available_at": available_at,
            "collected_at": DECISION_TIME - timedelta(minutes=25),
            "effective_from": source_event_time,
            "valid_until": valid_until,
            "availability_basis": "fixture",
            "research_asof_eligible": True,
            "revision_id": None,
            "vintage_id": None,
            "source_record_id": record_id,
        },
    )


def _ordered_semantics(context: object) -> list[tuple[str, str | None, str, str]]:
    return [
        (entry.cache_scope, entry.scope_target, entry.cache_name, entry.source)
        for entry in context.all_structured_context
    ]


def _readiness(context: object, source_id: str):
    return next(item for item in context.source_readiness if item.source_id == source_id)


def _assert_unknown_readiness(readiness: object) -> None:
    assert readiness.refresh_status == UNKNOWN_NOT_REFRESHED
    assert readiness.last_attempted_at is None
    assert readiness.last_completed_at is None
    assert readiness.last_usable_at is None
    assert readiness.last_full_success_at is None
    assert readiness.next_due_at is None
    assert readiness.last_status_observed_at is None
    assert readiness.consecutive_failure_count is None
    assert readiness.consecutive_non_usable_count is None
    assert readiness.readiness_age_seconds is None
