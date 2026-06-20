"""EIA Weekly Petroleum Status Report release flags and numeric context."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
import hashlib
import json
import math
import os
from typing import Any, Protocol

import requests

from market_relay_engine.common.time import (
    ensure_timezone_aware_utc,
    parse_utc_iso,
    to_utc_iso,
    utc_now,
)
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    ContextStateUpdateResult,
    ContextStateUpdateStatus,
    make_sector_context_entry,
    make_ticker_context_entry,
)
from market_relay_engine.contracts.context import ContextFlag, ContextIndicatorSnapshot


SOURCE_NAME = "eia_wpsr_v1"
FLAG_TYPE = "eia_wpsr_event_window"
SECTOR = "OIL"
API_ROOT = "https://api.eia.gov/v2"
SCHEDULE_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"


class EIAWPSRError(RuntimeError):
    """Raised when EIA WPSR configuration or collection cannot proceed safely."""


class EIAWPSRActionKind(str, Enum):
    REFRESH_RELEASE_WINDOW = "REFRESH_RELEASE_WINDOW"
    FETCH_NUMERIC_REPORT = "FETCH_NUMERIC_REPORT"
    RETRY_NUMERIC_REPORT = "RETRY_NUMERIC_REPORT"
    NO_ACTION = "NO_ACTION"


class EIAWPSRDataStatus(str, Enum):
    NOT_DUE = "NOT_DUE"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    DATA_DELAYED = "DATA_DELAYED"
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"


class EIAWPSRCollectionStatus(str, Enum):
    DISABLED = "DISABLED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NO_FRESH_DATA = "NO_FRESH_DATA"
    DATA_DELAYED = "DATA_DELAYED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class EIARelease:
    release_id: str
    release_at: datetime
    report_period: date
    pre_release_window_seconds: int = 300
    post_release_window_seconds: int = 900
    initial_fetch_delay_seconds: int = 30
    fast_retry_interval_seconds: int = 60
    fast_retry_window_seconds: int = 1200
    delayed_retry_interval_seconds: int = 3600

    def __post_init__(self) -> None:
        release_id = _required_string(self.release_id, "release_id")
        release_at = ensure_timezone_aware_utc(self.release_at)
        if not isinstance(self.report_period, date) or isinstance(self.report_period, datetime):
            raise EIAWPSRError("report_period must be a date")
        for field_name in (
            "pre_release_window_seconds",
            "post_release_window_seconds",
            "initial_fetch_delay_seconds",
            "fast_retry_interval_seconds",
            "fast_retry_window_seconds",
            "delayed_retry_interval_seconds",
        ):
            value = getattr(self, field_name)
            if field_name in {"fast_retry_interval_seconds", "delayed_retry_interval_seconds"}:
                _positive_int(value, field_name)
            else:
                _non_negative_int(value, field_name)
        if self.fast_retry_window_seconds < self.initial_fetch_delay_seconds:
            raise EIAWPSRError(
                "fast_retry_window_seconds must be at least initial_fetch_delay_seconds"
            )
        object.__setattr__(self, "release_id", release_id)
        object.__setattr__(self, "release_at", release_at)

    @property
    def window_start(self) -> datetime:
        return self.release_at - timedelta(seconds=self.pre_release_window_seconds)

    @property
    def window_end(self) -> datetime:
        return self.release_at + timedelta(seconds=self.post_release_window_seconds)

    @property
    def initial_fetch_at(self) -> datetime:
        return self.release_at + timedelta(seconds=self.initial_fetch_delay_seconds)

    @property
    def fast_retry_end(self) -> datetime:
        return self.release_at + timedelta(seconds=self.fast_retry_window_seconds)


@dataclass(frozen=True, kw_only=True)
class EIAWPSRConfig:
    event_windows_enabled: bool
    numeric_source_enabled: bool
    releases: tuple[EIARelease, ...]
    oil_tickers: tuple[str, ...]
    api_key_env: str = "EIA_API_KEY"
    timeout_seconds: float = 10.0
    max_staleness_seconds: int = 86400
    writes_questdb_ledger: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.event_windows_enabled, bool):
            raise EIAWPSRError("event_windows_enabled must be bool")
        if not isinstance(self.numeric_source_enabled, bool):
            raise EIAWPSRError("numeric_source_enabled must be bool")
        if self.numeric_source_enabled and not self.event_windows_enabled:
            raise EIAWPSRError("numeric EIA collection requires enabled release windows")
        releases = tuple(self.releases)
        _validate_release_order(releases)
        if self.event_windows_enabled and not releases:
            raise EIAWPSRError("enabled EIA configuration requires reviewed releases")
        tickers = tuple(sorted({_normalize_symbol(item) for item in self.oil_tickers}))
        if self.event_windows_enabled and not tickers:
            raise EIAWPSRError("enabled EIA configuration requires oil tickers")
        timeout = _positive_float(self.timeout_seconds, "timeout_seconds")
        max_staleness = _positive_int(self.max_staleness_seconds, "max_staleness_seconds")
        object.__setattr__(self, "releases", releases)
        object.__setattr__(self, "oil_tickers", tickers)
        object.__setattr__(self, "api_key_env", _required_string(self.api_key_env, "api_key_env"))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_staleness_seconds", max_staleness)

    @classmethod
    def from_repository_configs(
        cls,
        calendar_events: Mapping[str, Any],
        context_sources: Mapping[str, Any],
        symbols: Mapping[str, Any],
        **overrides: Any,
    ) -> "EIAWPSRConfig":
        eia_window = _required_mapping(
            _required_mapping(calendar_events, "event_windows"), "eia"
        )
        source = _required_mapping(
            _required_mapping(context_sources, "structured_sources"), "eia"
        )
        policy = {
            name: _required_non_negative_int(eia_window, name)
            for name in (
                "pre_release_window_seconds",
                "post_release_window_seconds",
                "initial_fetch_delay_seconds",
                "fast_retry_window_seconds",
            )
        }
        policy["fast_retry_interval_seconds"] = _required_positive_int(
            eia_window, "fast_retry_interval_seconds"
        )
        policy["delayed_retry_interval_seconds"] = _required_positive_int(
            eia_window, "delayed_retry_interval_seconds"
        )
        raw_releases = eia_window.get("releases")
        if not isinstance(raw_releases, list):
            raise EIAWPSRError("event_windows.eia.releases must be a list")
        releases: list[EIARelease] = []
        for index, raw in enumerate(raw_releases):
            if not isinstance(raw, Mapping):
                raise EIAWPSRError(f"release {index} must be a mapping")
            releases.append(
                EIARelease(
                    release_id=_required_value(raw, "release_id"),
                    release_at=_parse_release_at(_required_value(raw, "release_at")),
                    report_period=_parse_date(_required_value(raw, "report_period"), "report_period"),
                    **policy,
                )
            )
        values: dict[str, Any] = {
            "event_windows_enabled": eia_window.get("enabled", False),
            "numeric_source_enabled": source.get("enabled", False),
            "releases": tuple(releases),
            "oil_tickers": derive_oil_tickers(symbols),
            "api_key_env": source.get("api_key_env", "EIA_API_KEY"),
            "timeout_seconds": source.get("timeout_seconds", 10.0),
            "max_staleness_seconds": _required_positive_int(source, "max_staleness_seconds"),
            "writes_questdb_ledger": source.get("writes_questdb_ledger", True),
        }
        values.update(overrides)
        return cls(**values)


@dataclass(frozen=True, kw_only=True)
class EIAWPSRActionPlan:
    release_id: str | None
    action_kind: EIAWPSRActionKind
    due_at: datetime | None
    next_action_at: datetime | None
    expected_report_period: date | None
    data_status: EIAWPSRDataStatus
    next_release_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class EIAWPSRIssue:
    issue_type: str
    message: str
    metric: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class EIAWPSRCollectionResult:
    status: EIAWPSRCollectionStatus
    action_plan: EIAWPSRActionPlan
    expected_report_period: date | None
    last_seen_report_period: date | None
    next_retry_at: datetime | None
    data_status: EIAWPSRDataStatus
    context_flags: tuple[ContextFlag, ...] = ()
    indicator_snapshots: tuple[ContextIndicatorSnapshot, ...] = ()
    cache_update_results: tuple[ContextStateUpdateResult, ...] = ()
    ledger_write_results: tuple[object, ...] = ()
    issues: tuple[EIAWPSRIssue, ...] = ()


@dataclass(frozen=True)
class EIAMetricSpec:
    indicator_name: str
    route: str
    series_id: str
    units: str
    change_units: str
    facets: Mapping[str, str]


STOCK_ROUTE = "petroleum/stoc/wstk"
UTILIZATION_ROUTE = "petroleum/pnp/wiup"
METRICS = (
    EIAMetricSpec("commercial_crude_inventory", STOCK_ROUTE, "WCESTUS1", "MBBL", "MBBL", {"duoarea": "NUS", "product": "EPC0", "process": "SAX"}),
    EIAMetricSpec("cushing_crude_inventory", STOCK_ROUTE, "W_EPC0_SAX_YCUOK_MBBL", "MBBL", "MBBL", {"duoarea": "YCUOK", "product": "EPC0", "process": "SAX"}),
    EIAMetricSpec("motor_gasoline_inventory", STOCK_ROUTE, "WGTSTUS1", "MBBL", "MBBL", {"duoarea": "NUS", "product": "EPM0", "process": "SAE"}),
    EIAMetricSpec("distillate_fuel_oil_inventory", STOCK_ROUTE, "WDISTUS1", "MBBL", "MBBL", {"duoarea": "NUS", "product": "EPD0", "process": "SAE"}),
    EIAMetricSpec("refinery_utilization", UTILIZATION_ROUTE, "WPULEUS3", "%", "percentage_points", {"duoarea": "NUS", "product": "(NA)", "process": "YUP"}),
)


class EIADataClient(Protocol):
    def fetch_weekly_records(
        self, route: str, series_ids: Sequence[str], *, observations_per_series: int = 3
    ) -> list[dict[str, object]]:
        ...


class EIAWPSRWriter(Protocol):
    def write_context_indicator_snapshot(self, snapshot: ContextIndicatorSnapshot, **kwargs: Any) -> object | None:
        ...

    def write_context_flag(self, flag: ContextFlag, **kwargs: Any) -> object | None:
        ...


class EIAWPSRClient:
    """Small credential-safe client for the two official EIA weekly routes."""

    def __init__(
        self,
        *,
        api_key_env: str = "EIA_API_KEY",
        timeout_seconds: float = 10.0,
        request_get: Callable[..., Any] = requests.get,
    ) -> None:
        self.api_key_env = _required_string(api_key_env, "api_key_env")
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.request_get = request_get

    def fetch_weekly_records(
        self, route: str, series_ids: Sequence[str], *, observations_per_series: int = 3
    ) -> list[dict[str, object]]:
        key = os.getenv(self.api_key_env)
        if not key:
            raise EIAWPSRError(f"{self.api_key_env} missing")
        series = tuple(_required_string(value, "series_id") for value in series_ids)
        params: list[tuple[str, str]] = [
            ("api_key", key),
            ("frequency", "weekly"),
            ("data[0]", "value"),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "desc"),
            ("offset", "0"),
            ("length", str(max(2, observations_per_series) * len(series))),
        ]
        params.extend(("facets[series][]", value) for value in series)
        try:
            response = self.request_get(
                f"{API_ROOT}/{route}/data/",
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise EIAWPSRError(f"official EIA request failed for route {route}") from exc
        if response.status_code != 200:
            raise EIAWPSRError(
                f"official EIA route {route} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise EIAWPSRError(f"official EIA route {route} returned invalid JSON") from exc
        body = payload.get("response") if isinstance(payload, Mapping) else None
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, list):
            raise EIAWPSRError(f"official EIA route {route} response has no data list")
        return [dict(item) for item in data if isinstance(item, Mapping)]


def plan_eia_wpsr_action(
    *,
    releases: Sequence[EIARelease],
    evaluation_time: datetime,
    last_numeric_attempt_at: datetime | None,
    last_successful_report_period: date | None,
) -> EIAWPSRActionPlan:
    """Return one deterministic action without reading a clock or doing I/O."""
    now = ensure_timezone_aware_utc(evaluation_time)
    ordered = tuple(releases)
    _validate_release_order(ordered)
    last_attempt = (
        None
        if last_numeric_attempt_at is None
        else ensure_timezone_aware_utc(last_numeric_attempt_at)
    )
    if not ordered:
        return EIAWPSRActionPlan(
            release_id=None,
            action_kind=EIAWPSRActionKind.NO_ACTION,
            due_at=None,
            next_action_at=None,
            expected_report_period=None,
            data_status=EIAWPSRDataStatus.NOT_DUE,
        )

    target_index = _target_release_index(ordered, now)
    release = ordered[target_index]
    next_release = ordered[target_index + 1] if target_index + 1 < len(ordered) else None
    effective_last_attempt = last_attempt if _attempt_belongs_to_release(ordered, target_index, last_attempt) else None
    effective_successful_report_period = (
        last_successful_report_period
        if last_successful_report_period == release.report_period
        else None
    )

    if effective_successful_report_period == release.report_period:
        return EIAWPSRActionPlan(
            release_id=release.release_id,
            action_kind=EIAWPSRActionKind.NO_ACTION,
            due_at=None,
            next_action_at=None if next_release is None else next_release.window_start,
            expected_report_period=release.report_period,
            data_status=EIAWPSRDataStatus.CURRENT,
            next_release_id=None if next_release is None else next_release.release_id,
        )

    if now < release.window_start:
        return _plan(release, EIAWPSRActionKind.NO_ACTION, None, release.window_start, EIAWPSRDataStatus.NOT_DUE)
    if now < release.initial_fetch_at:
        return _plan(release, EIAWPSRActionKind.REFRESH_RELEASE_WINDOW, release.window_start, release.initial_fetch_at, EIAWPSRDataStatus.NOT_DUE)

    if now <= release.fast_retry_end:
        due = release.initial_fetch_at if effective_last_attempt is None else effective_last_attempt + timedelta(seconds=release.fast_retry_interval_seconds)
        if now >= due:
            kind = EIAWPSRActionKind.FETCH_NUMERIC_REPORT if effective_last_attempt is None else EIAWPSRActionKind.RETRY_NUMERIC_REPORT
            return _plan(release, kind, due, now + timedelta(seconds=release.fast_retry_interval_seconds), EIAWPSRDataStatus.WAITING_FOR_DATA)
        if release.window_start <= now <= release.window_end:
            return _plan(release, EIAWPSRActionKind.REFRESH_RELEASE_WINDOW, release.window_start, due, EIAWPSRDataStatus.WAITING_FOR_DATA)
        return _plan(release, EIAWPSRActionKind.NO_ACTION, None, due, EIAWPSRDataStatus.WAITING_FOR_DATA)

    delayed_due = now if effective_last_attempt is None else effective_last_attempt + timedelta(seconds=release.delayed_retry_interval_seconds)
    if now >= delayed_due:
        return _plan(release, EIAWPSRActionKind.RETRY_NUMERIC_REPORT, delayed_due, now + timedelta(seconds=release.delayed_retry_interval_seconds), EIAWPSRDataStatus.DATA_DELAYED)
    return _plan(release, EIAWPSRActionKind.NO_ACTION, None, delayed_due, EIAWPSRDataStatus.DATA_DELAYED)


class EIAWPSRCollector:
    def __init__(
        self,
        *,
        cache: ContextStateCache,
        config: EIAWPSRConfig,
        client: EIADataClient | None = None,
        ledger_writer: EIAWPSRWriter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.cache = cache
        self.config = config
        self.client = client or EIAWPSRClient(
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
        )
        self.ledger_writer = ledger_writer
        self.clock = clock

    def collect(
        self,
        *,
        evaluation_time: datetime | None = None,
        last_numeric_attempt_at: datetime | None = None,
        last_successful_report_period: date | None = None,
        write_questdb: bool = False,
        questdb_required: bool = False,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> EIAWPSRCollectionResult:
        if write_questdb and questdb_required and self.ledger_writer is None:
            raise EIAWPSRError("QuestDB writes are required but no writer was provided")
        now = ensure_timezone_aware_utc(evaluation_time or self.clock())
        if not self.config.event_windows_enabled and not self.config.numeric_source_enabled:
            plan = plan_eia_wpsr_action(releases=(), evaluation_time=now, last_numeric_attempt_at=last_numeric_attempt_at, last_successful_report_period=last_successful_report_period)
            return EIAWPSRCollectionResult(status=EIAWPSRCollectionStatus.DISABLED, action_plan=plan, expected_report_period=None, last_seen_report_period=None, next_retry_at=None, data_status=EIAWPSRDataStatus.NOT_DUE)

        plan = plan_eia_wpsr_action(
            releases=self.config.releases,
            evaluation_time=now,
            last_numeric_attempt_at=last_numeric_attempt_at,
            last_successful_report_period=last_successful_report_period,
        )
        issues: list[EIAWPSRIssue] = []
        flags: list[ContextFlag] = []
        snapshots: list[ContextIndicatorSnapshot] = []
        cache_results: list[ContextStateUpdateResult] = []
        ledger_results: list[object] = []

        for release in self.config.releases:
            if release.window_start <= now <= release.window_end:
                for ticker in self.config.oil_tickers:
                    flag = _build_release_flag(release, ticker)
                    flags.append(flag)
                    details = {
                        "release_id": release.release_id,
                        "release_at": to_utc_iso(release.release_at),
                        "window_starts_at": to_utc_iso(release.window_start),
                        "window_ends_at": to_utc_iso(release.window_end),
                        "context_flag_id": flag.context_flag_id,
                        "flag_type": flag.flag_type,
                        "severity": flag.severity,
                        "sector": SECTOR,
                    }
                    update = self.cache.update(make_ticker_context_entry(ticker=ticker, name=f"{SOURCE_NAME}:event_window_active:{release.release_id}", value=True, updated_at=flag.event_time, source=SOURCE_NAME, source_event_time=release.release_at, valid_until=release.window_end, details=details))
                    cache_results.append(update)
                    self._write_if_changed(flag, update, ledger_results, issues, write_questdb, questdb_required, run_id, session_id)

        if plan.data_status is EIAWPSRDataStatus.SUPERSEDED:
            return _result(EIAWPSRCollectionStatus.SUPERSEDED, plan, None, flags, snapshots, cache_results, ledger_results, issues)

        numeric_due = plan.action_kind in {EIAWPSRActionKind.FETCH_NUMERIC_REPORT, EIAWPSRActionKind.RETRY_NUMERIC_REPORT}
        if not numeric_due or not self.config.numeric_source_enabled:
            status = EIAWPSRCollectionStatus.PARTIAL if flags and issues else (EIAWPSRCollectionStatus.SUCCESS if flags else EIAWPSRCollectionStatus.NO_FRESH_DATA)
            return _result(status, plan, None, flags, snapshots, cache_results, ledger_results, issues)

        release_index = next((index for index, item in enumerate(self.config.releases) if item.release_id == plan.release_id), None)
        if release_index is None:
            raise EIAWPSRError("action plan release is absent from configuration")
        release = self.config.releases[release_index]
        next_release = self.config.releases[release_index + 1] if release_index + 1 < len(self.config.releases) else None
        valid_until = next_release.release_at if next_release is not None else release.release_at + timedelta(seconds=self.config.max_staleness_seconds)

        records_by_route: dict[str, list[dict[str, object]]] = {}
        for route in (STOCK_ROUTE, UTILIZATION_ROUTE):
            specs = [spec for spec in METRICS if spec.route == route]
            try:
                records_by_route[route] = self.client.fetch_weekly_records(route, [spec.series_id for spec in specs], observations_per_series=3)
            except Exception as exc:  # noqa: BLE001 - source adapter boundary.
                issues.append(EIAWPSRIssue(issue_type="SOURCE_REQUEST_FAILED", message=str(exc), details={"route": route}))

        seen_periods: list[date] = []
        for route_records in records_by_route.values():
            for record in route_records:
                try:
                    seen_periods.append(_parse_date(record.get("period"), "period"))
                except EIAWPSRError:
                    continue

        for spec in METRICS:
            builds, metric_issues = _normalize_metric(spec, records_by_route.get(spec.route, []), release, valid_until, now)
            issues.extend(metric_issues)
            for snapshot, details in builds:
                update = self.cache.update(make_sector_context_entry(sector=SECTOR, name=f"{SOURCE_NAME}:{snapshot.indicator_name}:weekly", value=snapshot.value, updated_at=now, source=SOURCE_NAME, source_event_time=release.release_at, valid_until=valid_until, details=details))
                cache_results.append(update)
                snapshots.append(snapshot)
                self._write_if_changed(snapshot, update, ledger_results, issues, write_questdb, questdb_required, run_id, session_id)

        last_seen = max(seen_periods) if seen_periods else None
        current_count = sum(1 for item in snapshots if not item.indicator_name.endswith("_change_wow"))
        if current_count == len(METRICS):
            data_status = EIAWPSRDataStatus.CURRENT
            status = EIAWPSRCollectionStatus.PARTIAL if issues or len(snapshots) != len(METRICS) * 2 else EIAWPSRCollectionStatus.SUCCESS
            next_retry = None
        elif plan.data_status is EIAWPSRDataStatus.DATA_DELAYED:
            data_status = EIAWPSRDataStatus.DATA_DELAYED
            status = EIAWPSRCollectionStatus.DATA_DELAYED if not snapshots else EIAWPSRCollectionStatus.PARTIAL
            next_retry = now + timedelta(seconds=release.delayed_retry_interval_seconds)
            issues.append(EIAWPSRIssue(issue_type="DATA_DELAYED", message="expected WPSR report period is still unavailable"))
        else:
            data_status = EIAWPSRDataStatus.WAITING_FOR_DATA
            status = EIAWPSRCollectionStatus.NO_FRESH_DATA if not snapshots else EIAWPSRCollectionStatus.PARTIAL
            next_retry = now + timedelta(seconds=release.fast_retry_interval_seconds)
        adjusted_plan = EIAWPSRActionPlan(release_id=plan.release_id, action_kind=plan.action_kind, due_at=plan.due_at, next_action_at=next_retry if next_retry is not None else (None if next_release is None else next_release.window_start), expected_report_period=plan.expected_report_period, data_status=data_status, next_release_id=plan.next_release_id)
        return EIAWPSRCollectionResult(status=status, action_plan=adjusted_plan, expected_report_period=release.report_period, last_seen_report_period=last_seen, next_retry_at=next_retry, data_status=data_status, context_flags=tuple(flags), indicator_snapshots=tuple(snapshots), cache_update_results=tuple(cache_results), ledger_write_results=tuple(ledger_results), issues=tuple(issues))

    def _write_if_changed(self, record: ContextFlag | ContextIndicatorSnapshot, update: ContextStateUpdateResult, ledger_results: list[object], issues: list[EIAWPSRIssue], write_questdb: bool, required: bool, run_id: str | None, session_id: str | None) -> None:
        if not write_questdb or not self.config.writes_questdb_ledger or update.status not in {ContextStateUpdateStatus.WRITTEN, ContextStateUpdateStatus.REPLACED}:
            return
        if self.ledger_writer is None:
            return
        try:
            result = self.ledger_writer.write_context_flag(record, run_id=run_id, session_id=session_id) if isinstance(record, ContextFlag) else self.ledger_writer.write_context_indicator_snapshot(record, run_id=run_id, session_id=session_id)
            if result is not None:
                ledger_results.append(result)
        except Exception as exc:  # noqa: BLE001 - protocol writer boundary.
            issue = EIAWPSRIssue(issue_type="LEDGER_WRITE_FAILED", message=str(exc))
            issues.append(issue)
            if required:
                raise EIAWPSRError(issue.message) from exc


def derive_oil_tickers(symbols: Mapping[str, Any]) -> tuple[str, ...]:
    universe = symbols.get("tradable_universe")
    if not isinstance(universe, Mapping):
        raise EIAWPSRError("tradable_universe must be a mapping")
    tickers: set[str] = set()
    for group in universe.values():
        if not isinstance(group, Mapping) or group.get("enabled") is not True:
            continue
        entries = group.get("symbols")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and _normalize_symbol(entry.get("sector")) == SECTOR:
                tickers.add(_normalize_symbol(entry.get("ticker")))
    return tuple(sorted(tickers))


def deterministic_context_flag_id(release: EIARelease, ticker: str) -> str:
    payload = {"release_at": to_utc_iso(release.release_at), "release_id": release.release_id, "source": SOURCE_NAME, "ticker": _normalize_symbol(ticker), "window_ends_at": to_utc_iso(release.window_end), "window_starts_at": to_utc_iso(release.window_start)}
    return _deterministic_id("context_flag", payload)


def deterministic_context_indicator_id(release: EIARelease, spec: EIAMetricSpec, indicator_name: str) -> str:
    payload = {"indicator_name": indicator_name, "release_id": release.release_id, "report_period": release.report_period.isoformat(), "series_id": spec.series_id, "source": SOURCE_NAME, "source_event_time": to_utc_iso(release.release_at)}
    return _deterministic_id("context_indicator", payload)


def _normalize_metric(spec: EIAMetricSpec, records: Sequence[Mapping[str, object]], release: EIARelease, valid_until: datetime, collected_at: datetime) -> tuple[list[tuple[ContextIndicatorSnapshot, dict[str, object]]], list[EIAWPSRIssue]]:
    issues: list[EIAWPSRIssue] = []
    by_period: dict[date, list[Mapping[str, object]]] = {}
    parsed_records: list[tuple[date, Mapping[str, object]]] = []
    for record in records:
        if record.get("series") != spec.series_id:
            continue
        try:
            period = _parse_date(record.get("period"), "period")
        except EIAWPSRError:
            continue
        parsed_records.append((period, record))
    for period, record in sorted(parsed_records, key=lambda item: item[0]):
        by_period.setdefault(period, []).append(record)
    current, current_issue = _select_record(by_period.get(release.report_period, []), spec, "CURRENT_PERIOD_INVALID")
    if current_issue:
        issues.append(EIAWPSRIssue(issue_type=current_issue, message="configured current report period is missing, invalid, or ambiguous", metric=spec.indicator_name, details={"expected_report_period": release.report_period.isoformat()}))
        return [], issues
    assert current is not None
    current_value = _numeric_value(current.get("value"))
    expected_prior = release.report_period - timedelta(days=7)
    prior, prior_issue = _select_record(by_period.get(expected_prior, []), spec, "EXPECTED_PRIOR_PERIOD_MISSING")
    details: dict[str, object] = {"release_id": release.release_id, "release_at": to_utc_iso(release.release_at), "report_period": release.report_period.isoformat(), "expected_prior_period": expected_prior.isoformat(), "actual_prior_period": None if prior is None else expected_prior.isoformat(), "route": f"/v2/{spec.route}/data/", "series_id": spec.series_id, "facets": dict(spec.facets), "source_units": spec.units, "collection_verification_status": "CURRENT_VERIFIED" if prior is None else "CURRENT_AND_PRIOR_VERIFIED", "valid_until": to_utc_iso(valid_until)}
    level = ContextIndicatorSnapshot(snapshot_time=collected_at, source=SOURCE_NAME, ticker_or_sector=SECTOR, indicator_name=spec.indicator_name, value=current_value, context_indicator_id=deterministic_context_indicator_id(release, spec, spec.indicator_name), window="weekly", units=spec.units, freshness_seconds=max(0.0, (collected_at - release.release_at).total_seconds()), source_event_time=release.release_at, details=details)
    builds = [(level, details)]
    if prior_issue:
        issues.append(EIAWPSRIssue(issue_type=prior_issue, message="exact prior weekly period is missing, invalid, or ambiguous", metric=spec.indicator_name, details={"expected_prior_period": expected_prior.isoformat()}))
        return builds, issues
    assert prior is not None
    prior_value = _numeric_value(prior.get("value"))
    change_name = f"{spec.indicator_name}_change_wow"
    change_details = dict(details)
    change = ContextIndicatorSnapshot(snapshot_time=collected_at, source=SOURCE_NAME, ticker_or_sector=SECTOR, indicator_name=change_name, value=current_value - prior_value, context_indicator_id=deterministic_context_indicator_id(release, spec, change_name), window="weekly", units=spec.change_units, freshness_seconds=max(0.0, (collected_at - release.release_at).total_seconds()), source_event_time=release.release_at, details=change_details)
    builds.append((change, change_details))
    return builds, issues


def _select_record(records: Sequence[Mapping[str, object]], spec: EIAMetricSpec, issue: str) -> tuple[Mapping[str, object] | None, str | None]:
    usable: list[Mapping[str, object]] = []
    for record in records:
        try:
            _numeric_value(record.get("value"))
        except EIAWPSRError:
            continue
        if record.get("units") != spec.units:
            continue
        if any(record.get(key) != value for key, value in spec.facets.items()):
            continue
        usable.append(record)
    if not usable:
        return None, issue
    identities = {(str(item.get("value")), tuple((key, item.get(key)) for key in sorted(spec.facets))) for item in usable}
    if len(identities) != 1:
        return None, issue
    return usable[0], None


def _build_release_flag(release: EIARelease, ticker: str) -> ContextFlag:
    return ContextFlag(event_time=release.window_start, source=SOURCE_NAME, flag_type=FLAG_TYPE, severity="NORMAL", context_flag_id=deterministic_context_flag_id(release, ticker), ticker=ticker, sector=SECTOR, valid_until=release.window_end)


def _result(status: EIAWPSRCollectionStatus, plan: EIAWPSRActionPlan, last_seen: date | None, flags: list[ContextFlag], snapshots: list[ContextIndicatorSnapshot], cache_results: list[ContextStateUpdateResult], ledger_results: list[object], issues: list[EIAWPSRIssue]) -> EIAWPSRCollectionResult:
    return EIAWPSRCollectionResult(status=status, action_plan=plan, expected_report_period=plan.expected_report_period, last_seen_report_period=last_seen, next_retry_at=plan.next_action_at if plan.data_status in {EIAWPSRDataStatus.WAITING_FOR_DATA, EIAWPSRDataStatus.DATA_DELAYED} else None, data_status=plan.data_status, context_flags=tuple(flags), indicator_snapshots=tuple(snapshots), cache_update_results=tuple(cache_results), ledger_write_results=tuple(ledger_results), issues=tuple(issues))


def _plan(release: EIARelease, action: EIAWPSRActionKind, due: datetime | None, next_action: datetime | None, status: EIAWPSRDataStatus) -> EIAWPSRActionPlan:
    return EIAWPSRActionPlan(release_id=release.release_id, action_kind=action, due_at=due, next_action_at=next_action, expected_report_period=release.report_period, data_status=status)


def _target_release_index(
    releases: Sequence[EIARelease],
    now: datetime,
) -> int:
    selected = 0
    for index, release in enumerate(releases):
        if release.window_start <= now:
            selected = index
        else:
            break
    return selected


def _attempt_belongs_to_release(releases: Sequence[EIARelease], index: int, attempt: datetime | None) -> bool:
    if attempt is None or attempt < releases[index].initial_fetch_at:
        return False
    return index + 1 >= len(releases) or attempt < releases[index + 1].window_start


def _validate_release_order(releases: Sequence[EIARelease]) -> None:
    ids: set[str] = set()
    timestamps: set[datetime] = set()
    periods: set[date] = set()
    previous: datetime | None = None
    previous_period: date | None = None
    for release in releases:
        if not isinstance(release, EIARelease):
            raise EIAWPSRError("releases must contain EIARelease values")
        if release.release_id in ids:
            raise EIAWPSRError("duplicate release_id")
        if release.release_at in timestamps:
            raise EIAWPSRError("duplicate release_at")
        if release.report_period in periods:
            raise EIAWPSRError("duplicate report_period")
        if previous is not None and release.release_at <= previous:
            raise EIAWPSRError("releases must be strictly ordered by release_at")
        if previous_period is not None and release.report_period <= previous_period:
            raise EIAWPSRError("releases must be strictly ordered by report_period")
        ids.add(release.release_id); timestamps.add(release.release_at); periods.add(release.report_period); previous = release.release_at; previous_period = release.report_period


def _parse_release_at(value: object) -> datetime:
    if isinstance(value, datetime):
        return ensure_timezone_aware_utc(value)
    if not isinstance(value, str):
        raise EIAWPSRError("release_at must be an ISO-8601 string")
    try:
        return parse_utc_iso(value)
    except (TypeError, ValueError) as exc:
        raise EIAWPSRError("release_at must be offset-aware ISO-8601") from exc


def _parse_date(value: object, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise EIAWPSRError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise EIAWPSRError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise EIAWPSRError(f"{field_name} must use YYYY-MM-DD")
    return parsed


def _numeric_value(value: object) -> float:
    if isinstance(value, bool):
        raise EIAWPSRError("EIA value must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EIAWPSRError("EIA value must be numeric") from exc
    if not math.isfinite(result):
        raise EIAWPSRError("EIA value must be finite")
    return result


def _deterministic_id(prefix: str, payload: Mapping[str, object]) -> str:
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"{prefix}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]}"


def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise EIAWPSRError(f"{key} must be a mapping")
    return value


def _required_value(mapping: Mapping[str, Any], key: str) -> Any:
    if key not in mapping:
        raise EIAWPSRError(f"missing required EIA configuration field: {key}")
    return mapping[key]


def _required_non_negative_int(mapping: Mapping[str, Any], key: str) -> int:
    return _non_negative_int(_required_value(mapping, key), key)


def _required_positive_int(mapping: Mapping[str, Any], key: str) -> int:
    return _positive_int(_required_value(mapping, key), key)


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EIAWPSRError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_symbol(value: object) -> str:
    return _required_string(value, "symbol").upper()


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EIAWPSRError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: object, field_name: str) -> int:
    result = _non_negative_int(value, field_name)
    if result <= 0:
        raise EIAWPSRError(f"{field_name} must be positive")
    return result


def _positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise EIAWPSRError(f"{field_name} must be positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EIAWPSRError(f"{field_name} must be positive") from exc
    if not math.isfinite(result) or result <= 0:
        raise EIAWPSRError(f"{field_name} must be positive")
    return result


__all__ = [
    "EIARelease", "EIAWPSRActionKind", "EIAWPSRActionPlan", "EIAWPSRClient",
    "EIAWPSRCollectionResult", "EIAWPSRCollectionStatus", "EIAWPSRConfig",
    "EIAWPSRDataStatus", "EIAWPSRError", "EIAWPSRIssue", "EIAWPSRCollector",
    "METRICS", "SOURCE_NAME", "derive_oil_tickers", "plan_eia_wpsr_action",
]
