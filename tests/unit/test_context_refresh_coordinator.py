from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import json

import pandas as pd
import pytest

from market_relay_engine.context.eia_wpsr import (
    EIARelease,
    EIAWPSRActionKind,
    EIAWPSRActionPlan,
    EIAWPSRCollectionResult,
    EIAWPSRCollectionStatus,
    EIAWPSRCollector,
    EIAWPSRConfig,
    EIAWPSRDataStatus,
)
from market_relay_engine.context.macro_calendar import (
    MacroCalendar,
    MacroCalendarCollectionResult,
    MacroCalendarCollectionStatus,
    MacroCalendarEvent,
    MacroWindowProfile,
)
from market_relay_engine.context.refresh_coordinator import (
    ContextRefreshAdapterResult,
    ContextRefreshCoordinator,
    ContextRefreshError,
    ContextRefreshPolicy,
    ContextRefreshRuntimeState,
    ContextRefreshSourcePolicy,
    ContextRefreshSourceState,
    ContextRefreshStatus,
    EIAWPSRRefreshAdapter,
    MacroCalendarRefreshAdapter,
    SUPPORTED_SOURCE_IDS,
    YFinanceRefreshAdapter,
    next_yfinance_bar_due_at,
)
from market_relay_engine.context.state_cache import ContextStateCache
from market_relay_engine.context.yfinance_proxy import (
    YFinanceProxyCollectionResult,
    YFinanceProxyCollectionStatus,
    YFinanceProxyCollector,
    YFinanceProxyConfig,
    build_proxy_registry,
)


BASE_TIME = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


class FakeAdapter:
    def __init__(
        self,
        source_id: str,
        *,
        enabled: bool = True,
        status: ContextRefreshStatus = ContextRefreshStatus.SUCCESS,
        usable_context: bool = True,
        next_due_at: datetime | None = None,
        native_result: object | None = None,
        adapter_state: dict[str, object] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.source_id = source_id
        self.enabled = enabled
        self.status = status
        self.usable_context = usable_context
        self.next_due_at = next_due_at
        self.native_result = native_result
        self.adapter_state = {} if adapter_state is None else adapter_state
        self.exception = exception
        self.calls: list[dict[str, object]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def run_once(
        self,
        evaluation_time: datetime,
        source_state: ContextRefreshSourceState,
        *,
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> ContextRefreshAdapterResult:
        self.calls.append(
            {
                "evaluation_time": evaluation_time,
                "source_state": source_state,
                "write_questdb": write_questdb,
                "questdb_required": questdb_required,
                "run_id": run_id,
                "session_id": session_id,
            }
        )
        if self.exception is not None:
            raise self.exception
        return ContextRefreshAdapterResult(
            status=self.status,
            usable_context=self.usable_context,
            next_due_at=self.next_due_at,
            adapter_state=self.adapter_state,
            native_result=self.native_result,
        )


def _policy(
    *,
    order: tuple[str, ...] = SUPPORTED_SOURCE_IDS,
    interval: int = 60,
) -> ContextRefreshPolicy:
    return ContextRefreshPolicy(
        schema_version=1,
        source_order=order,
        sources={
            source_id: ContextRefreshSourcePolicy(
                source_id=source_id,
                fallback_interval_seconds=interval,
            )
            for source_id in SUPPORTED_SOURCE_IDS
        },
    )


def _coordinator(*adapters: FakeAdapter, policy: ContextRefreshPolicy | None = None) -> ContextRefreshCoordinator:
    supplied = {adapter.source_id: adapter for adapter in adapters}
    full = [
        supplied.get(source_id, FakeAdapter(source_id))
        for source_id in (policy.source_order if policy else SUPPORTED_SOURCE_IDS)
    ]
    return ContextRefreshCoordinator(adapters=full, policy=policy or _policy())


def _state_for(source_id: str, state: ContextRefreshSourceState) -> ContextRefreshRuntimeState:
    return ContextRefreshRuntimeState(sources={source_id: state})


def test_runtime_state_none_initializes_complete_default_state() -> None:
    result = _coordinator().run_due_once(BASE_TIME, None)

    assert set(result.updated_runtime_state.sources) == set(SUPPORTED_SOURCE_IDS)
    for state in result.updated_runtime_state.sources.values():
        assert state.last_attempted_at == BASE_TIME
        assert state.consecutive_failure_count == 0
        assert state.consecutive_non_usable_count == 0


def test_missing_known_source_state_gets_default_state() -> None:
    existing = ContextRefreshSourceState(
        last_attempted_at=BASE_TIME,
        next_due_at=BASE_TIME + timedelta(hours=1),
    )

    result = _coordinator().run_due_once(BASE_TIME, _state_for("macro_calendar", existing))

    assert set(result.updated_runtime_state.sources) == set(SUPPORTED_SOURCE_IDS)
    assert result.updated_runtime_state.sources["macro_calendar"].last_status is ContextRefreshStatus.SKIPPED_NOT_DUE
    assert result.updated_runtime_state.sources["eia_wpsr"].last_attempted_at == BASE_TIME


def test_unknown_source_state_id_is_rejected() -> None:
    state = ContextRefreshRuntimeState(sources={"bad_source": ContextRefreshSourceState()})

    with pytest.raises(ContextRefreshError, match="unknown context refresh source state"):
        _coordinator().run_due_once(BASE_TIME, state)


def test_first_call_is_due_and_runs_once() -> None:
    adapter = FakeAdapter("macro_calendar")

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert len(adapter.calls) == 1
    assert "macro_calendar" in result.sources_run
    assert result.updated_runtime_state.sources["macro_calendar"].last_attempted_at == BASE_TIME


def test_before_next_due_is_skipped() -> None:
    adapter = FakeAdapter("macro_calendar")
    evaluation_time = BASE_TIME + timedelta(minutes=1)
    state = _state_for(
        "macro_calendar",
        ContextRefreshSourceState(
            last_attempted_at=BASE_TIME,
            next_due_at=BASE_TIME + timedelta(minutes=10),
        ),
    )

    result = _coordinator(adapter).run_due_once(evaluation_time, state)

    assert adapter.calls == []
    assert result.sources_skipped_not_due == ("macro_calendar",)
    skipped_state = result.updated_runtime_state.sources["macro_calendar"]
    assert skipped_state.last_status is ContextRefreshStatus.SKIPPED_NOT_DUE
    assert skipped_state.last_status_observed_at == evaluation_time


def test_exact_next_due_is_due() -> None:
    adapter = FakeAdapter("macro_calendar")
    state = _state_for(
        "macro_calendar",
        ContextRefreshSourceState(
            last_attempted_at=BASE_TIME,
            next_due_at=BASE_TIME + timedelta(minutes=5),
        ),
    )

    _coordinator(adapter).run_due_once(BASE_TIME + timedelta(minutes=5), state)

    assert len(adapter.calls) == 1


def test_disabled_adapter_is_never_called() -> None:
    adapter = FakeAdapter("macro_calendar", enabled=False)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert adapter.calls == []
    assert result.sources_disabled == ("macro_calendar",)
    assert result.source_outcomes[0].status is ContextRefreshStatus.DISABLED
    disabled_state = result.updated_runtime_state.sources["macro_calendar"]
    assert disabled_state.last_status is ContextRefreshStatus.DISABLED
    assert disabled_state.last_status_observed_at == BASE_TIME


def test_valid_adapter_next_due_hint_overrides_fallback() -> None:
    hint = BASE_TIME + timedelta(hours=4)
    adapter = FakeAdapter("macro_calendar", next_due_at=hint)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.updated_runtime_state.sources["macro_calendar"].next_due_at == hint


def test_missing_next_due_hint_uses_fallback() -> None:
    adapter = FakeAdapter("macro_calendar", next_due_at=None)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.updated_runtime_state.sources["macro_calendar"].next_due_at == BASE_TIME + timedelta(seconds=60)
    assert not [issue for issue in result.issues if issue.issue_type == "INVALID_NEXT_DUE_HINT"]


@pytest.mark.parametrize(
    "hint",
    [
        BASE_TIME - timedelta(seconds=1),
        BASE_TIME,
        datetime(2026, 1, 2, 12, 1),
    ],
)
def test_invalid_next_due_hint_creates_issue_and_uses_fallback(hint: datetime) -> None:
    adapter = FakeAdapter("macro_calendar", next_due_at=hint)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.updated_runtime_state.sources["macro_calendar"].next_due_at == BASE_TIME + timedelta(seconds=60)
    assert [issue for issue in result.issues if issue.issue_type == "INVALID_NEXT_DUE_HINT"]


def test_adapter_exception_becomes_failed_and_later_source_still_runs() -> None:
    bad = FakeAdapter("macro_calendar", exception=RuntimeError("source failed badly"))
    later = FakeAdapter("eia_wpsr")

    result = _coordinator(bad, later).run_due_once(BASE_TIME, None)

    macro = result.updated_runtime_state.sources["macro_calendar"]
    assert macro.last_status is ContextRefreshStatus.FAILED
    assert macro.last_status_observed_at == BASE_TIME
    assert macro.consecutive_failure_count == 1
    assert macro.consecutive_non_usable_count == 1
    assert len(later.calls) == 1


def test_keyboard_interrupt_is_not_swallowed() -> None:
    class InterruptingAdapter(FakeAdapter):
        def run_once(self, *args: Any, **kwargs: Any) -> ContextRefreshAdapterResult:
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _coordinator(InterruptingAdapter("macro_calendar")).run_due_once(BASE_TIME, None)


def test_runtime_state_is_not_mutated_in_place() -> None:
    original_source = ContextRefreshSourceState(
        last_attempted_at=BASE_TIME,
        next_due_at=BASE_TIME,
    )
    state = _state_for("macro_calendar", original_source)

    result = _coordinator().run_due_once(BASE_TIME, state)

    assert state.sources["macro_calendar"] is original_source
    assert state.sources["macro_calendar"].last_status is None
    assert result.updated_runtime_state.sources["macro_calendar"].last_status is ContextRefreshStatus.SUCCESS


def test_source_execution_order_follows_configuration() -> None:
    order = (
        "fred",
        "macro_calendar",
        "eia_wpsr",
        "usaspending",
        "yfinance_dev_only",
    )
    calls: list[str] = []

    class OrderedAdapter(FakeAdapter):
        def run_once(self, *args: Any, **kwargs: Any) -> ContextRefreshAdapterResult:
            calls.append(self.source_id)
            return super().run_once(*args, **kwargs)

    adapters = [OrderedAdapter(source_id) for source_id in order]

    ContextRefreshCoordinator(adapters=adapters, policy=_policy(order=order)).run_due_once(BASE_TIME, None)

    assert calls == list(order)


def test_native_result_is_not_stored_in_runtime_state_json_projection() -> None:
    native = object()
    adapter = FakeAdapter("macro_calendar", native_result=native)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)
    encoded_state = json.dumps(result.updated_runtime_state.to_json_dict(), allow_nan=False)
    encoded_run = json.dumps(result.to_json_dict(), allow_nan=False)

    assert result.source_outcomes[0].native_result is native
    assert "native_result" not in encoded_state
    assert "native_result_summary" in encoded_run


def test_status_observation_timestamp_round_trips_in_runtime_state_projection() -> None:
    state = ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_status=ContextRefreshStatus.SUCCESS,
                last_status_observed_at=BASE_TIME,
            )
        }
    )

    encoded = json.dumps(state.to_json_dict(), allow_nan=False, sort_keys=True)
    decoded = json.loads(encoded)

    assert decoded["sources"]["macro_calendar"]["last_status"] == "SUCCESS"
    assert decoded["sources"]["macro_calendar"]["last_status_observed_at"] == "2026-01-02T12:00:00Z"


def test_source_state_construction_without_status_observed_at_remains_valid() -> None:
    state = ContextRefreshSourceState(last_status=ContextRefreshStatus.SUCCESS)

    assert state.last_status is ContextRefreshStatus.SUCCESS
    assert state.last_status_observed_at is None


@pytest.mark.parametrize(
    "status",
    [
        ContextRefreshStatus.STALE,
        ContextRefreshStatus.NO_FRESH_DATA,
        ContextRefreshStatus.DATA_DELAYED,
        ContextRefreshStatus.PARTIAL,
        ContextRefreshStatus.SUCCESS,
    ],
)
def test_meaningful_statuses_remain_distinguishable(status: ContextRefreshStatus) -> None:
    adapter = FakeAdapter("macro_calendar", status=status, usable_context=False)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.source_outcomes[0].status is status
    assert result.updated_runtime_state.sources["macro_calendar"].last_status is status
    assert result.updated_runtime_state.sources["macro_calendar"].last_status_observed_at == BASE_TIME


def test_productive_partial_updates_usable_but_not_full_success() -> None:
    adapter = FakeAdapter(
        "macro_calendar",
        status=ContextRefreshStatus.PARTIAL,
        usable_context=True,
    )

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)
    state = result.updated_runtime_state.sources["macro_calendar"]

    assert state.last_usable_at == BASE_TIME
    assert state.last_full_success_at is None


def test_repeated_stale_non_usable_results_increment_non_usable_count() -> None:
    adapter = FakeAdapter(
        "macro_calendar",
        status=ContextRefreshStatus.STALE,
        usable_context=False,
    )
    first = _coordinator(adapter).run_due_once(BASE_TIME, None)
    second = _coordinator(adapter).run_due_once(
        BASE_TIME + timedelta(seconds=60),
        first.updated_runtime_state,
    )

    assert second.updated_runtime_state.sources["macro_calendar"].consecutive_non_usable_count == 2


def test_full_success_updates_success_timestamps_and_resets_non_usable_count() -> None:
    adapter = FakeAdapter("macro_calendar", status=ContextRefreshStatus.SUCCESS, usable_context=True)
    state = _state_for(
        "macro_calendar",
        ContextRefreshSourceState(
            consecutive_non_usable_count=3,
            last_attempted_at=BASE_TIME - timedelta(minutes=1),
            next_due_at=BASE_TIME,
        ),
    )

    result = _coordinator(adapter).run_due_once(BASE_TIME, state)
    source_state = result.updated_runtime_state.sources["macro_calendar"]

    assert source_state.last_completed_at == BASE_TIME
    assert source_state.last_usable_at == BASE_TIME
    assert source_state.last_full_success_at == BASE_TIME
    assert source_state.consecutive_non_usable_count == 0


def test_failure_preserves_history_and_increments_failure_count() -> None:
    prior_completed = BASE_TIME - timedelta(days=2)
    prior_usable = BASE_TIME - timedelta(days=1)
    prior_success = BASE_TIME - timedelta(hours=12)
    adapter = FakeAdapter("macro_calendar", status=ContextRefreshStatus.FAILED, usable_context=False)
    state = _state_for(
        "macro_calendar",
        ContextRefreshSourceState(
            last_attempted_at=BASE_TIME - timedelta(minutes=1),
            last_completed_at=prior_completed,
            last_usable_at=prior_usable,
            last_full_success_at=prior_success,
            next_due_at=BASE_TIME,
            consecutive_failure_count=2,
        ),
    )

    result = _coordinator(adapter).run_due_once(BASE_TIME, state)
    source_state = result.updated_runtime_state.sources["macro_calendar"]

    assert source_state.last_completed_at == prior_completed
    assert source_state.last_usable_at == prior_usable
    assert source_state.last_full_success_at == prior_success
    assert source_state.last_status is ContextRefreshStatus.FAILED
    assert source_state.last_status_observed_at == BASE_TIME
    assert source_state.consecutive_failure_count == 3


def test_due_adapter_receives_forwarded_write_and_session_arguments() -> None:
    adapter = FakeAdapter("macro_calendar")

    _coordinator(adapter).run_due_once(
        BASE_TIME,
        None,
        write_questdb=True,
        questdb_required=True,
        run_id="run_1",
        session_id="session_1",
    )

    assert adapter.calls[0]["write_questdb"] is True
    assert adapter.calls[0]["questdb_required"] is True
    assert adapter.calls[0]["run_id"] == "run_1"
    assert adapter.calls[0]["session_id"] == "session_1"


def test_skipped_and_disabled_adapters_receive_no_run_once_call() -> None:
    skipped = FakeAdapter("macro_calendar")
    disabled = FakeAdapter("eia_wpsr", enabled=False)
    state = ContextRefreshRuntimeState(
        sources={
            "macro_calendar": ContextRefreshSourceState(
                last_attempted_at=BASE_TIME,
                next_due_at=BASE_TIME + timedelta(hours=1),
            )
        }
    )

    _coordinator(skipped, disabled).run_due_once(BASE_TIME, state)

    assert skipped.calls == []
    assert disabled.calls == []


@pytest.mark.parametrize(
    "broken",
    [
        {"schema_version": 1, "source_order": list(SUPPORTED_SOURCE_IDS), "sources": {}},
        {
            "schema_version": 1,
            "source_order": ["macro_calendar", "macro_calendar", "fred", "usaspending", "yfinance_dev_only"],
            "sources": {},
        },
        {
            "schema_version": 1,
            "source_order": list(SUPPORTED_SOURCE_IDS),
            "sources": {
                source_id: {"fallback_interval_seconds": 0}
                for source_id in SUPPORTED_SOURCE_IDS
            },
        },
        {
            "schema_version": 1,
            "source_order": list(SUPPORTED_SOURCE_IDS),
            "sources": {
                **{
                    source_id: {"fallback_interval_seconds": 60}
                    for source_id in SUPPORTED_SOURCE_IDS
                },
                "bad": {"fallback_interval_seconds": 60},
            },
        },
        {
            "schema_version": 1,
            "source_order": list(SUPPORTED_SOURCE_IDS),
            "sources": {
                source_id: {"fallback_interval_seconds": 60, "extra": True}
                for source_id in SUPPORTED_SOURCE_IDS
            },
        },
    ],
)
def test_invalid_coordinator_config_is_rejected(broken: dict[str, object]) -> None:
    with pytest.raises(ContextRefreshError):
        ContextRefreshPolicy.from_mapping(broken)


def test_checked_in_config_loads_and_checker_passes() -> None:
    from scripts.check_context_refresh_coordinator import run_checks

    policy = ContextRefreshPolicy.from_yaml(Path("config/context_refresh.yaml"), base_dir=Path.cwd())
    failures = [result.message for result in run_checks() if not result.ok]

    assert policy.source_order == SUPPORTED_SOURCE_IDS
    assert failures == []


@dataclass
class FakeEIACollector:
    result: EIAWPSRCollectionResult
    config: object | None = None

    def __post_init__(self) -> None:
        if self.config is None:
            self.config = SimpleNamespace(
                event_windows_enabled=True,
                numeric_source_enabled=True,
                releases=(),
            )
        self.calls: list[dict[str, object]] = []

    def collect(self, **kwargs: object) -> EIAWPSRCollectionResult:
        self.calls.append(kwargs)
        return self.result


class RecordingEIAClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int]] = []

    def fetch_weekly_records(
        self,
        route: str,
        series_ids: list[str],
        *,
        observations_per_series: int,
    ) -> list[dict[str, object]]:
        self.calls.append((route, list(series_ids), observations_per_series))
        return []


class RecordingEIACollector(EIAWPSRCollector):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.collect_calls: list[dict[str, object]] = []

    def collect(self, **kwargs: object) -> EIAWPSRCollectionResult:
        self.collect_calls.append(kwargs)
        return super().collect(**kwargs)


def _eia_release_pair() -> tuple[EIARelease, EIARelease]:
    return (
        EIARelease(
            release_id="eia_1",
            release_at=BASE_TIME,
            report_period=date(2026, 1, 2),
        ),
        EIARelease(
            release_id="eia_2",
            release_at=BASE_TIME + timedelta(days=7),
            report_period=date(2026, 1, 9),
        ),
    )


def _eia_config(*, numeric_source_enabled: bool) -> EIAWPSRConfig:
    return EIAWPSRConfig(
        event_windows_enabled=True,
        numeric_source_enabled=numeric_source_enabled,
        releases=_eia_release_pair(),
        oil_tickers=("XOM",),
    )


def _eia_plan(
    *,
    action_kind: EIAWPSRActionKind,
    next_action_at: datetime | None,
    data_status: EIAWPSRDataStatus = EIAWPSRDataStatus.NOT_DUE,
    expected_report_period: date = date(2026, 1, 2),
) -> EIAWPSRActionPlan:
    return EIAWPSRActionPlan(
        release_id="eia_1",
        action_kind=action_kind,
        due_at=None,
        next_action_at=next_action_at,
        expected_report_period=expected_report_period,
        data_status=data_status,
    )


def _eia_result(
    *,
    status: EIAWPSRCollectionStatus,
    action_kind: EIAWPSRActionKind = EIAWPSRActionKind.NO_ACTION,
    next_action_at: datetime | None = None,
    next_retry_at: datetime | None = None,
    data_status: EIAWPSRDataStatus = EIAWPSRDataStatus.NOT_DUE,
    expected_report_period: date = date(2026, 1, 2),
    last_seen_report_period: date | None = None,
    flags: tuple[object, ...] = (),
    snapshots: tuple[object, ...] = (),
) -> EIAWPSRCollectionResult:
    return EIAWPSRCollectionResult(
        status=status,
        action_plan=_eia_plan(
            action_kind=action_kind,
            next_action_at=next_action_at,
            data_status=data_status,
            expected_report_period=expected_report_period,
        ),
        expected_report_period=expected_report_period,
        last_seen_report_period=last_seen_report_period,
        next_retry_at=next_retry_at,
        data_status=data_status,
        context_flags=flags,  # type: ignore[arg-type]
        indicator_snapshots=snapshots,  # type: ignore[arg-type]
    )


def test_eia_before_release_window_returns_window_start_as_next_due() -> None:
    window_start = BASE_TIME + timedelta(minutes=5)
    adapter = EIAWPSRRefreshAdapter(
        FakeEIACollector(
            _eia_result(
                status=EIAWPSRCollectionStatus.NO_FRESH_DATA,
                next_action_at=window_start,
            )
        )  # type: ignore[arg-type]
    )

    result = adapter.run_once(BASE_TIME, ContextRefreshSourceState(), write_questdb=False, questdb_required=False, run_id=None, session_id=None)

    assert result.next_due_at == window_start


def test_eia_fast_retry_native_timing_wins_over_fallback() -> None:
    retry_at = BASE_TIME + timedelta(seconds=60)
    adapter = EIAWPSRRefreshAdapter(
        FakeEIACollector(
            _eia_result(
                status=EIAWPSRCollectionStatus.NO_FRESH_DATA,
                action_kind=EIAWPSRActionKind.FETCH_NUMERIC_REPORT,
                next_retry_at=retry_at,
                data_status=EIAWPSRDataStatus.WAITING_FOR_DATA,
            )
        )  # type: ignore[arg-type]
    )

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.updated_runtime_state.sources["eia_wpsr"].next_due_at == retry_at


def test_eia_continuation_state_preserves_numeric_attempt_and_successful_period() -> None:
    report_period = date(2026, 1, 2)
    collector = FakeEIACollector(
        _eia_result(
            status=EIAWPSRCollectionStatus.SUCCESS,
            action_kind=EIAWPSRActionKind.FETCH_NUMERIC_REPORT,
            data_status=EIAWPSRDataStatus.CURRENT,
            expected_report_period=report_period,
            last_seen_report_period=report_period,
            snapshots=(object(),),
        )
    )
    adapter = EIAWPSRRefreshAdapter(collector)  # type: ignore[arg-type]
    state = ContextRefreshSourceState(
        adapter_state={
            "last_numeric_attempt_at": "2026-01-02T11:00:00Z",
            "last_successful_report_period": "2025-12-26",
        }
    )

    result = adapter.run_once(BASE_TIME, state, write_questdb=True, questdb_required=True, run_id="r", session_id="s")

    assert collector.calls[0]["last_numeric_attempt_at"] == datetime(2026, 1, 2, 11, 0, tzinfo=UTC)
    assert collector.calls[0]["last_successful_report_period"] == date(2025, 12, 26)
    assert result.adapter_state["last_numeric_attempt_at"] == "2026-01-02T12:00:00Z"
    assert result.adapter_state["last_successful_report_period"] == "2026-01-02"


def test_eia_disabled_numeric_source_does_not_record_numeric_attempt() -> None:
    evaluation_time = BASE_TIME + timedelta(minutes=1)
    client = RecordingEIAClient()
    collector = EIAWPSRCollector(
        cache=ContextStateCache(),
        config=_eia_config(numeric_source_enabled=False),
        client=client,
    )
    adapter = EIAWPSRRefreshAdapter(collector)

    empty_result = adapter.run_once(
        evaluation_time,
        ContextRefreshSourceState(),
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )
    existing_result = adapter.run_once(
        evaluation_time,
        ContextRefreshSourceState(
            adapter_state={"last_numeric_attempt_at": "2026-01-02T10:00:00Z"}
        ),
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )

    assert "last_numeric_attempt_at" not in empty_result.adapter_state
    assert existing_result.adapter_state["last_numeric_attempt_at"] == "2026-01-02T10:00:00Z"
    assert client.calls == []


def test_eia_disabled_numeric_source_uses_next_release_window_not_retry_loop() -> None:
    evaluation_time = BASE_TIME + timedelta(minutes=1)
    numeric_retry_at = evaluation_time + timedelta(seconds=60)
    next_release_window = _eia_release_pair()[1].window_start
    collector = EIAWPSRCollector(
        cache=ContextStateCache(),
        config=_eia_config(numeric_source_enabled=False),
        client=RecordingEIAClient(),
    )
    adapter = EIAWPSRRefreshAdapter(collector)
    coordinator = _coordinator(adapter)

    first = coordinator.run_due_once(evaluation_time, None)
    second = coordinator.run_due_once(
        evaluation_time + timedelta(seconds=60),
        first.updated_runtime_state,
    )

    assert first.updated_runtime_state.sources["eia_wpsr"].next_due_at == next_release_window
    assert first.updated_runtime_state.sources["eia_wpsr"].next_due_at != numeric_retry_at
    assert second.updated_runtime_state.sources["eia_wpsr"].last_status is ContextRefreshStatus.SKIPPED_NOT_DUE


def test_eia_enabled_numeric_source_still_records_attempt_and_native_retry() -> None:
    retry_at = BASE_TIME + timedelta(seconds=60)
    collector = FakeEIACollector(
        _eia_result(
            status=EIAWPSRCollectionStatus.NO_FRESH_DATA,
            action_kind=EIAWPSRActionKind.FETCH_NUMERIC_REPORT,
            next_retry_at=retry_at,
            data_status=EIAWPSRDataStatus.WAITING_FOR_DATA,
        ),
        config=SimpleNamespace(
            event_windows_enabled=True,
            numeric_source_enabled=True,
            releases=_eia_release_pair(),
        ),
    )
    adapter = EIAWPSRRefreshAdapter(collector)

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)
    state = result.updated_runtime_state.sources["eia_wpsr"]

    assert state.adapter_state["last_numeric_attempt_at"] == "2026-01-02T12:00:00Z"
    assert state.next_due_at == retry_at


def test_eia_enabling_numeric_later_uses_no_fabricated_prior_attempt() -> None:
    evaluation_time = BASE_TIME + timedelta(minutes=1)
    disabled_collector = EIAWPSRCollector(
        cache=ContextStateCache(),
        config=_eia_config(numeric_source_enabled=False),
        client=RecordingEIAClient(),
    )
    disabled_adapter = EIAWPSRRefreshAdapter(disabled_collector)
    disabled_result = disabled_adapter.run_once(
        evaluation_time,
        ContextRefreshSourceState(),
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )
    enabled_client = RecordingEIAClient()
    enabled_collector = RecordingEIACollector(
        cache=ContextStateCache(),
        config=_eia_config(numeric_source_enabled=True),
        client=enabled_client,
    )
    enabled_adapter = EIAWPSRRefreshAdapter(enabled_collector)

    enabled_result = enabled_adapter.run_once(
        evaluation_time,
        ContextRefreshSourceState(adapter_state=disabled_result.adapter_state),
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )

    assert "last_numeric_attempt_at" not in disabled_result.adapter_state
    assert enabled_collector.collect_calls[0]["last_numeric_attempt_at"] is None
    assert enabled_client.calls
    assert enabled_result.adapter_state["last_numeric_attempt_at"] == "2026-01-02T12:01:00Z"


def test_failed_eia_fetch_preserves_native_retry_timing() -> None:
    retry_at = BASE_TIME + timedelta(seconds=60)
    adapter = EIAWPSRRefreshAdapter(
        FakeEIACollector(
            _eia_result(
                status=EIAWPSRCollectionStatus.FAILED,
                action_kind=EIAWPSRActionKind.FETCH_NUMERIC_REPORT,
                next_retry_at=retry_at,
                data_status=EIAWPSRDataStatus.WAITING_FOR_DATA,
            )
        )  # type: ignore[arg-type]
    )

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.updated_runtime_state.sources["eia_wpsr"].next_due_at == retry_at
    assert result.updated_runtime_state.sources["eia_wpsr"].adapter_state["last_numeric_attempt_at"] == "2026-01-02T12:00:00Z"


def _macro_calendar(*, event_time: datetime, pre_minutes: int, post_minutes: int) -> MacroCalendar:
    profile = MacroWindowProfile(
        profile_id="TIER_3",
        pre_event_minutes=pre_minutes,
        post_event_minutes=post_minutes,
    )
    event = MacroCalendarEvent(
        calendar_event_id="event_1",
        logical_occurrence_id="logical_1",
        event_type="CPI",
        scheduled_at=event_time,
        source_time_text="12:00",
        schedule_status="CONFIRMED",
        source_provider="BLS",
        source_record_id="record_1",
        source_reference="https://example.test/calendar",
        schedule_revision_id="rev_1",
        schedule_captured_at=BASE_TIME - timedelta(days=1),
        official_schedule_published_at=BASE_TIME - timedelta(days=2),
        research_tier="TIER_3",
        window_profile_id="TIER_3",
    )
    return MacroCalendar(
        schema_version=1,
        calendar_version="test",
        calendar_captured_at=BASE_TIME - timedelta(days=1),
        source_manifest={},
        window_profiles={"TIER_3": profile},
        event_type_policies={},
        events=(event,),
    )


class FakeMacroCollector:
    def __init__(self, calendar: MacroCalendar, status: MacroCalendarCollectionStatus = MacroCalendarCollectionStatus.NO_ACTIVE_EVENTS) -> None:
        self.config = SimpleNamespace(enabled=True, feeds_memory_cache=True, artifact_path="unused.yaml")
        self.calendar = calendar
        self.base_dir = None
        self.status = status

    def collect_once(self, evaluation_time: datetime, **kwargs: object) -> MacroCalendarCollectionResult:
        return MacroCalendarCollectionResult(status=self.status, evaluation_time=evaluation_time)


def test_macro_next_due_at_or_before_next_effective_from_boundary() -> None:
    event_time = BASE_TIME + timedelta(minutes=30)
    calendar = _macro_calendar(event_time=event_time, pre_minutes=5, post_minutes=2)
    adapter = MacroCalendarRefreshAdapter(FakeMacroCollector(calendar))  # type: ignore[arg-type]

    result = adapter.run_once(BASE_TIME, ContextRefreshSourceState(), write_questdb=False, questdb_required=False, run_id=None, session_id=None)

    assert result.next_due_at == event_time - timedelta(minutes=5)


def test_macro_active_event_next_due_is_expiry_transition() -> None:
    event_time = BASE_TIME + timedelta(minutes=1)
    calendar = _macro_calendar(event_time=event_time, pre_minutes=5, post_minutes=2)
    adapter = MacroCalendarRefreshAdapter(FakeMacroCollector(calendar))  # type: ignore[arg-type]

    result = adapter.run_once(BASE_TIME, ContextRefreshSourceState(), write_questdb=False, questdb_required=False, run_id=None, session_id=None)

    assert result.next_due_at == event_time + timedelta(minutes=2, microseconds=1)


def test_macro_short_tier_three_window_is_not_missed_by_fallback() -> None:
    event_time = BASE_TIME + timedelta(minutes=3)
    calendar = _macro_calendar(event_time=event_time, pre_minutes=2, post_minutes=2)
    adapter = MacroCalendarRefreshAdapter(FakeMacroCollector(calendar))  # type: ignore[arg-type]

    result = _coordinator(adapter).run_due_once(BASE_TIME, None)

    assert result.updated_runtime_state.sources["macro_calendar"].next_due_at == BASE_TIME + timedelta(minutes=1)


def test_macro_adapter_does_not_import_runtime_network_clients() -> None:
    text = Path("src/market_relay_engine/context/refresh_coordinator.py").read_text(encoding="utf-8")

    assert "import requests" not in text
    assert "import yfinance" not in text
    assert "urllib.request" not in text


def _yfinance_config() -> YFinanceProxyConfig:
    registry = build_proxy_registry(None)
    return YFinanceProxyConfig(
        enabled=True,
        requested_symbols=("XLE",),
        registry=(registry["XLE"],),
    )


def _frame(start: datetime, periods: int = 14) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": [100.0 + index for index in range(periods)]},
        index=pd.date_range(start=start, periods=periods, freq="5min", tz="UTC"),
    )


def test_yfinance_explicit_evaluation_time_makes_timestamps_deterministic() -> None:
    def bad_clock() -> datetime:
        raise AssertionError("clock should not be called when evaluation_time is supplied")

    collector = YFinanceProxyCollector(
        cache=ContextStateCache(),
        config=_yfinance_config(),
        download=lambda **_: _frame(datetime(2026, 1, 2, 14, 0, tzinfo=UTC)),
        clock=bad_clock,
    )
    evaluation_time = datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC)

    result = collector.collect(evaluation_time=evaluation_time)

    assert result.started_at == evaluation_time
    assert result.completed_at == evaluation_time
    assert {snapshot.snapshot_time for snapshot in result.indicator_snapshots} == {evaluation_time}


def test_yfinance_next_due_aligns_to_next_completed_five_minute_bar_plus_grace() -> None:
    assert next_yfinance_bar_due_at(
        datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC),
        bar_completion_grace_seconds=30,
    ) == datetime(2026, 1, 2, 15, 10, 30, tzinfo=UTC)
    assert next_yfinance_bar_due_at(
        datetime(2026, 1, 2, 15, 10, 31, tzinfo=UTC),
        bar_completion_grace_seconds=30,
    ) == datetime(2026, 1, 2, 15, 15, 30, tzinfo=UTC)


def test_yfinance_adapter_uses_bar_boundary_next_due() -> None:
    class FakeYFinanceCollector:
        def __init__(self) -> None:
            self.config = SimpleNamespace(enabled=True, bar_completion_grace_seconds=30)

        def collect(self, **kwargs: object) -> YFinanceProxyCollectionResult:
            return YFinanceProxyCollectionResult(
                status=YFinanceProxyCollectionStatus.NO_FRESH_DATA,
                started_at=BASE_TIME,
                completed_at=BASE_TIME,
                requested_symbols=("XLE",),
                successful_symbols=(),
                failed_symbols=(),
                stale_symbols=(),
                issues=(),
                indicator_snapshots=(),
                cache_update_results=(),
                ledger_write_results=(),
            )

    adapter = YFinanceRefreshAdapter(FakeYFinanceCollector())  # type: ignore[arg-type]
    evaluation_time = datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC)

    result = adapter.run_once(evaluation_time, ContextRefreshSourceState(), write_questdb=False, questdb_required=False, run_id=None, session_id=None)

    assert result.next_due_at == datetime(2026, 1, 2, 15, 10, 30, tzinfo=UTC)
