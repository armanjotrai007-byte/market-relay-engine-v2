from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import inspect
import json
import math
import os
from pathlib import Path

import pytest

from market_relay_engine.context.eia_wpsr import (
    EIARelease,
    EIAWPSRActionKind,
    EIAWPSRCollectionStatus,
    EIAWPSRCollector,
    EIAWPSRConfig,
    EIAWPSRDataStatus,
    EIAWPSRError,
    EIAWPSRClient,
    STOCK_ROUTE,
    UTILIZATION_ROUTE,
    plan_eia_wpsr_action,
)
from market_relay_engine.context.state_cache import ContextStateCache
from market_relay_engine.contracts.context import ContextIndicatorSnapshot
from market_relay_engine.risk import context_risk_input_from_contracts
from scripts.refresh_eia_wpsr_schedule import parse_schedule_candidates


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eia_wpsr"
RELEASE_AT = datetime(2026, 6, 17, 14, 30, tzinfo=UTC)
NEXT_RELEASE_AT = datetime(2026, 6, 24, 14, 30, tzinfo=UTC)


def _release(
    *,
    release_id: str = "eia_wpsr_2026_06_17",
    release_at: datetime = RELEASE_AT,
    report_period: date = date(2026, 6, 12),
) -> EIARelease:
    return EIARelease(
        release_id=release_id,
        release_at=release_at,
        report_period=report_period,
    )


def _releases() -> tuple[EIARelease, EIARelease]:
    return (
        _release(),
        _release(
            release_id="eia_wpsr_2026_06_24",
            release_at=NEXT_RELEASE_AT,
            report_period=date(2026, 6, 19),
        ),
    )


def _config(*, enabled: bool = True) -> EIAWPSRConfig:
    return EIAWPSRConfig(
        event_windows_enabled=enabled,
        numeric_source_enabled=enabled,
        releases=_releases() if enabled else (),
        oil_tickers=("XOM", "CVX") if enabled else (),
    )


def _records(name: str) -> list[dict[str, object]]:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return payload["response"]["data"]


class FakeClient:
    def __init__(self, *, stocks: list[dict[str, object]] | None = None, utilization: list[dict[str, object]] | None = None) -> None:
        self.stocks = deepcopy(stocks if stocks is not None else _records("weekly_stocks.json"))
        self.utilization = deepcopy(utilization if utilization is not None else _records("refinery_utilization.json"))
        self.calls: list[str] = []

    def fetch_weekly_records(self, route: str, series_ids: object, *, observations_per_series: int = 3) -> list[dict[str, object]]:
        self.calls.append(route)
        return deepcopy(self.stocks if route == STOCK_ROUTE else self.utilization)


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.flags: list[object] = []
        self.indicators: list[object] = []

    def write_context_flag(self, flag: object, **kwargs: object) -> str:
        if self.fail:
            raise RuntimeError("writer unavailable")
        self.flags.append(flag)
        return "flag"

    def write_context_indicator_snapshot(self, snapshot: object, **kwargs: object) -> str:
        if self.fail:
            raise RuntimeError("writer unavailable")
        self.indicators.append(snapshot)
        return "indicator"


def test_release_configuration_and_boundaries() -> None:
    release = _release()
    assert release.window_start == RELEASE_AT - timedelta(seconds=300)
    assert release.window_end == RELEASE_AT + timedelta(seconds=900)
    assert release.initial_fetch_at == RELEASE_AT + timedelta(seconds=30)

    for field_name in ("pre_release_window_seconds", "post_release_window_seconds"):
        values = {
            "release_id": "x",
            "release_at": RELEASE_AT,
            "report_period": date(2026, 6, 12),
            field_name: -1,
        }
        with pytest.raises(EIAWPSRError, match=field_name):
            EIARelease(**values)
        values[field_name] = True
        with pytest.raises(EIAWPSRError, match=field_name):
            EIARelease(**values)


def test_repository_config_requires_all_window_fields() -> None:
    calendar = {
        "event_windows": {
            "eia": {
                "enabled": False,
                "pre_release_window_seconds": 300,
                "post_release_window_seconds": 900,
                "initial_fetch_delay_seconds": 30,
                "fast_retry_interval_seconds": 60,
                "fast_retry_window_seconds": 1200,
                "delayed_retry_interval_seconds": 3600,
                "releases": [],
            }
        }
    }
    sources = {"structured_sources": {"eia": {"enabled": False, "max_staleness_seconds": 86400}}}
    symbols = {"tradable_universe": {}}
    for missing in ("pre_release_window_seconds", "post_release_window_seconds"):
        candidate = deepcopy(calendar)
        del candidate["event_windows"]["eia"][missing]
        with pytest.raises(EIAWPSRError, match=missing):
            EIAWPSRConfig.from_repository_configs(candidate, sources, symbols)


def test_planner_is_explicit_and_rejects_naive_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import market_relay_engine.context.eia_wpsr as module

    monkeypatch.setattr(module, "utc_now", lambda: (_ for _ in ()).throw(AssertionError("clock used")))
    plan = plan_eia_wpsr_action(
        releases=_releases(),
        evaluation_time=RELEASE_AT - timedelta(minutes=5),
        last_numeric_attempt_at=None,
        last_successful_report_period=None,
    )
    assert plan.action_kind is EIAWPSRActionKind.REFRESH_RELEASE_WINDOW
    with pytest.raises(ValueError, match="timezone-aware"):
        plan_eia_wpsr_action(
            releases=_releases(),
            evaluation_time=datetime(2026, 6, 17, 14, 25),
            last_numeric_attempt_at=None,
            last_successful_report_period=None,
        )


def test_planner_fetch_retry_and_delayed_boundaries() -> None:
    releases = _releases()
    before = plan_eia_wpsr_action(releases=releases, evaluation_time=RELEASE_AT + timedelta(seconds=29), last_numeric_attempt_at=None, last_successful_report_period=None)
    first = plan_eia_wpsr_action(releases=releases, evaluation_time=RELEASE_AT + timedelta(seconds=30), last_numeric_attempt_at=None, last_successful_report_period=None)
    retry = plan_eia_wpsr_action(releases=releases, evaluation_time=RELEASE_AT + timedelta(seconds=90), last_numeric_attempt_at=RELEASE_AT + timedelta(seconds=30), last_successful_report_period=None)
    delayed = plan_eia_wpsr_action(releases=releases, evaluation_time=RELEASE_AT + timedelta(seconds=3630), last_numeric_attempt_at=RELEASE_AT + timedelta(seconds=30), last_successful_report_period=None)

    assert before.action_kind is EIAWPSRActionKind.REFRESH_RELEASE_WINDOW
    assert first.action_kind is EIAWPSRActionKind.FETCH_NUMERIC_REPORT
    assert retry.action_kind is EIAWPSRActionKind.RETRY_NUMERIC_REPORT
    assert delayed.action_kind is EIAWPSRActionKind.RETRY_NUMERIC_REPORT
    assert delayed.data_status is EIAWPSRDataStatus.DATA_DELAYED


def test_delayed_release_is_superseded_and_new_cycle_advances() -> None:
    releases = _releases()
    at_next_window = NEXT_RELEASE_AT - timedelta(seconds=300)
    superseded = plan_eia_wpsr_action(
        releases=releases,
        evaluation_time=at_next_window,
        last_numeric_attempt_at=RELEASE_AT + timedelta(hours=1),
        last_successful_report_period=None,
    )
    advanced = plan_eia_wpsr_action(
        releases=releases,
        evaluation_time=at_next_window,
        last_numeric_attempt_at=None,
        last_successful_report_period=None,
    )
    assert superseded.data_status is EIAWPSRDataStatus.SUPERSEDED
    assert superseded.action_kind is EIAWPSRActionKind.NO_ACTION
    assert superseded.next_release_id == releases[1].release_id
    assert advanced.release_id == releases[1].release_id
    assert advanced.action_kind is EIAWPSRActionKind.REFRESH_RELEASE_WINDOW


def test_completed_release_advances_with_carried_attempt() -> None:
    releases = _releases()
    completed_attempt = RELEASE_AT + timedelta(seconds=30)
    next_window_start = releases[1].window_start

    at_next_window = plan_eia_wpsr_action(
        releases=releases,
        evaluation_time=next_window_start,
        last_numeric_attempt_at=completed_attempt,
        last_successful_report_period=releases[0].report_period,
    )
    at_next_fetch = plan_eia_wpsr_action(
        releases=releases,
        evaluation_time=releases[1].initial_fetch_at,
        last_numeric_attempt_at=completed_attempt,
        last_successful_report_period=releases[0].report_period,
    )

    assert at_next_window.release_id == releases[1].release_id
    assert at_next_window.expected_report_period == releases[1].report_period
    assert at_next_window.action_kind is EIAWPSRActionKind.REFRESH_RELEASE_WINDOW
    assert at_next_window.next_action_at == releases[1].initial_fetch_at
    assert at_next_window.next_action_at >= next_window_start
    assert at_next_fetch.release_id == releases[1].release_id
    assert at_next_fetch.expected_report_period == releases[1].report_period
    assert at_next_fetch.action_kind is EIAWPSRActionKind.FETCH_NUMERIC_REPORT
    assert at_next_fetch.due_at == releases[1].initial_fetch_at


def test_incomplete_release_retries_before_next_window_then_is_superseded() -> None:
    releases = _releases()
    next_window_start = releases[1].window_start
    prior_attempt = next_window_start - timedelta(hours=2)

    retry = plan_eia_wpsr_action(
        releases=releases,
        evaluation_time=next_window_start - timedelta(hours=1),
        last_numeric_attempt_at=prior_attempt,
        last_successful_report_period=None,
    )
    superseded = plan_eia_wpsr_action(
        releases=releases,
        evaluation_time=next_window_start,
        last_numeric_attempt_at=prior_attempt,
        last_successful_report_period=None,
    )

    assert retry.release_id == releases[0].release_id
    assert retry.action_kind is EIAWPSRActionKind.RETRY_NUMERIC_REPORT
    assert retry.data_status is EIAWPSRDataStatus.DATA_DELAYED
    assert superseded.release_id == releases[0].release_id
    assert superseded.action_kind is EIAWPSRActionKind.NO_ACTION
    assert superseded.data_status is EIAWPSRDataStatus.SUPERSEDED
    assert superseded.next_release_id == releases[1].release_id


def test_disabled_collection_has_no_side_effects() -> None:
    client = FakeClient()
    cache = ContextStateCache()
    result = EIAWPSRCollector(cache=cache, config=_config(enabled=False), client=client).collect(evaluation_time=RELEASE_AT)
    assert result.status is EIAWPSRCollectionStatus.DISABLED
    assert client.calls == []
    assert cache.snapshot(now=RELEASE_AT)["entry_count"] == 0


def test_pre_release_flags_are_inclusive_and_numeric_is_not_fetched() -> None:
    client = FakeClient()
    cache = ContextStateCache()
    collector = EIAWPSRCollector(cache=cache, config=_config(), client=client)
    at_start = collector.collect(evaluation_time=RELEASE_AT - timedelta(seconds=300))
    before_fetch = collector.collect(evaluation_time=RELEASE_AT + timedelta(seconds=29))
    assert client.calls == []
    at_end = collector.collect(evaluation_time=RELEASE_AT + timedelta(seconds=900))
    after = collector.collect(evaluation_time=RELEASE_AT + timedelta(seconds=901), last_numeric_attempt_at=RELEASE_AT + timedelta(seconds=900))

    assert {flag.ticker for flag in at_start.context_flags} == {"CVX", "XOM"}
    assert len(before_fetch.context_flags) == 2
    assert len(at_end.context_flags) == 2
    assert after.context_flags == ()


def test_flag_only_collection_reports_optional_ledger_failures() -> None:
    evaluation_time = RELEASE_AT - timedelta(seconds=300)
    successful = EIAWPSRCollector(cache=ContextStateCache(), config=_config(), client=FakeClient()).collect(
        evaluation_time=evaluation_time,
        write_questdb=False,
    )

    cache = ContextStateCache()
    partial = EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient(), ledger_writer=FakeWriter(fail=True)).collect(
        evaluation_time=evaluation_time,
        write_questdb=True,
    )

    assert successful.status is EIAWPSRCollectionStatus.SUCCESS
    assert len(successful.context_flags) == 2
    assert successful.issues == ()
    assert partial.status is EIAWPSRCollectionStatus.PARTIAL
    assert len(partial.context_flags) == 2
    assert len(partial.cache_update_results) == 2
    assert cache.snapshot(now=evaluation_time)["entry_count"] == 2
    assert {issue.issue_type for issue in partial.issues} == {"LEDGER_WRITE_FAILED"}


def test_flag_only_required_ledger_failure_still_raises() -> None:
    collector = EIAWPSRCollector(
        cache=ContextStateCache(),
        config=_config(),
        client=FakeClient(),
        ledger_writer=FakeWriter(fail=True),
    )
    with pytest.raises(EIAWPSRError, match="writer unavailable"):
        collector.collect(
            evaluation_time=RELEASE_AT - timedelta(seconds=300),
            write_questdb=True,
            questdb_required=True,
        )


def test_release_flag_maps_to_existing_event_window_risk_input() -> None:
    result = EIAWPSRCollector(cache=ContextStateCache(), config=_config(), client=FakeClient()).collect(evaluation_time=RELEASE_AT - timedelta(seconds=300))
    risk_input = context_risk_input_from_contracts(
        context_flags=result.context_flags,
        evaluation_time=RELEASE_AT - timedelta(seconds=300),
    )
    assert risk_input.event_window_active is True


def test_full_collection_publishes_exactly_ten_sector_records() -> None:
    cache = ContextStateCache()
    result = EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient()).collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    assert result.status is EIAWPSRCollectionStatus.SUCCESS
    assert len(result.indicator_snapshots) == 10
    assert {snapshot.ticker_or_sector for snapshot in result.indicator_snapshots} == {"OIL"}
    assert all(cache.get_sector("OIL", f"eia_wpsr_v1:{snapshot.indicator_name}:weekly", now=RELEASE_AT + timedelta(seconds=30)) is not None for snapshot in result.indicator_snapshots)
    ticker_entries = cache.latest_for_ticker("XOM", now=RELEASE_AT + timedelta(seconds=30))
    assert ticker_entries
    assert all("inventory" not in entry.key.name and "utilization" not in entry.key.name for entry in ticker_entries)


def test_sector_entries_appear_in_existing_context_snapshot() -> None:
    cache = ContextStateCache()
    EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient()).collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    snapshot = cache.to_context_state_snapshot(ticker="XOM", sector="OIL", now=RELEASE_AT + timedelta(seconds=31))
    sector_entries = snapshot.context_summary["sectors"]["OIL"]
    assert "eia_wpsr_v1:commercial_crude_inventory:weekly" in sector_entries


def test_exact_prior_week_is_required_and_response_order_is_irrelevant() -> None:
    result = EIAWPSRCollector(cache=ContextStateCache(), config=_config(), client=FakeClient()).collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    values = {item.indicator_name: item.value for item in result.indicator_snapshots}
    assert values["commercial_crude_inventory_change_wow"] == -8263.0
    assert values["refinery_utilization_change_wow"] == pytest.approx(1.4)


@pytest.mark.parametrize("days_earlier", [8, 14, 21])
def test_non_weekly_prior_gap_produces_level_only_partial(days_earlier: int) -> None:
    stocks = _records("weekly_stocks.json")
    utilization = _records("refinery_utilization.json")
    for record in stocks + utilization:
        if record["period"] == "2026-06-05":
            record["period"] = (date(2026, 6, 12) - timedelta(days=days_earlier)).isoformat()
    result = EIAWPSRCollector(cache=ContextStateCache(), config=_config(), client=FakeClient(stocks=stocks, utilization=utilization)).collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    assert result.status is EIAWPSRCollectionStatus.PARTIAL
    assert len(result.indicator_snapshots) == 5
    assert all(not item.indicator_name.endswith("_change_wow") for item in result.indicator_snapshots)
    assert {issue.issue_type for issue in result.issues} == {"EXPECTED_PRIOR_PERIOD_MISSING"}


def test_nonmatching_current_period_publishes_no_numeric_rows() -> None:
    stocks = _records("weekly_stocks.json")
    utilization = _records("refinery_utilization.json")
    for record in stocks + utilization:
        record["period"] = "2026-06-04" if record["period"] == "2026-06-12" else "2026-05-28"
    result = EIAWPSRCollector(cache=ContextStateCache(), config=_config(), client=FakeClient(stocks=stocks, utilization=utilization)).collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    assert result.indicator_snapshots == ()
    assert result.status is EIAWPSRCollectionStatus.NO_FRESH_DATA


def test_numeric_validity_uses_existing_inclusive_cache_boundary() -> None:
    cache = ContextStateCache()
    result = EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient()).collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    name = "eia_wpsr_v1:commercial_crude_inventory:weekly"
    assert cache.get_sector("OIL", name, now=NEXT_RELEASE_AT) is not None
    assert cache.get_sector("OIL", name, now=NEXT_RELEASE_AT + timedelta(microseconds=1)) is None
    assert cache.get_sector("OIL", name, now=NEXT_RELEASE_AT + timedelta(microseconds=1), include_expired=True) is not None
    assert {item.details["valid_until"] for item in result.indicator_snapshots} == {NEXT_RELEASE_AT.isoformat().replace("+00:00", "Z")}


def test_final_release_collects_with_bounded_fallback_validity() -> None:
    final_release = _release()
    max_staleness_seconds = 7200
    config = EIAWPSRConfig(
        event_windows_enabled=True,
        numeric_source_enabled=True,
        releases=(final_release,),
        oil_tickers=("XOM", "CVX"),
        max_staleness_seconds=max_staleness_seconds,
    )
    cache = ContextStateCache()
    client = FakeClient()
    result = EIAWPSRCollector(cache=cache, config=config, client=client).collect(
        evaluation_time=final_release.initial_fetch_at,
    )
    valid_until = final_release.release_at + timedelta(seconds=max_staleness_seconds)

    assert result.status is EIAWPSRCollectionStatus.SUCCESS
    assert client.calls == [STOCK_ROUTE, UTILIZATION_ROUTE]
    assert len(result.indicator_snapshots) == 10
    assert "NEXT_RELEASE_UNAVAILABLE" not in {issue.issue_type for issue in result.issues}
    assert {item.details["valid_until"] for item in result.indicator_snapshots} == {valid_until.isoformat().replace("+00:00", "Z")}
    for snapshot in result.indicator_snapshots:
        entry = cache.get_sector("OIL", f"eia_wpsr_v1:{snapshot.indicator_name}:weekly", now=final_release.initial_fetch_at)
        assert entry is not None
        assert entry.valid_until == valid_until


def test_final_release_retry_does_not_extend_fallback_validity() -> None:
    final_release = _release()
    config = EIAWPSRConfig(
        event_windows_enabled=True,
        numeric_source_enabled=True,
        releases=(final_release,),
        oil_tickers=("XOM", "CVX"),
        max_staleness_seconds=7200,
    )
    cache = ContextStateCache()
    EIAWPSRCollector(cache=cache, config=config, client=FakeClient()).collect(
        evaluation_time=final_release.initial_fetch_at,
    )
    name = "eia_wpsr_v1:commercial_crude_inventory:weekly"
    original = cache.get_sector("OIL", name, now=final_release.initial_fetch_at, include_expired=True)
    delayed = EIAWPSRCollector(cache=cache, config=config, client=FakeClient(stocks=[], utilization=[])).collect(
        evaluation_time=final_release.release_at + timedelta(seconds=3630),
        last_numeric_attempt_at=final_release.initial_fetch_at,
    )
    after = cache.get_sector("OIL", name, now=final_release.release_at + timedelta(seconds=3630), include_expired=True)

    assert delayed.data_status is EIAWPSRDataStatus.DATA_DELAYED
    assert original is not None and after is not None
    assert after.valid_until == original.valid_until == final_release.release_at + timedelta(seconds=7200)


def test_required_writer_is_checked_before_cache_mutation() -> None:
    cache = ContextStateCache()
    collector = EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient())
    with pytest.raises(EIAWPSRError, match="no writer"):
        collector.collect(
            evaluation_time=RELEASE_AT + timedelta(seconds=30),
            write_questdb=True,
            questdb_required=True,
        )
    assert cache.snapshot(now=RELEASE_AT + timedelta(seconds=30))["entry_count"] == 0


def test_optional_writer_failure_is_partial_and_duplicate_writes_are_suppressed() -> None:
    failing = FakeWriter(fail=True)
    partial = EIAWPSRCollector(cache=ContextStateCache(), config=_config(), client=FakeClient(), ledger_writer=failing).collect(
        evaluation_time=RELEASE_AT + timedelta(seconds=30),
        write_questdb=True,
    )
    assert partial.status is EIAWPSRCollectionStatus.PARTIAL
    assert "LEDGER_WRITE_FAILED" in {issue.issue_type for issue in partial.issues}

    cache = ContextStateCache()
    writer = FakeWriter()
    collector = EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient(), ledger_writer=writer)
    collector.collect(evaluation_time=RELEASE_AT + timedelta(seconds=30), write_questdb=True)
    first_counts = (len(writer.flags), len(writer.indicators))
    collector.collect(evaluation_time=RELEASE_AT + timedelta(seconds=30), write_questdb=True)
    assert (len(writer.flags), len(writer.indicators)) == first_counts


def test_missing_api_key_fails_safely_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    called: list[bool] = []
    client = EIAWPSRClient(request_get=lambda *args, **kwargs: called.append(True))
    with pytest.raises(EIAWPSRError, match="EIA_API_KEY missing"):
        client.fetch_weekly_records(STOCK_ROUTE, ["WCESTUS1"])
    assert called == []


def test_delayed_next_report_does_not_extend_old_validity() -> None:
    cache = ContextStateCache()
    first = EIAWPSRCollector(cache=cache, config=_config(), client=FakeClient())
    first.collect(evaluation_time=RELEASE_AT + timedelta(seconds=30))
    name = "eia_wpsr_v1:commercial_crude_inventory:weekly"
    original = cache.get_sector("OIL", name, now=NEXT_RELEASE_AT, include_expired=True)
    stale = FakeClient()
    delayed = EIAWPSRCollector(cache=cache, config=_config(), client=stale).collect(
        evaluation_time=NEXT_RELEASE_AT + timedelta(hours=1, seconds=31),
        last_numeric_attempt_at=NEXT_RELEASE_AT + timedelta(seconds=30),
    )
    after = cache.get_sector("OIL", name, now=NEXT_RELEASE_AT + timedelta(hours=1, seconds=31), include_expired=True)
    assert delayed.data_status is EIAWPSRDataStatus.DATA_DELAYED
    assert original is not None and after is not None
    assert after.updated_at == original.updated_at
    assert after.valid_until == NEXT_RELEASE_AT


def test_module_contains_no_scheduler_sleep_or_background_thread() -> None:
    import market_relay_engine.context.eia_wpsr as module

    source = inspect.getsource(module)
    for forbidden in ("time.sleep", "import threading", "while True"):
        assert forbidden not in source


def test_schedule_parser_applies_normal_and_holiday_release_times() -> None:
    html = """
    <table><tr><th>Data for the week ending</th><th>Alternate release date</th><th>Release day</th><th>Release time</th><th>Holiday</th></tr>
    <tr><td>September 4, 2026</td><td>September 10, 2026</td><td>Thursday</td><td>12:00 p.m.</td><td>Labor Day</td></tr></table>
    """
    candidates = parse_schedule_candidates(
        html,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 18),
    )
    by_period = {item["report_period"]: item for item in candidates}
    assert by_period["2026-09-04"]["release_at"] == "2026-09-10T12:00:00-04:00"
    assert by_period["2026-09-11"]["release_at"] == "2026-09-16T10:30:00-04:00"


def test_context_indicator_details_are_deep_copied_and_json_safe() -> None:
    details = {"nested": {"items": ["x"]}}
    snapshot = ContextIndicatorSnapshot(
        snapshot_time=RELEASE_AT,
        source="fixture",
        ticker_or_sector="OIL",
        indicator_name="metric",
        value=1.0,
        details=details,
    )
    details["nested"]["items"].append("changed")
    assert snapshot.details == {"nested": {"items": ["x"]}}
    with pytest.raises(TypeError, match="JSON serializable"):
        ContextIndicatorSnapshot(
            snapshot_time=RELEASE_AT,
            source="fixture",
            ticker_or_sector="OIL",
            indicator_name="metric",
            value=1.0,
            details={"bad": object()},
        )
    with pytest.raises(ValueError, match="Out of range float"):
        ContextIndicatorSnapshot(
            snapshot_time=RELEASE_AT,
            source="fixture",
            ticker_or_sector="OIL",
            indicator_name="metric",
            value=1.0,
            details={"bad": math.nan},
        )
