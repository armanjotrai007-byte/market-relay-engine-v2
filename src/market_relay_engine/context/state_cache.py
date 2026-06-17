"""Bounded in-memory cache for latest structured context state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
import json
import math
from threading import RLock
from typing import Any

from market_relay_engine.common.serialization import to_json_dict
from market_relay_engine.common.time import ensure_timezone_aware_utc, to_utc_iso, utc_now
from market_relay_engine.contracts.context import ContextStateSnapshot

ContextStateValue = str | int | float | bool


class ContextStateCacheError(ValueError):
    """Raised for invalid local context cache inputs."""


class ContextScope(str, Enum):
    """Supported context state scopes."""

    GLOBAL = "GLOBAL"
    TICKER = "TICKER"
    SECTOR = "SECTOR"


class ContextStateUpdateStatus(str, Enum):
    """Result status for a context state update attempt."""

    WRITTEN = "WRITTEN"
    REPLACED = "REPLACED"
    IGNORED_STALE = "IGNORED_STALE"
    IGNORED_DUPLICATE = "IGNORED_DUPLICATE"


_SEVERITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
_SEVERITY_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_RISK_LEVEL_BY_SEVERITY = {
    "LOW": "LOW",
    "MEDIUM": "ELEVATED",
    "HIGH": "HIGH",
    "CRITICAL": "HIGH",
}


@dataclass(frozen=True, kw_only=True)
class ContextStateKey:
    """Identity for one latest context state fact."""

    scope: ContextScope
    name: str
    ticker: str | None = None
    sector: str | None = None

    def __post_init__(self) -> None:
        scope = _coerce_scope(self.scope)
        name = _required_string(self.name, "name")
        ticker = self.ticker
        sector = self.sector
        if scope is ContextScope.GLOBAL:
            if ticker is not None or sector is not None:
                raise ContextStateCacheError("GLOBAL context keys cannot include ticker or sector")
        elif scope is ContextScope.TICKER:
            ticker = _normalize_symbol(ticker, "ticker")
            if sector is not None:
                raise ContextStateCacheError("TICKER context keys cannot include sector")
        elif scope is ContextScope.SECTOR:
            sector = _normalize_symbol(sector, "sector")
            if ticker is not None:
                raise ContextStateCacheError("SECTOR context keys cannot include ticker")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "sector", sector)


@dataclass(frozen=True, kw_only=True)
class ContextStateEntry:
    """Latest structured context state value for one key."""

    key: ContextStateKey
    value: ContextStateValue
    updated_at: datetime
    severity: str = "INFO"
    source: str = "manual"
    source_event_time: datetime | None = None
    valid_until: datetime | None = None
    confidence: float | None = None
    details: dict[str, object] = field(default_factory=dict)
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ContextStateKey):
            raise ContextStateCacheError("key must be a ContextStateKey")
        object.__setattr__(self, "value", _normalize_value(self.value))
        severity = _required_string(self.severity, "severity").upper()
        if severity not in _SEVERITIES:
            raise ContextStateCacheError("severity must be one of CRITICAL, HIGH, INFO, LOW, MEDIUM")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "source", _required_string(self.source, "source"))
        object.__setattr__(self, "updated_at", _normalize_datetime(self.updated_at, "updated_at"))
        object.__setattr__(self, "source_event_time", _normalize_optional_datetime(self.source_event_time, "source_event_time"))
        object.__setattr__(self, "valid_until", _normalize_optional_datetime(self.valid_until, "valid_until"))
        object.__setattr__(self, "confidence", _normalize_confidence(self.confidence))
        object.__setattr__(self, "details", _json_safe_details_copy(self.details))
        object.__setattr__(self, "trace_id", _optional_string(self.trace_id, "trace_id"))


@dataclass(frozen=True, kw_only=True)
class ContextStateUpdateResult:
    """Outcome from writing one entry into the cache."""

    status: ContextStateUpdateStatus
    key: ContextStateKey
    evicted_count: int = 0
    message: str | None = None


class ContextStateCache:
    """Mutable bounded cache of latest context state entries."""

    def __init__(self, *, max_entries: int = 10000) -> None:
        self.max_entries = _positive_int(max_entries, "max_entries")
        self._entries: dict[ContextStateKey, ContextStateEntry] = {}
        self._lock = RLock()

    def update(self, entry: ContextStateEntry) -> ContextStateUpdateResult:
        """Write an entry if it is not stale or duplicate."""
        if not isinstance(entry, ContextStateEntry):
            raise ContextStateCacheError("entry must be a ContextStateEntry")
        stored_entry = _entry_copy(entry)
        with self._lock:
            existing = self._entries.get(stored_entry.key)
            message: str | None = None
            evicted_count = 0
            if existing is None:
                self._entries[stored_entry.key] = stored_entry
                status = ContextStateUpdateStatus.WRITTEN
            elif stored_entry.updated_at < existing.updated_at:
                status = ContextStateUpdateStatus.IGNORED_STALE
                message = "ignored stale context state update"
            elif stored_entry.updated_at == existing.updated_at and _same_content(existing, stored_entry):
                status = ContextStateUpdateStatus.IGNORED_DUPLICATE
                message = "ignored duplicate context state update"
            else:
                self._entries[stored_entry.key] = stored_entry
                status = ContextStateUpdateStatus.REPLACED
            if status in {ContextStateUpdateStatus.WRITTEN, ContextStateUpdateStatus.REPLACED}:
                evicted_count = self._evict_over_limit_locked()
            return ContextStateUpdateResult(status=status, key=stored_entry.key, evicted_count=evicted_count, message=message)

    def get(self, key: ContextStateKey, *, now: datetime | None = None, include_expired: bool = False) -> ContextStateEntry | None:
        key = _coerce_key(key)
        timestamp = _normalize_now(now)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or (not include_expired and _is_expired(entry, timestamp)):
                return None
            return _entry_copy(entry)

    def get_global(self, name: str, *, now: datetime | None = None, include_expired: bool = False) -> ContextStateEntry | None:
        return self.get(ContextStateKey(scope=ContextScope.GLOBAL, name=name), now=now, include_expired=include_expired)

    def get_ticker(self, ticker: str, name: str, *, now: datetime | None = None, include_expired: bool = False) -> ContextStateEntry | None:
        return self.get(ContextStateKey(scope=ContextScope.TICKER, ticker=ticker, name=name), now=now, include_expired=include_expired)

    def get_sector(self, sector: str, name: str, *, now: datetime | None = None, include_expired: bool = False) -> ContextStateEntry | None:
        return self.get(ContextStateKey(scope=ContextScope.SECTOR, sector=sector, name=name), now=now, include_expired=include_expired)

    def latest_for_ticker(self, ticker: str, *, now: datetime | None = None, include_expired: bool = False) -> list[ContextStateEntry]:
        ticker = _normalize_symbol(ticker, "ticker")
        return self._latest(lambda entry: entry.key.scope is ContextScope.TICKER and entry.key.ticker == ticker, now=now, include_expired=include_expired)

    def latest_for_sector(self, sector: str, *, now: datetime | None = None, include_expired: bool = False) -> list[ContextStateEntry]:
        sector = _normalize_symbol(sector, "sector")
        return self._latest(lambda entry: entry.key.scope is ContextScope.SECTOR and entry.key.sector == sector, now=now, include_expired=include_expired)

    def latest_global(self, *, now: datetime | None = None, include_expired: bool = False) -> list[ContextStateEntry]:
        return self._latest(lambda entry: entry.key.scope is ContextScope.GLOBAL, now=now, include_expired=include_expired)

    def snapshot(self, *, now: datetime | None = None, include_expired: bool = False) -> dict[str, object]:
        timestamp = _normalize_now(now)
        with self._lock:
            result = _empty_snapshot_dict()
            entries = sorted(self._visible_entries_locked(timestamp, include_expired), key=_entry_sort_key)
            for entry in entries:
                _add_entry_dict(result, _entry_to_dict(entry, timestamp))
            result["entry_count"] = len(entries)
            return _json_safe_object_copy(result)

    def to_context_state_snapshot(
        self,
        *,
        ticker: str,
        sector: str | None = None,
        now: datetime | None = None,
        include_global: bool = True,
        include_sector: bool = True,
        include_ticker: bool = True,
        trace_id: str | None = None,
    ) -> ContextStateSnapshot:
        ticker = _normalize_symbol(ticker, "ticker")
        sector = None if sector is None else _normalize_symbol(sector, "sector")
        timestamp = _normalize_now(now)
        trace_id = _optional_string(trace_id, "trace_id")
        with self._lock:
            fresh: list[ContextStateEntry] = []
            expired: list[ContextStateEntry] = []
            for entry in self._entries.values():
                key = entry.key
                relevant = (
                    (include_global and key.scope is ContextScope.GLOBAL)
                    or (include_ticker and key.scope is ContextScope.TICKER and key.ticker == ticker)
                    or (include_sector and sector is not None and key.scope is ContextScope.SECTOR and key.sector == sector)
                )
                if not relevant:
                    continue
                (expired if _is_expired(entry, timestamp) else fresh).append(entry)
            fresh = sorted(fresh, key=_entry_sort_key)
            expired = sorted(expired, key=_entry_sort_key)
            summary = _empty_snapshot_dict()
            for entry in fresh:
                _add_entry_dict(summary, _entry_to_dict(entry, timestamp))
            summary["entry_count"] = len(fresh)
            highest = _highest_severity(fresh)
            risk_level = _risk_level_for_severity(highest)
            valid_until = _earliest_valid_until(fresh)
            if expired:
                summary["fresh_entry_count"] = len(fresh)
                summary["expired_entry_count"] = len(expired)
                summary["expired_context_present"] = True
                summary["stale_context_policy"] = "ELEVATED"
                summary["expired_entries"] = [_entry_to_dict(entry, timestamp) for entry in expired]
                if risk_level in {None, "LOW"}:
                    highest = "EXPIRED"
                    risk_level = "ELEVATED"
            return ContextStateSnapshot(
                snapshot_time=timestamp,
                ticker=ticker,
                sector=sector,
                context_summary=_json_safe_object_copy(summary),
                highest_severity=highest,
                risk_level=risk_level,
                valid_until=valid_until,
                trace_id=trace_id,
            )

    def purge_expired(self, *, now: datetime | None = None) -> int:
        timestamp = _normalize_now(now)
        with self._lock:
            expired_keys = [key for key, entry in self._entries.items() if _is_expired(entry, timestamp)]
            for key in expired_keys:
                del self._entries[key]
            return len(expired_keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _latest(self, predicate: Any, *, now: datetime | None, include_expired: bool) -> list[ContextStateEntry]:
        timestamp = _normalize_now(now)
        with self._lock:
            entries = [entry for entry in self._visible_entries_locked(timestamp, include_expired) if predicate(entry)]
            return [_entry_copy(entry) for entry in sorted(entries, key=_entry_sort_key)]

    def _visible_entries_locked(self, now: datetime, include_expired: bool) -> list[ContextStateEntry]:
        return list(self._entries.values()) if include_expired else [entry for entry in self._entries.values() if not _is_expired(entry, now)]

    def _evict_over_limit_locked(self) -> int:
        evicted_count = 0
        while len(self._entries) > self.max_entries:
            key, _entry = min(self._entries.items(), key=lambda item: (item[1].updated_at, _key_sort_key(item[0])))
            del self._entries[key]
            evicted_count += 1
        return evicted_count


def make_global_context_entry(*, name: str, value: ContextStateValue, updated_at: datetime, severity: str = "INFO", source: str = "manual", source_event_time: datetime | None = None, valid_until: datetime | None = None, confidence: float | None = None, details: dict[str, object] | None = None, trace_id: str | None = None) -> ContextStateEntry:
    return ContextStateEntry(key=ContextStateKey(scope=ContextScope.GLOBAL, name=name), value=value, updated_at=updated_at, severity=severity, source=source, source_event_time=source_event_time, valid_until=valid_until, confidence=confidence, details={} if details is None else details, trace_id=trace_id)


def make_ticker_context_entry(*, ticker: str, name: str, value: ContextStateValue, updated_at: datetime, severity: str = "INFO", source: str = "manual", source_event_time: datetime | None = None, valid_until: datetime | None = None, confidence: float | None = None, details: dict[str, object] | None = None, trace_id: str | None = None) -> ContextStateEntry:
    return ContextStateEntry(key=ContextStateKey(scope=ContextScope.TICKER, ticker=ticker, name=name), value=value, updated_at=updated_at, severity=severity, source=source, source_event_time=source_event_time, valid_until=valid_until, confidence=confidence, details={} if details is None else details, trace_id=trace_id)


def make_sector_context_entry(*, sector: str, name: str, value: ContextStateValue, updated_at: datetime, severity: str = "INFO", source: str = "manual", source_event_time: datetime | None = None, valid_until: datetime | None = None, confidence: float | None = None, details: dict[str, object] | None = None, trace_id: str | None = None) -> ContextStateEntry:
    return ContextStateEntry(key=ContextStateKey(scope=ContextScope.SECTOR, sector=sector, name=name), value=value, updated_at=updated_at, severity=severity, source=source, source_event_time=source_event_time, valid_until=valid_until, confidence=confidence, details={} if details is None else details, trace_id=trace_id)


def _normalize_value(value: object) -> ContextStateValue:
    if isinstance(value, str):
        return _required_string(value, "value")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContextStateCacheError("value must be finite")
        return value
    raise ContextStateCacheError("value must be a JSON-safe scalar: str, int, float, or bool")


def _coerce_scope(value: object) -> ContextScope:
    try:
        return ContextScope(value)
    except ValueError as exc:
        raise ContextStateCacheError("scope must be GLOBAL, TICKER, or SECTOR") from exc


def _coerce_key(value: object) -> ContextStateKey:
    if not isinstance(value, ContextStateKey):
        raise ContextStateCacheError("key must be a ContextStateKey")
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextStateCacheError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: str | None, field_name: str) -> str | None:
    return None if value is None else _required_string(value, field_name)


def _normalize_symbol(value: object, field_name: str) -> str:
    return _required_string(value, field_name).upper()


def _normalize_datetime(value: object, field_name: str) -> datetime:
    try:
        return ensure_timezone_aware_utc(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ContextStateCacheError(f"{field_name} must be a timezone-aware datetime") from exc


def _normalize_optional_datetime(value: datetime | None, field_name: str) -> datetime | None:
    return None if value is None else _normalize_datetime(value, field_name)


def _normalize_now(value: datetime | None) -> datetime:
    return utc_now() if value is None else _normalize_datetime(value, "now")


def _normalize_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ContextStateCacheError("confidence must be numeric")
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ContextStateCacheError("confidence must be numeric") from exc
    if not math.isfinite(confidence):
        raise ContextStateCacheError("confidence must be finite")
    if confidence < 0.0 or confidence > 1.0:
        raise ContextStateCacheError("confidence must be between 0.0 and 1.0")
    return confidence


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextStateCacheError(f"{field_name} must be a positive int")
    return value


def _json_safe_object_copy(value: Any) -> Any:
    try:
        safe_value = to_json_dict(value)
        encoded = json.dumps(safe_value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ContextStateCacheError("value must be JSON-safe") from exc


def _json_safe_details_copy(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContextStateCacheError("details must be a dictionary")
    copied = _json_safe_object_copy(value)
    if not isinstance(copied, dict):
        raise ContextStateCacheError("details must be a dictionary")
    return copied


def _is_expired(entry: ContextStateEntry, now: datetime) -> bool:
    return entry.valid_until is not None and now > entry.valid_until


def _same_content(left: ContextStateEntry, right: ContextStateEntry) -> bool:
    return (left.value == right.value and left.severity == right.severity and left.source == right.source and left.source_event_time == right.source_event_time and left.valid_until == right.valid_until and left.confidence == right.confidence and left.details == right.details)


def _key_sort_key(key: ContextStateKey) -> tuple[str, str, str, str]:
    return (key.scope.value, key.ticker or "", key.sector or "", key.name)


def _entry_sort_key(entry: ContextStateEntry) -> tuple[str, str, str, str]:
    return _key_sort_key(entry.key)


def _entry_copy(entry: ContextStateEntry) -> ContextStateEntry:
    return replace(entry, details=_json_safe_details_copy(entry.details))


def _entry_to_dict(entry: ContextStateEntry, now: datetime) -> dict[str, object]:
    return {
        "scope": entry.key.scope.value,
        "ticker": entry.key.ticker,
        "sector": entry.key.sector,
        "name": entry.key.name,
        "value": entry.value,
        "severity": entry.severity,
        "source": entry.source,
        "updated_at": to_utc_iso(entry.updated_at),
        "source_event_time": to_utc_iso(entry.source_event_time) if entry.source_event_time is not None else None,
        "valid_until": to_utc_iso(entry.valid_until) if entry.valid_until is not None else None,
        "confidence": entry.confidence,
        "details": _json_safe_details_copy(entry.details),
        "trace_id": entry.trace_id,
        "expired": _is_expired(entry, now),
    }


def _empty_snapshot_dict() -> dict[str, object]:
    return {"global": {}, "tickers": {}, "sectors": {}, "entry_count": 0}


def _add_entry_dict(snapshot: dict[str, object], entry_dict: dict[str, object]) -> None:
    scope = entry_dict["scope"]
    name = entry_dict["name"]
    if scope == ContextScope.GLOBAL.value:
        snapshot["global"][name] = entry_dict  # type: ignore[index]
    elif scope == ContextScope.TICKER.value:
        snapshot["tickers"].setdefault(entry_dict["ticker"], {})[name] = entry_dict  # type: ignore[union-attr,index]
    elif scope == ContextScope.SECTOR.value:
        snapshot["sectors"].setdefault(entry_dict["sector"], {})[name] = entry_dict  # type: ignore[union-attr,index]


def _highest_severity(entries: list[ContextStateEntry]) -> str | None:
    return None if not entries else max((entry.severity for entry in entries), key=lambda severity: _SEVERITY_RANK[severity])


def _risk_level_for_severity(severity: str | None) -> str | None:
    return None if severity is None else _RISK_LEVEL_BY_SEVERITY.get(severity)


def _earliest_valid_until(entries: list[ContextStateEntry]) -> datetime | None:
    values = [entry.valid_until for entry in entries if entry.valid_until is not None]
    return min(values) if values else None


__all__ = [
    "ContextScope",
    "ContextStateCache",
    "ContextStateCacheError",
    "ContextStateEntry",
    "ContextStateKey",
    "ContextStateUpdateResult",
    "ContextStateUpdateStatus",
    "ContextStateValue",
    "make_global_context_entry",
    "make_sector_context_entry",
    "make_ticker_context_entry",
]
