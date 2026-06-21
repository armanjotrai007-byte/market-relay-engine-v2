"""Bounded one-shot FRED Treasury-rate context collection."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import math
import os
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from market_relay_engine.common.time import ensure_timezone_aware_utc, to_utc_iso, utc_now
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    ContextStateEntry,
    ContextStateUpdateResult,
    ContextStateUpdateStatus,
    make_global_context_entry,
)
from market_relay_engine.contracts.context import ContextIndicatorSnapshot


SOURCE_NAME = "fred_rates_v1"
API_URL = "https://api.stlouisfed.org/fred/series/observations"
GLOBAL_SCOPE = "GLOBAL"
_ZERO = timedelta(0)
_HOUR = timedelta(hours=1)


def _first_sunday_on_or_after(value: datetime) -> datetime:
    days_to_go = 6 - value.weekday()
    if days_to_go:
        value += timedelta(days=days_to_go)
    return value


def _us_dst_range(year: int) -> tuple[datetime, datetime]:
    """Return post-2007 U.S. DST boundaries used by America/New_York."""
    start = _first_sunday_on_or_after(datetime(year, 3, 8, 2))
    end = _first_sunday_on_or_after(datetime(year, 11, 1, 2))
    return start, end


class _BundledEasternTime(tzinfo):
    """Dependency-free Windows fallback for post-2007 America/New_York rules."""

    key = "America/New_York"
    _standard_offset = timedelta(hours=-5)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._standard_offset + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None or dt.tzinfo is None:
            return _ZERO
        start, end = _us_dst_range(dt.year)
        naive = dt.replace(tzinfo=None)
        if start + _HOUR <= naive < end - _HOUR:
            return _HOUR
        if end - _HOUR <= naive < end:
            return _ZERO if dt.fold else _HOUR
        if start <= naive < start + _HOUR:
            return _HOUR if dt.fold else _ZERO
        return _ZERO

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc requires a datetime with matching tzinfo")
        start, end = _us_dst_range(dt.year)
        start = start.replace(tzinfo=self)
        end = end.replace(tzinfo=self)
        standard_time = dt + self._standard_offset
        daylight_time = standard_time + _HOUR
        if end <= daylight_time < end + _HOUR:
            return standard_time.replace(fold=1)
        if standard_time < start or daylight_time >= end:
            return standard_time
        return daylight_time


def _load_new_york_timezone() -> tzinfo:
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return _BundledEasternTime()


NEW_YORK = _load_new_york_timezone()
SOURCE_EVENT_TIME_BASIS = "observation_date_utc_midnight_convention"
AVAILABILITY_BASIS = "collector_observed"
VINTAGE_TRACKING_MODE = "current_fred_unpinned_v1"
SPREAD_DERIVATION_VERSION = "fred_treasury_spread_v1"
CHANGE_DERIVATION_VERSION = "fred_previous_valid_observation_change_v1"
REGIME_DERIVATION_VERSION = "fred_rate_curve_regime_v1"


class FREDCollectorError(RuntimeError):
    """Raised when FRED configuration or collection cannot proceed safely."""


@dataclass(frozen=True)
class FREDSeriesSpec:
    indicator_name: str
    series_id: str
    series_name: str


SERIES_SPECS = (
    FREDSeriesSpec(
        "us_treasury_3m_yield",
        "DGS3MO",
        "U.S. Treasury 3-month constant-maturity yield",
    ),
    FREDSeriesSpec(
        "us_treasury_2y_yield",
        "DGS2",
        "U.S. Treasury 2-year constant-maturity yield",
    ),
    FREDSeriesSpec(
        "us_treasury_10y_yield",
        "DGS10",
        "U.S. Treasury 10-year constant-maturity yield",
    ),
)
EXPECTED_SERIES_IDS = {spec.indicator_name: spec.series_id for spec in SERIES_SPECS}
_SPEC_BY_NAME = {spec.indicator_name: spec for spec in SERIES_SPECS}


@dataclass(frozen=True, kw_only=True)
class FREDConfig:
    enabled: bool = False
    api_key_env: str = "FRED_API_KEY"
    purpose: str = "Slow daily Treasury-rate context."
    feeds_memory_cache: bool = True
    writes_questdb_ledger: bool = True
    used_in_per_tick_loop: bool = False
    timeout_seconds: float = 10.0
    max_observation_age_calendar_days: int = 5
    observation_fetch_limit: int = 20
    series_ids: Mapping[str, str] = field(default_factory=lambda: dict(EXPECTED_SERIES_IDS))

    def __post_init__(self) -> None:
        for name in (
            "enabled",
            "feeds_memory_cache",
            "writes_questdb_ledger",
            "used_in_per_tick_loop",
        ):
            if not isinstance(getattr(self, name), bool):
                raise FREDCollectorError(f"{name} must be bool")
        if self.feeds_memory_cache is not True:
            raise FREDCollectorError("feeds_memory_cache must be true")
        if self.used_in_per_tick_loop is not False:
            raise FREDCollectorError("used_in_per_tick_loop must be false")
        api_key_env = _required_string(self.api_key_env, "api_key_env")
        purpose = _required_string(self.purpose, "purpose")
        timeout = _positive_float(self.timeout_seconds, "timeout_seconds")
        max_age = _non_negative_int(
            self.max_observation_age_calendar_days,
            "max_observation_age_calendar_days",
        )
        fetch_limit = _bounded_int(
            self.observation_fetch_limit,
            "observation_fetch_limit",
            minimum=4,
            maximum=50,
        )
        series_ids = dict(self.series_ids) if isinstance(self.series_ids, Mapping) else None
        if series_ids != EXPECTED_SERIES_IDS:
            raise FREDCollectorError(
                "series_ids must contain exactly DGS3MO, DGS2, and DGS10 under their required indicators"
            )
        object.__setattr__(self, "api_key_env", api_key_env)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_observation_age_calendar_days", max_age)
        object.__setattr__(self, "observation_fetch_limit", fetch_limit)
        object.__setattr__(self, "series_ids", series_ids)

    @classmethod
    def from_repository_config(cls, context_sources: Mapping[str, Any]) -> "FREDConfig":
        structured = _required_mapping(context_sources, "structured_sources")
        source = _required_mapping(structured, "fred")
        required_fields = {
            "enabled",
            "api_key_env",
            "purpose",
            "feeds_memory_cache",
            "writes_questdb_ledger",
            "used_in_per_tick_loop",
            "timeout_seconds",
            "max_observation_age_calendar_days",
            "observation_fetch_limit",
            "series_ids",
        }
        missing = sorted(required_fields.difference(source))
        if missing:
            raise FREDCollectorError(
                f"missing required FRED configuration field: {missing[0]}"
            )
        unexpected = sorted(set(source).difference(required_fields))
        if unexpected:
            raise FREDCollectorError(f"unexpected FRED configuration field: {unexpected[0]}")
        return cls(**{name: source[name] for name in required_fields})


class FREDCollectionStatus(str, Enum):
    DISABLED = "DISABLED"
    FAILED = "FAILED"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    SUCCESS = "SUCCESS"


class FREDSeriesStatus(str, Enum):
    REQUEST_FAILED = "REQUEST_FAILED"
    NO_VALID_OBSERVATION = "NO_VALID_OBSERVATION"
    CURRENT_NO_PRIOR_OBSERVATION = "CURRENT_NO_PRIOR_OBSERVATION"
    CURRENT = "CURRENT"
    STALE = "STALE"


@dataclass(frozen=True, kw_only=True)
class FREDObservation:
    observation_date: date
    value: Decimal


@dataclass(frozen=True, kw_only=True)
class FREDSeriesResult:
    indicator_name: str
    series_id: str
    status: FREDSeriesStatus
    latest_valid_observation: FREDObservation | None = None
    previous_valid_observation: FREDObservation | None = None


@dataclass(frozen=True, kw_only=True)
class FREDIssue:
    issue_type: str
    message: str
    series_id: str | None = None
    indicator_name: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class FREDCollectionResult:
    status: FREDCollectionStatus
    checked_at: datetime
    series_results: tuple[FREDSeriesResult, ...] = ()
    indicator_snapshots: tuple[ContextIndicatorSnapshot, ...] = ()
    cache_update_results: tuple[ContextStateUpdateResult, ...] = ()
    ledger_write_results: tuple[object, ...] = ()
    issues: tuple[FREDIssue, ...] = ()


class FREDDataClient(Protocol):
    def fetch_observations(
        self,
        series_id: str,
        *,
        file_type: str,
        sort_order: str,
        order_by: str,
        limit: int,
    ) -> list[dict[str, object]]:
        ...


class FREDLedgerWriter(Protocol):
    def write_context_indicator_snapshot(
        self,
        snapshot: ContextIndicatorSnapshot,
        **kwargs: Any,
    ) -> object | None:
        ...


class FREDClient:
    """Credential-safe bounded client for current unpinned FRED observations."""

    def __init__(
        self,
        *,
        api_key_env: str = "FRED_API_KEY",
        timeout_seconds: float = 10.0,
        request_get: Callable[..., Any] = requests.get,
    ) -> None:
        self.api_key_env = _required_string(api_key_env, "api_key_env")
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.request_get = request_get

    def fetch_observations(
        self,
        series_id: str,
        *,
        file_type: str,
        sort_order: str,
        order_by: str,
        limit: int,
    ) -> list[dict[str, object]]:
        series_id = _required_string(series_id, "series_id")
        if file_type != "json" or sort_order != "desc" or order_by != "observation_date":
            raise FREDCollectorError("unsupported FRED observation request policy")
        limit = _bounded_int(limit, "limit", minimum=4, maximum=50)
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise FREDCollectorError("FRED credential is unavailable")
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": file_type,
            "sort_order": sort_order,
            "order_by": order_by,
            "limit": limit,
        }
        try:
            response = self.request_get(
                API_URL,
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            raise FREDCollectorError(
                f"official FRED observation request failed for series {series_id}"
            ) from None
        if response.status_code != 200:
            raise FREDCollectorError(
                f"official FRED observation endpoint returned HTTP {response.status_code} for series {series_id}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise FREDCollectorError(
                f"official FRED observation endpoint returned invalid JSON for series {series_id}"
            ) from None
        observations = payload.get("observations") if isinstance(payload, Mapping) else None
        if not isinstance(observations, list):
            raise FREDCollectorError(
                f"official FRED response has no observations list for series {series_id}"
            )
        return [dict(item) for item in observations if isinstance(item, Mapping)]


@dataclass(frozen=True)
class _Fact:
    indicator_name: str
    value: float | str
    units: str
    value_kind: str
    source_event_time: datetime
    valid_until: datetime
    identity_payload: Mapping[str, object]
    details: Mapping[str, object]


class FREDCollector:
    """Collect one bounded current FRED context snapshot when explicitly invoked."""

    def __init__(
        self,
        *,
        cache: ContextStateCache,
        config: FREDConfig,
        client: FREDDataClient | None = None,
        ledger_writer: FREDLedgerWriter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.cache = cache
        self.config = config
        self.client = client or FREDClient(
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
        )
        self.ledger_writer = ledger_writer
        self.clock = clock

    def collect(
        self,
        *,
        evaluation_time: datetime | None = None,
        write_questdb: bool = False,
        questdb_required: bool = False,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> FREDCollectionResult:
        if write_questdb and questdb_required and self.ledger_writer is None:
            raise FREDCollectorError("QuestDB writes are required but no writer was provided")
        checked_at = ensure_timezone_aware_utc(evaluation_time or self.clock())
        if not self.config.enabled:
            return FREDCollectionResult(
                status=FREDCollectionStatus.DISABLED,
                checked_at=checked_at,
            )

        evaluation_date = checked_at.astimezone(NEW_YORK).date()
        series_results: list[FREDSeriesResult] = []
        issues: list[FREDIssue] = []
        request_failures = 0

        for spec in SERIES_SPECS:
            try:
                records = self.client.fetch_observations(
                    spec.series_id,
                    file_type="json",
                    sort_order="desc",
                    order_by="observation_date",
                    limit=self.config.observation_fetch_limit,
                )
            except Exception as exc:  # noqa: BLE001 - source adapter boundary.
                request_failures += 1
                series_results.append(
                    FREDSeriesResult(
                        indicator_name=spec.indicator_name,
                        series_id=spec.series_id,
                        status=FREDSeriesStatus.REQUEST_FAILED,
                    )
                )
                issues.append(
                    FREDIssue(
                        issue_type="SOURCE_REQUEST_FAILED",
                        message="official FRED source request failed",
                        series_id=spec.series_id,
                        indicator_name=spec.indicator_name,
                        details={"error_type": type(exc).__name__},
                    )
                )
                continue

            latest, previous = _select_valid_observations(records, evaluation_date)
            if latest is None:
                series_results.append(
                    FREDSeriesResult(
                        indicator_name=spec.indicator_name,
                        series_id=spec.series_id,
                        status=FREDSeriesStatus.NO_VALID_OBSERVATION,
                    )
                )
                issues.append(
                    FREDIssue(
                        issue_type="NO_VALID_OBSERVATION",
                        message="bounded FRED response contained no usable latest numeric observation",
                        series_id=spec.series_id,
                        indicator_name=spec.indicator_name,
                    )
                )
                continue

            if not _is_current(
                latest.observation_date,
                evaluation_date,
                self.config.max_observation_age_calendar_days,
            ):
                status = FREDSeriesStatus.STALE
                issues.append(
                    FREDIssue(
                        issue_type="STALE_OBSERVATION",
                        message="latest valid FRED observation is stale",
                        series_id=spec.series_id,
                        indicator_name=spec.indicator_name,
                        details={"observation_date": latest.observation_date.isoformat()},
                    )
                )
            elif previous is None:
                status = FREDSeriesStatus.CURRENT_NO_PRIOR_OBSERVATION
                issues.append(
                    FREDIssue(
                        issue_type="CURRENT_NO_PRIOR_OBSERVATION",
                        message="bounded FRED response contained no previous valid observation",
                        series_id=spec.series_id,
                        indicator_name=spec.indicator_name,
                        details={"observation_date": latest.observation_date.isoformat()},
                    )
                )
            else:
                status = FREDSeriesStatus.CURRENT
            series_results.append(
                FREDSeriesResult(
                    indicator_name=spec.indicator_name,
                    series_id=spec.series_id,
                    status=status,
                    latest_valid_observation=latest,
                    previous_valid_observation=previous,
                )
            )

        if request_failures == len(SERIES_SPECS):
            return _collection_result(
                FREDCollectionStatus.FAILED,
                checked_at,
                series_results,
                issues=issues,
            )
        reachable = [item for item in series_results if item.status is not FREDSeriesStatus.REQUEST_FAILED]
        if request_failures == 0 and reachable and all(
            item.latest_valid_observation is None for item in reachable
        ):
            return _collection_result(
                FREDCollectionStatus.FAILED,
                checked_at,
                series_results,
                issues=issues,
            )
        if request_failures == 0 and len(reachable) == len(SERIES_SPECS) and all(
            item.status is FREDSeriesStatus.STALE for item in reachable
        ):
            return _collection_result(
                FREDCollectionStatus.STALE,
                checked_at,
                series_results,
                issues=issues,
            )

        current_by_name = {
            item.indicator_name: item
            for item in series_results
            if item.status in {
                FREDSeriesStatus.CURRENT,
                FREDSeriesStatus.CURRENT_NO_PRIOR_OBSERVATION,
            }
            and item.latest_valid_observation is not None
        }
        facts: list[_Fact] = []
        for spec in SERIES_SPECS:
            result = current_by_name.get(spec.indicator_name)
            if result is None or result.latest_valid_observation is None:
                continue
            facts.append(
                _raw_fact(
                    spec,
                    result.latest_valid_…701 tokens truncated…ot)
            cache_results.append(update)
            self._write_if_changed(
                snapshot,
                update,
                ledger_results,
                issues,
                write_questdb=write_questdb,
                required=questdb_required,
                run_id=run_id,
                session_id=session_id,
            )

        complete_series = all(item.status is FREDSeriesStatus.CURRENT for item in series_results)
        status = (
            FREDCollectionStatus.SUCCESS
            if complete_series and len(snapshots) == 10 and not issues
            else FREDCollectionStatus.PARTIAL
        )
        return _collection_result(
            status,
            checked_at,
            series_results,
            snapshots,
            cache_results,
            ledger_results,
            issues,
        )

    def _cache_fact(
        self,
        fact: _Fact,
        checked_at: datetime,
    ) -> tuple[ContextIndicatorSnapshot, ContextStateUpdateResult]:
        cache_name = f"fred:{fact.indicator_name}"
        context_indicator_id = _deterministic_id(fact.identity_payload)
        canonical_details = {
            **dict(fact.details),
            "context_indicator_id": context_indicator_id,
            "valid_until": to_utc_iso(fact.valid_until),
        }
        existing = self.cache.get_global(
            cache_name,
            now=checked_at,
            include_expired=True,
        )
        if _same_semantic_fact(existing, fact.value, canonical_details, fact.valid_until):
            assert existing is not None
            details = dict(existing.details)
            updated_at = existing.updated_at
            first_collected_at = _detail_datetime(details, "first_collected_at")
        else:
            first_collected_at = checked_at
            updated_at = fact.source_event_time
            details = {
                **canonical_details,
                "first_collected_at": to_utc_iso(first_collected_at),
            }

        snapshot = ContextIndicatorSnapshot(
            snapshot_time=first_collected_at,
            source=SOURCE_NAME,
            ticker_or_sector=GLOBAL_SCOPE,
            indicator_name=fact.indicator_name,
            value=fact.value,
            context_indicator_id=context_indicator_id,
            window="latest_valid_observation",
            units=fact.units,
            freshness_seconds=None,
            source_event_time=fact.source_event_time,
            details=details,
        )
        update = self.cache.update(
            make_global_context_entry(
                name=cache_name,
                value=fact.value,
                updated_at=updated_at,
                source=SOURCE_NAME,
                source_event_time=fact.source_event_time,
                valid_until=fact.valid_until,
                details=details,
            )
        )
        return snapshot, update

    def _write_if_changed(
        self,
        snapshot: ContextIndicatorSnapshot,
        update: ContextStateUpdateResult,
        ledger_results: list[object],
        issues: list[FREDIssue],
        *,
        write_questdb: bool,
        required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> None:
        if (
            not write_questdb
            or not self.config.writes_questdb_ledger
            or update.status
            not in {ContextStateUpdateStatus.WRITTEN, ContextStateUpdateStatus.REPLACED}
            or self.ledger_writer is None
        ):
            return
        try:
            result = self.ledger_writer.write_context_indicator_snapshot(
                snapshot,
                run_id=run_id,
                session_id=session_id,
            )
            if result is not None:
                ledger_results.append(result)
        except Exception as exc:  # noqa: BLE001 - writer protocol boundary.
            issues.append(
                FREDIssue(
                    issue_type="LEDGER_WRITE_FAILED",
                    message="QuestDB context indicator write failed",
                    indicator_name=snapshot.indicator_name,
                    details={"error_type": type(exc).__name__},
                )
            )
            if required:
                raise FREDCollectorError("QuestDB context indicator write failed") from None


def _select_valid_observations(
    records: Sequence[Mapping[str, object]],
    evaluation_date: date,
) -> tuple[FREDObservation | None, FREDObservation | None]:
    by_date: dict[date, FREDObservation] = {}
    for record in records:
        try:
            observation_date = _parse_date(record.get("date"))
            value = _decimal_value(record.get("value"))
        except FREDCollectorError:
            continue
        if observation_date > evaluation_date or observation_date in by_date:
            continue
        by_date[observation_date] = FREDObservation(
            observation_date=observation_date,
            value=value,
        )
    ordered = sorted(by_date.values(), key=lambda item: item.observation_date, reverse=True)
    latest = ordered[0] if ordered else None
    previous = ordered[1] if len(ordered) > 1 else None
    return latest, previous


def _raw_fact(spec: FREDSeriesSpec, observation: FREDObservation, max_age: int) -> _Fact:
    value = _float_value(observation.value)
    source_event_time = _observation_time(observation.observation_date)
    details = _base_details(
        units="percent",
        value_kind="yield_level",
        observation_date=observation.observation_date,
    )
    details.update({"series_id": spec.series_id, "series_name": spec.series_name})
    return _Fact(
        indicator_name=spec.indicator_name,
        value=value,
        units="percent",
        value_kind="yield_level",
        source_event_time=source_event_time,
        valid_until=_valid_until(observation.observation_date, max_age),
        identity_payload={
            "source": SOURCE_NAME,
            "indicator_name": spec.indicator_name,
            "series_id": spec.series_id,
            "observation_date": observation.observation_date.isoformat(),
            "normalized_value": _canonical_decimal(observation.value),
        },
        details=details,
    )


def _change_fact(
    spec: FREDSeriesSpec,
    current: FREDObservation,
    previous: FREDObservation,
    max_age: int,
) -> _Fact:
    indicator_name = f"{spec.indicator_name}_change_prev_valid_obs"
    change = current.value - previous.value
    details = _base_details(
        units="percentage_points",
        value_kind="change_previous_valid_observation",
        observation_date=current.observation_date,
    )
    details.update(
        {
            "series_id": spec.series_id,
            "series_name": spec.series_name,
            "previous_observation_date": previous.observation_date.isoformat(),
            "observation_interval_calendar_days": (
                current.observation_date - previous.observation_date
            ).days,
            "observation_interval_kind": "previous_valid_observation",
            "derivation_version": CHANGE_DERIVATION_VERSION,
        }
    )
    return _Fact(
        indicator_name=indicator_name,
        value=_float_value(change),
        units="percentage_points",
        value_kind="change_previous_valid_observation",
        source_event_time=_observation_time(current.observation_date),
        valid_until=_valid_until(current.observation_date, max_age),
        identity_payload={
            "source": SOURCE_NAME,
            "indicator_name": indicator_name,
            "derivation_version": CHANGE_DERIVATION_VERSION,
            "series_id": spec.series_id,
            "current_observation_date": current.observation_date.isoformat(),
            "prior_observation_date": previous.observation_date.isoformat(),
            "normalized_value": _canonical_decimal(change),
        },
        details=details,
    )


def _spread_fact(
    indicator_name: str,
    left: FREDSeriesResult,
    right: FREDSeriesResult,
    max_age: int,
) -> _Fact:
    assert left.latest_valid_observation is not None
    assert right.latest_valid_observation is not None
    observation_date = left.latest_valid_observation.observation_date
    value = left.latest_valid_observation.value - right.latest_valid_observation.value
    component_names = (left.indicator_name, right.indicator_name)
    component_specs = tuple(_SPEC_BY_NAME[name] for name in component_names)
    component_ids = [spec.series_id for spec in component_specs]
    component_dates = {name: observation_date.isoformat() for name in component_names}
    details = _base_details(
        units="percentage_points",
        value_kind="yield_spread",
        observation_date=observation_date,
    )
    details.update(
        {
            "component_series_ids": component_ids,
            "component_series_names": {
                spec.series_id: spec.series_name for spec in component_specs
            },
            "component_observation_dates": component_dates,
            "component_values": {
                left.indicator_name: _float_value(left.latest_valid_observation.value),
                right.indicator_name: _float_value(right.latest_valid_observation.value),
            },
            "derivation_version": SPREAD_DERIVATION_VERSION,
        }
    )
    return _Fact(
        indicator_name=indicator_name,
        value=_float_value(value),
        units="percentage_points",
        value_kind="yield_spread",
        source_event_time=_observation_time(observation_date),
        valid_until=_valid_until(observation_date, max_age),
        identity_payload={
            "source": SOURCE_NAME,
            "indicator_name": indicator_name,
            "derivation_version": SPREAD_DERIVATION_VERSION,
            "component_series_ids": component_ids,
            "component_observation_dates": component_dates,
            "normalized_value": _canonical_decimal(value),
        },
        details=details,
    )


def _regime_fact(results: Sequence[FREDSeriesResult], max_age: int) -> _Fact:
    by_name = {item.indicator_name: item for item in results}
    three_month = by_name["us_treasury_3m_yield"].latest_valid_observation
    two_year = by_name["us_treasury_2y_yield"].latest_valid_observation
    ten_year = by_name["us_treasury_10y_yield"].latest_valid_observation
    assert three_month is not None and two_year is not None and ten_year is not None
    observation_date = three_month.observation_date
    front = two_year.value - three_month.value
    long = ten_year.value - two_year.value
    value = (
        ("FRONT_INVERTED" if front < 0 else "FRONT_POSITIVE")
        + "__"
        + ("LONG_INVERTED" if long < 0 else "LONG_POSITIVE")
    )
    component_dates = {
        item.indicator_name: item.latest_valid_observation.observation_date.isoformat()  # type: ignore[union-attr]
        for item in results
    }
    component_ids = [_SPEC_BY_NAME[item.indicator_name].series_id for item in results]
    details = _base_details(
        units="category",
        value_kind="categorical_regime",
        observation_date=observation_date,
    )
    details.update(
        {
            "component_series_ids": component_ids,
            "component_series_names": {
                spec.series_id: spec.series_name for spec in SERIES_SPECS
            },
            "component_observation_dates": component_dates,
            "component_values": {
                item.indicator_name: _float_value(item.latest_valid_observation.value)  # type: ignore[union-attr]
                for item in results
            },
            "front_spread_percentage_points": _float_value(front),
            "long_spread_percentage_points": _float_value(long),
            "derivation_version": REGIME_DERIVATION_VERSION,
        }
    )
    return _Fact(
        indicator_name="rate_curve_regime_v1",
        value=value,
        units="category",
        value_kind="categorical_regime",
        source_event_time=_observation_time(observation_date),
        valid_until=_valid_until(observation_date, max_age),
        identity_payload={
            "source": SOURCE_NAME,
            "indicator_name": "rate_curve_regime_v1",
            "derivation_version": REGIME_DERIVATION_VERSION,
            "component_series_ids": component_ids,
            "component_observation_dates": component_dates,
            "regime_value": value,
        },
        details=details,
    )


def _base_details(*, units: str, value_kind: str, observation_date: date) -> dict[str, object]:
    return {
        "source": SOURCE_NAME,
        "observation_date": observation_date.isoformat(),
        "units": units,
        "value_kind": value_kind,
        "source_event_time_basis": SOURCE_EVENT_TIME_BASIS,
        "availability_basis": AVAILABILITY_BASIS,
        "research_asof_eligible": False,
        "vintage_tracking_mode": VINTAGE_TRACKING_MODE,
    }


def _date_misaligned_issue(
    indicator_name: str,
    results: Sequence[FREDSeriesResult],
) -> FREDIssue:
    return FREDIssue(
        issue_type="DATE_MISALIGNED",
        message="current FRED component observation dates are not aligned",
        indicator_name=indicator_name,
        details={
            "component_observation_dates": {
                item.indicator_name: item.latest_valid_observation.observation_date.isoformat()  # type: ignore[union-attr]
                for item in results
            }
        },
    )


def _same_semantic_fact(
    existing: ContextStateEntry | None,
    value: float | str,
    canonical_details: Mapping[str, object],
    valid_until: datetime,
) -> bool:
    if existing is None or existing.value != value or existing.valid_until != valid_until:
        return False
    existing_details = dict(existing.details)
    existing_details.pop("first_collected_at", None)
    return existing_details == dict(canonical_details)


def _detail_datetime(details: Mapping[str, object], name: str) -> datetime:
    value = details.get(name)
    if not isinstance(value, str):
        raise FREDCollectorError(f"cached {name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ensure_timezone_aware_utc(parsed)
    except (TypeError, ValueError):
        raise FREDCollectorError(f"cached {name} must be timezone-aware") from None


def _observation_time(observation_date: date) -> datetime:
    return datetime.combine(observation_date, time.min, UTC)


def _valid_until(observation_date: date, max_age: int) -> datetime:
    return datetime.combine(
        observation_date + timedelta(days=max_age),
        time.max,
        NEW_YORK,
    ).astimezone(UTC)


def _is_current(observation_date: date, evaluation_date: date, max_age: int) -> bool:
    return observation_date <= evaluation_date <= observation_date + timedelta(days=max_age)


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise FREDCollectorError("observation date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise FREDCollectorError("observation date must be YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise FREDCollectorError("observation date must be YYYY-MM-DD")
    return parsed


def _decimal_value(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise FREDCollectorError("FRED observation value must be numeric")
    text = str(value).strip()
    if not text or text == ".":
        raise FREDCollectorError("FRED observation value must be numeric")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise FREDCollectorError("FRED observation value must be numeric") from None
    if not number.is_finite():
        raise FREDCollectorError("FRED observation value must be finite")
    return number


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _float_value(value: Decimal) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise FREDCollectorError("normalized FRED value must be finite")
    return result


def _deterministic_id(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"context_indicator_{digest}"


def _collection_result(
    status: FREDCollectionStatus,
    checked_at: datetime,
    series_results: Sequence[FREDSeriesResult],
    snapshots: Sequence[ContextIndicatorSnapshot] = (),
    cache_results: Sequence[ContextStateUpdateResult] = (),
    ledger_results: Sequence[object] = (),
    issues: Sequence[FREDIssue] = (),
) -> FREDCollectionResult:
    return FREDCollectionResult(
        status=status,
        checked_at=checked_at,
        series_results=tuple(series_results),
        indicator_snapshots=tuple(snapshots),
        cache_update_results=tuple(cache_results),
        ledger_write_results=tuple(ledger_results),
        issues=tuple(issues),
    )


def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise FREDCollectorError(f"{key} must be a mapping")
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FREDCollectorError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise FREDCollectorError(f"{field_name} must be positive")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FREDCollectorError(f"{field_name} must be positive") from None
    if not math.isfinite(number) or number <= 0:
        raise FREDCollectorError(f"{field_name} must be positive")
    return number


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FREDCollectorError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FREDCollectorError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise FREDCollectorError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


__all__ = [
    "API_URL",
    "EXPECTED_SERIES_IDS",
    "FREDClient",
    "FREDCollectionResult",
    "FREDCollectionStatus",
    "FREDCollector",
    "FREDCollectorError",
    "FREDConfig",
    "FREDDataClient",
    "FREDIssue",
    "FREDLedgerWriter",
    "FREDObservation",
    "FREDSeriesResult",
    "FREDSeriesStatus",
    "SERIES_SPECS",
    "SOURCE_NAME",
]
