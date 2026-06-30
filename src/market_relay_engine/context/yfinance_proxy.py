"""Development-only yfinance proxy context collector."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import json
import math
from typing import Any, Protocol

import pandas as pd

from market_relay_engine.common.time import ensure_timezone_aware_utc, to_utc_iso, utc_now
from market_relay_engine.context.provenance import attach_provenance
from market_relay_engine.context.state_cache import (
    ContextScope,
    ContextStateCache,
    ContextStateUpdateResult,
    ContextStateUpdateStatus,
    make_global_context_entry,
    make_sector_context_entry,
)
from market_relay_engine.contracts.context import ContextIndicatorSnapshot

SOURCE_NAME = "yfinance_dev_raw_v1"
SUPPORTED_INTERVAL = "5m"
INTERVAL_SECONDS = 300
DIGEST_PREFIX_HEX_LENGTH = 32
_PRICE_COLUMNS = frozenset({"Open", "High", "Low", "Close", "Adj Close", "Volume"})
_DEFAULT_COLLECTION_SYMBOLS = ("SPY", "QQQ", "IWM", "GLD", "^VIX", "XLE", "XOP", "OIH", "XLI", "PPA", "ITA")
_DEFAULT_REGISTRY = {
    "SPY": (ContextScope.GLOBAL, None),
    "QQQ": (ContextScope.GLOBAL, None),
    "IWM": (ContextScope.GLOBAL, None),
    "GLD": (ContextScope.GLOBAL, None),
    "^VIX": (ContextScope.GLOBAL, None),
    "XLE": (ContextScope.SECTOR, "OIL"),
    "XOP": (ContextScope.SECTOR, "OIL"),
    "OIH": (ContextScope.SECTOR, "OIL"),
    "XLI": (ContextScope.SECTOR, "INDUSTRIALS"),
    "PPA": (ContextScope.SECTOR, "DEFENSE"),
    "ITA": (ContextScope.SECTOR, "DEFENSE"),
}

DownloadFunction = Callable[..., Any]
ClockFunction = Callable[[], datetime]


class YFinanceProxyError(RuntimeError):
    """Raised when the yfinance development collector cannot proceed safely."""


class YFinanceProxyCollectionStatus(str, Enum):
    DISABLED = "DISABLED"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NO_FRESH_DATA = "NO_FRESH_DATA"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class YFinanceProxyIssue:
    issue_type: str
    message: str
    symbol: str | None = None
    window: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ProxyIndicatorReading:
    symbol: str
    sector: str | None
    indicator_name: str
    window: str
    value: float
    source_event_time: datetime
    valid_until: datetime | None
    context_indicator_id: str


@dataclass(frozen=True, kw_only=True)
class ProxySymbolRegistration:
    symbol: str
    scope: ContextScope
    sector: str | None = None

    def __post_init__(self) -> None:
        symbol = _normalize_symbol(self.symbol)
        scope = ContextScope(self.scope)
        sector = None if self.sector is None else _normalize_symbol(self.sector)
        if scope is ContextScope.GLOBAL and sector is not None:
            raise YFinanceProxyError("GLOBAL proxy registrations cannot include sector")
        if scope is ContextScope.SECTOR and sector is None:
            raise YFinanceProxyError("SECTOR proxy registrations require sector")
        if scope is ContextScope.TICKER:
            raise YFinanceProxyError("PR25 does not create ticker-scoped yfinance proxy entries")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "sector", sector)


class ContextIndicatorWriter(Protocol):
    def write_context_indicator_snapshot(
        self,
        snapshot: ContextIndicatorSnapshot,
        **kwargs: Any,
    ) -> object | None:
        ...


@dataclass(frozen=True, kw_only=True)
class YFinanceProxyConfig:
    enabled: bool = False
    development_only: bool = True
    production_critical: bool = False
    feeds_memory_cache: bool = True
    writes_questdb_ledger: bool = True
    used_in_per_tick_loop: bool = False
    required: bool = False
    period: str = "5d"
    interval: str = SUPPORTED_INTERVAL
    timeout_seconds: float = 10.0
    bar_completion_grace_seconds: int = 30
    max_staleness_seconds: int = 360
    auto_adjust: bool = False
    actions: bool = False
    repair: bool = False
    keepna: bool = True
    prepost: bool = False
    threads: bool = True
    purpose: str | None = None
    requested_symbols: tuple[str, ...] = _DEFAULT_COLLECTION_SYMBOLS
    registry: tuple[ProxySymbolRegistration, ...] = field(default_factory=lambda: _default_registry_tuple())

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise YFinanceProxyError("enabled must be bool")
        if self.development_only is not True:
            raise YFinanceProxyError("yfinance_dev_only must be development_only")
        if self.production_critical is not False:
            raise YFinanceProxyError("yfinance_dev_only must not be production critical")
        if self.feeds_memory_cache is not True:
            raise YFinanceProxyError("yfinance_dev_only must feed the memory cache")
        if self.used_in_per_tick_loop is not False:
            raise YFinanceProxyError("yfinance_dev_only must not run in the per-tick loop")
        if self.interval != SUPPORTED_INTERVAL:
            raise YFinanceProxyError("PR25 supports only interval='5m'")
        timeout = _positive_float(self.timeout_seconds, "timeout_seconds")
        grace = _non_negative_int(self.bar_completion_grace_seconds, "bar_completion_grace_seconds")
        staleness = _positive_int(self.max_staleness_seconds, "max_staleness_seconds")
        if staleness < INTERVAL_SECONDS + grace:
            raise YFinanceProxyError("max_staleness_seconds must be at least 300 + bar_completion_grace_seconds")
        symbols = tuple(_normalize_symbol(symbol) for symbol in self.requested_symbols)
        if not symbols:
            raise YFinanceProxyError("requested_symbols must not be empty")
        registry = tuple(self.registry)
        registry_map = {registration.symbol: registration for registration in registry}
        missing = [symbol for symbol in symbols if symbol not in registry_map]
        if missing:
            raise YFinanceProxyError(f"missing proxy registry mappings: {missing}")
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "bar_completion_grace_seconds", grace)
        object.__setattr__(self, "max_staleness_seconds", staleness)
        object.__setattr__(self, "requested_symbols", symbols)
        object.__setattr__(self, "registry", registry)

    @classmethod
    def from_repository_configs(
        cls,
        context_sources: Mapping[str, Any],
        symbols: Mapping[str, Any],
        **overrides: Any,
    ) -> "YFinanceProxyConfig":
        source = context_sources.get("structured_sources", {}).get("yfinance_dev_only", {})
        if not isinstance(source, Mapping):
            raise YFinanceProxyError("structured_sources.yfinance_dev_only must be a mapping")
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in source.items() if key in allowed}
        registry = build_proxy_registry(symbols)
        values["registry"] = tuple(registry.values())
        values["requested_symbols"] = _DEFAULT_COLLECTION_SYMBOLS
        values.update(overrides)
        return cls(**values)


@dataclass(frozen=True, kw_only=True)
class YFinanceProxyCollectionResult:
    status: YFinanceProxyCollectionStatus
    started_at: datetime
    completed_at: datetime
    requested_symbols: tuple[str, ...]
    successful_symbols: tuple[str, ...]
    failed_symbols: tuple[str, ...]
    stale_symbols: tuple[str, ...]
    issues: tuple[YFinanceProxyIssue, ...]
    indicator_snapshots: tuple[ContextIndicatorSnapshot, ...]
    cache_update_results: tuple[ContextStateUpdateResult, ...]
    ledger_write_results: tuple[object, ...]


@dataclass(frozen=True)
class _PreparedFrame:
    frame: pd.DataFrame
    invalid_close_timestamps: frozenset[pd.Timestamp]


@dataclass(frozen=True)
class _IndicatorBuild:
    snapshot: ContextIndicatorSnapshot
    bar_start: datetime
    bar_end: datetime
    details: dict[str, object]


class YFinanceProxyCollector:
    def __init__(
        self,
        *,
        cache: ContextStateCache,
        config: YFinanceProxyConfig,
        download: DownloadFunction | None = None,
        clock: ClockFunction = utc_now,
        ledger_writer: ContextIndicatorWriter | None = None,
    ) -> None:
        self.cache = cache
        self.config = config
        self.download = download or _download_yfinance
        self.clock = clock
        self.ledger_writer = ledger_writer
        self.registry = {registration.symbol: registration for registration in config.registry}

    def collect(
        self,
        *,
        evaluation_time: datetime | None = None,
        write_questdb: bool = False,
        questdb_required: bool = False,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> YFinanceProxyCollectionResult:
        if write_questdb and questdb_required and self.ledger_writer is None:
            raise YFinanceProxyError("QuestDB writes are required but no ledger writer was provided")
        explicit_time = None if evaluation_time is None else ensure_timezone_aware_utc(evaluation_time)
        started_at = explicit_time or ensure_timezone_aware_utc(self.clock())
        if not self.config.enabled:
            return YFinanceProxyCollectionResult(
                status=YFinanceProxyCollectionStatus.DISABLED,
                started_at=started_at,
                completed_at=started_at,
                requested_symbols=tuple(self.config.requested_symbols),
                successful_symbols=(),
                failed_symbols=(),
                stale_symbols=(),
                issues=(),
                indicator_snapshots=(),
                cache_update_results=(),
                ledger_write_results=(),
            )

        issues: list[YFinanceProxyIssue] = []
        normalized: dict[str, pd.DataFrame] = {}
        try:
            batch = self._download(tuple(self.config.requested_symbols))
        except Exception as exc:  # noqa: BLE001 - download adapter boundary only.
            return self._failed_result(started_at, (YFinanceProxyIssue(issue_type="DOWNLOAD_FAILED", message=str(exc)),), completed_at=explicit_time)

        batch_symbols, fallback_symbols = self._normalize_batch(batch, tuple(self.config.requested_symbols), issues)
        normalized.update(batch_symbols)
        for symbol in fallback_symbols:
            try:
                individual = self._download((symbol,))
                symbol_frames, retry_missing = self._normalize_batch(individual, (symbol,), issues, individual=True)
                normalized.update(symbol_frames)
                if retry_missing:
                    issues.append(_issue("SYMBOL_MISSING", symbol, "symbol absent after individual fallback"))
            except Exception as exc:  # noqa: BLE001 - download adapter boundary only.
                issues.append(_issue("DOWNLOAD_FAILED", symbol, str(exc)))

        indicator_builds: list[_IndicatorBuild] = []
        stale_symbols: set[str] = set()
        failed_symbols: set[str] = set()
        no_fresh_only = True
        collected_at = explicit_time or ensure_timezone_aware_utc(self.clock())
        for symbol in self.config.requested_symbols:
            frame = normalized.get(symbol)
            if frame is None:
                failed_symbols.add(symbol)
                no_fresh_only = False
                continue
            prepared = _prepare_symbol_frame(symbol, frame, issues)
            if prepared is None:
                failed_symbols.add(symbol)
                no_fresh_only = False
                continue
            builds, symbol_stale, structural_failure = self._build_indicators(symbol, prepared, collected_at, issues)
            indicator_builds.extend(builds)
            if symbol_stale:
                stale_symbols.add(symbol)
            if structural_failure:
                failed_symbols.add(symbol)
                no_fresh_only = False

        snapshots: list[ContextIndicatorSnapshot] = []
        cache_results: list[ContextStateUpdateResult] = []
        ledger_results: list[object] = []
        successful_symbols: set[str] = set()
        for build in indicator_builds:
            snapshot = build.snapshot
            result = self.cache.update(self._cache_entry_for(build, collected_at))
            cache_results.append(result)
            if result.status is ContextStateUpdateStatus.IGNORED_DUPLICATE:
                existing = self._existing_cache_entry_for(snapshot, collected_at)
                if existing is not None:
                    snapshot = replace(snapshot, details=existing.details)
            snapshots.append(snapshot)
            successful_symbols.add(snapshot.ticker_or_sector)
            should_write_ledger = (
                write_questdb
                and self.config.writes_questdb_ledger
                and self.ledger_writer is not None
                and result.status in {ContextStateUpdateStatus.WRITTEN, ContextStateUpdateStatus.REPLACED}
            )
            if should_write_ledger:
                try:
                    write_result = self.ledger_writer.write_context_indicator_snapshot(snapshot, run_id=run_id, session_id=session_id)
                    if write_result is not None:
                        ledger_results.append(write_result)
                except Exception as exc:  # noqa: BLE001 - protocol writer boundary.
                    issue = _issue("LEDGER_WRITE_FAILED", snapshot.ticker_or_sector, str(exc), snapshot.window)
                    issues.append(issue)
                    if questdb_required:
                        raise YFinanceProxyError(issue.message) from exc

        completed_at = explicit_time or ensure_timezone_aware_utc(self.clock())
        if snapshots:
            status = YFinanceProxyCollectionStatus.PARTIAL if issues or failed_symbols or stale_symbols or len(successful_symbols) < len(self.config.requested_symbols) else YFinanceProxyCollectionStatus.SUCCESS
        elif no_fresh_only and issues and all(issue.issue_type in {"NO_COMPLETED_BARS", "STALE_SOURCE_DATA", "NO_VALID_COMPLETED_CLOSE", "MISSING_EXACT_LOOKBACK"} for issue in issues):
            status = YFinanceProxyCollectionStatus.NO_FRESH_DATA
        elif no_fresh_only and not issues:
            status = YFinanceProxyCollectionStatus.NO_FRESH_DATA
        else:
            status = YFinanceProxyCollectionStatus.FAILED
            if self.config.required:
                raise YFinanceProxyError("yfinance proxy collection failed without publishing indicators")
        return YFinanceProxyCollectionResult(
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            requested_symbols=tuple(self.config.requested_symbols),
            successful_symbols=tuple(sorted(successful_symbols)),
            failed_symbols=tuple(sorted(failed_symbols)),
            stale_symbols=tuple(sorted(stale_symbols)),
            issues=tuple(issues),
            indicator_snapshots=tuple(snapshots),
            cache_update_results=tuple(cache_results),
            ledger_write_results=tuple(ledger_results),
        )

    def _download(self, symbols: tuple[str, ...]) -> Any:
        return self.download(
            tickers=symbols,
            period=self.config.period,
            interval=self.config.interval,
            group_by="column",
            auto_adjust=self.config.auto_adjust,
            actions=self.config.actions,
            repair=self.config.repair,
            keepna=self.config.keepna,
            prepost=self.config.prepost,
            threads=self.config.threads,
            timeout=self.config.timeout_seconds,
            multi_level_index=True,
            progress=False,
        )

    def _normalize_batch(self, data: Any, requested: tuple[str, ...], issues: list[YFinanceProxyIssue], *, individual: bool = False) -> tuple[dict[str, pd.DataFrame], tuple[str, ...]]:
        if not isinstance(data, pd.DataFrame) or data.empty:
            if individual:
                issues.append(_issue("EMPTY_DATAFRAME", requested[0], "empty yfinance dataframe"))
                return {}, ()
            return {}, requested
        if data.columns.nlevels == 1:
            if len(requested) != 1:
                issues.append(YFinanceProxyIssue(issue_type="AMBIGUOUS_ONE_LEVEL_COLUMNS", message="multi-symbol one-level yfinance response requires individual fallback", details={"symbols": list(requested)}))
                return {}, requested
            return {requested[0]: data.copy()}, ()
        if data.columns.nlevels != 2:
            issues.append(YFinanceProxyIssue(issue_type="UNSUPPORTED_COLUMN_SHAPE", message="yfinance response must have one or two column levels", details={"levels": data.columns.nlevels}))
            return {}, requested
        level_values = [set(map(str, data.columns.get_level_values(level))) for level in range(2)]
        price_levels = [index for index, values in enumerate(level_values) if values.intersection(_PRICE_COLUMNS)]
        if len(price_levels) != 1:
            issues.append(YFinanceProxyIssue(issue_type="AMBIGUOUS_MULTIINDEX_COLUMNS", message="could not identify exactly one price level"))
            return {}, requested
        symbol_level = 1 - price_levels[0]
        available_symbols = set(map(str, data.columns.get_level_values(symbol_level)))
        normalized: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for symbol in requested:
            if symbol not in available_symbols:
                missing.append(symbol)
                continue
            try:
                selected = data.xs(symbol, axis=1, level=symbol_level, drop_level=True)
                selected.columns = [str(column) for column in selected.columns]
                normalized[symbol] = selected.copy()
            except (KeyError, ValueError) as exc:
                issues.append(_issue("SYMBOL_NORMALIZATION_FAILED", symbol, str(exc)))
                missing.append(symbol)
        if missing and not individual:
            issues.append(YFinanceProxyIssue(issue_type="MISSING_BATCH_SYMBOLS", message="missing symbols will be retried individually", details={"symbols": missing}))
        return normalized, tuple(missing)

    def _build_indicators(self, symbol: str, prepared: _PreparedFrame, collected_at: datetime, issues: list[YFinanceProxyIssue]) -> tuple[list[_IndicatorBuild], bool, bool]:
        frame = prepared.frame
        grace = timedelta(seconds=self.config.bar_completion_grace_seconds)
        completed_cutoff = collected_at - timedelta(seconds=INTERVAL_SECONDS) - grace
        completed = frame.loc[frame.index <= pd.Timestamp(completed_cutoff)]
        if completed.empty:
            issues.append(_issue("NO_COMPLETED_BARS", symbol, "no completed bars after completion grace"))
            return [], False, False
        valid_completed = completed[completed["_usable_close"]]
        if valid_completed.empty:
            issues.append(_issue("NO_VALID_COMPLETED_CLOSE", symbol, "no completed valid close"))
            return [], False, False
        latest_start = valid_completed.index[-1]
        latest_close = float(valid_completed.loc[latest_start, "Close"])
        latest_end = latest_start.to_pydatetime() + timedelta(seconds=INTERVAL_SECONDS)
        freshness_seconds = (collected_at - latest_end).total_seconds()
        if freshness_seconds > self.config.max_staleness_seconds:
            issues.append(_issue("STALE_SOURCE_DATA", symbol, "latest completed bar is stale", details={"freshness_seconds": freshness_seconds}))
            return [], True, False
        builds = [self._indicator_build(symbol, "latest_close", "5m", latest_close, latest_start, latest_end, freshness_seconds, None, None, collected_at=collected_at)]
        for indicator_name, minutes in (("return_5m", 5), ("return_15m", 15), ("return_60m", 60)):
            target_start = latest_start - pd.Timedelta(minutes=minutes)
            window = f"{minutes}m"
            if target_start not in completed.index:
                issues.append(_issue("MISSING_EXACT_LOOKBACK", symbol, "exact lookback timestamp missing", window))
                continue
            target_close = completed.loc[target_start, "Close"]
            if not _valid_close(target_close):
                issues.append(_issue("INVALID_TARGET_CLOSE", symbol, "exact lookback close is invalid", window))
                continue
            if not _valid_close(latest_close):
                issues.append(_issue("INVALID_LATEST_CLOSE", symbol, "latest close is invalid", window))
                continue
            value = latest_close / float(target_close) - 1.0
            builds.append(self._indicator_build(symbol, indicator_name, window, value, latest_start, latest_end, freshness_seconds, target_start.to_pydatetime(), float(target_close), latest_close=latest_close, collected_at=collected_at))
        return builds, False, False

    def _indicator_build(self, symbol: str, indicator_name: str, window: str, value: float, latest_start: pd.Timestamp, latest_end: datetime, freshness_seconds: float, target_start: datetime | None, target_close: float | None, *, latest_close: float | None = None, collected_at: datetime) -> _IndicatorBuild:
        source_event_time = ensure_timezone_aware_utc(latest_end)
        indicator_id = deterministic_context_indicator_id(SOURCE_NAME, symbol, indicator_name, window, source_event_time)
        valid_until = source_event_time + timedelta(seconds=self.config.max_staleness_seconds)
        effective_from = source_event_time + timedelta(seconds=self.config.bar_completion_grace_seconds)
        details: dict[str, object] = {
            "context_indicator_id": indicator_id,
            "symbol": symbol,
            "indicator_name": indicator_name,
            "window": window,
            "units": "price" if indicator_name == "latest_close" else "return",
            "bar_start": to_utc_iso(latest_start.to_pydatetime()),
            "bar_end": to_utc_iso(source_event_time),
            "freshness_seconds": freshness_seconds,
            "source": SOURCE_NAME,
            "price_basis": "raw",
            "interval": SUPPORTED_INTERVAL,
        }
        if target_start is not None:
            details.update({"target_bar_start": to_utc_iso(target_start), "latest_close": latest_close, "target_close": target_close, "exact_timestamp_match": True})
        details = attach_provenance(
            details,
            {
                "source_event_time": source_event_time,
                "source_observed_at": None,
                "available_at": None,
                "collected_at": collected_at,
                "effective_from": effective_from,
                "valid_until": valid_until,
                "availability_basis": "policy_completion_grace_unverified",
                "research_asof_eligible": False,
                "revision_id": None,
                "vintage_id": None,
                "source_record_id": f"{SOURCE_NAME}:{symbol}:{SUPPORTED_INTERVAL}:{indicator_name}:{window}:{to_utc_iso(source_event_time)}",
            },
        )
        snapshot = ContextIndicatorSnapshot(
            snapshot_time=collected_at,
            source=SOURCE_NAME,
            ticker_or_sector=symbol,
            indicator_name=indicator_name,
            value=float(value),
            context_indicator_id=indicator_id,
            window=window,
            units="price" if indicator_name == "latest_close" else "return",
            freshness_seconds=freshness_seconds,
            source_event_time=source_event_time,
            details=details,
        )
        return _IndicatorBuild(snapshot=snapshot, bar_start=latest_start.to_pydatetime(), bar_end=source_event_time, details=details)

    def _cache_entry_for(self, build: _IndicatorBuild, collected_at: datetime) -> Any:
        snapshot = build.snapshot
        registration = self.registry[snapshot.ticker_or_sector]
        name = cache_indicator_name(snapshot.ticker_or_sector, snapshot.indicator_name, snapshot.window or "")
        valid_until = (snapshot.source_event_time or build.bar_end) + timedelta(seconds=self.config.max_staleness_seconds)
        kwargs = dict(name=name, value=float(snapshot.value), severity="INFO", source=SOURCE_NAME, updated_at=snapshot.source_event_time or build.bar_end, source_event_time=snapshot.source_event_time, valid_until=valid_until, details=build.details)
        if registration.scope is ContextScope.GLOBAL:
            return make_global_context_entry(**kwargs)
        return make_sector_context_entry(sector=registration.sector or "", **kwargs)

    def _existing_cache_entry_for(
        self,
        snapshot: ContextIndicatorSnapshot,
        now: datetime,
    ) -> Any:
        registration = self.registry[snapshot.ticker_or_sector]
        name = cache_indicator_name(
            snapshot.ticker_or_sector,
            snapshot.indicator_name,
            snapshot.window or "",
        )
        if registration.scope is ContextScope.GLOBAL:
            return self.cache.get_global(name, now=now, include_expired=True)
        return self.cache.get_sector(
            registration.sector or "",
            name,
            now=now,
            include_expired=True,
        )

    def _failed_result(self, started_at: datetime, issues: tuple[YFinanceProxyIssue, ...], *, completed_at: datetime | None = None) -> YFinanceProxyCollectionResult:
        if self.config.required:
            raise YFinanceProxyError(issues[0].message if issues else "yfinance proxy collection failed")
        completed_at = completed_at or ensure_timezone_aware_utc(self.clock())
        return YFinanceProxyCollectionResult(status=YFinanceProxyCollectionStatus.FAILED, started_at=started_at, completed_at=completed_at, requested_symbols=tuple(sorted(self.config.requested_symbols)), successful_symbols=(), failed_symbols=tuple(sorted(self.config.requested_symbols)), stale_symbols=(), issues=issues, indicator_snapshots=(), cache_update_results=(), ledger_write_results=())


def deterministic_context_indicator_id(source: str, symbol: str, indicator_name: str, window: str, source_event_time: datetime) -> str:
    payload = json.dumps([source, _normalize_symbol(symbol), indicator_name, window, to_utc_iso(source_event_time)], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:DIGEST_PREFIX_HEX_LENGTH]
    return f"context_indicator_{digest}"


def cache_indicator_name(symbol: str, indicator_name: str, window: str) -> str:
    return f"{SOURCE_NAME}:{_normalize_symbol(symbol)}:{indicator_name}:{window}"


def build_proxy_registry(symbols_config: Mapping[str, Any] | None = None) -> dict[str, ProxySymbolRegistration]:
    if symbols_config is not None:
        groups = symbols_config.get("context_symbols", {})
        expected = {
            "broad_market": {"SPY", "QQQ", "IWM"},
            "oil_sector": {"XLE", "XOP", "OIH"},
            "industrial_sector": {"XLI"},
            "defense_sector": {"PPA", "ITA"},
            "volatility": {"^VIX"},
            "commodities": {"GLD", "WTI", "BRENT", "NATURAL_GAS"},
        }
        for group_name, expected_symbols in expected.items():
            actual = set(groups.get(group_name, []))
            if not expected_symbols.issubset(actual):
                raise YFinanceProxyError(f"context_symbols.{group_name} missing required PR25 symbols")
    return {symbol: ProxySymbolRegistration(symbol=symbol, scope=scope, sector=sector) for symbol, (scope, sector) in _DEFAULT_REGISTRY.items()}


def get_proxy_indicator(cache: ContextStateCache, registry: Mapping[str, ProxySymbolRegistration], *, symbol: str, indicator_name: str, window: str, now: datetime | None = None, include_expired: bool = False) -> ProxyIndicatorReading | None:
    registration = registry.get(_normalize_symbol(symbol))
    if registration is None:
        return None
    name = cache_indicator_name(registration.symbol, indicator_name, window)
    entry = cache.get_global(name, now=now, include_expired=include_expired) if registration.scope is ContextScope.GLOBAL else cache.get_sector(registration.sector or "", name, now=now, include_expired=include_expired)
    if entry is None or entry.source_event_time is None or not _finite_number(entry.value):
        return None
    context_indicator_id = entry.details.get("context_indicator_id")
    if not isinstance(context_indicator_id, str) or not context_indicator_id:
        return None
    return ProxyIndicatorReading(symbol=registration.symbol, sector=registration.sector, indicator_name=indicator_name, window=window, value=float(entry.value), source_event_time=entry.source_event_time, valid_until=entry.valid_until, context_indicator_id=context_indicator_id)


def get_sector_proxy_indicators(cache: ContextStateCache, registry: Mapping[str, ProxySymbolRegistration], *, sector: str, indicator_name: str, window: str, now: datetime | None = None, include_expired: bool = False) -> dict[str, ProxyIndicatorReading]:
    normalized_sector = _normalize_symbol(sector)
    readings: dict[str, ProxyIndicatorReading] = {}
    for registration in sorted(registry.values(), key=lambda item: item.symbol):
        if registration.scope is ContextScope.SECTOR and registration.sector == normalized_sector:
            reading = get_proxy_indicator(cache, registry, symbol=registration.symbol, indicator_name=indicator_name, window=window, now=now, include_expired=include_expired)
            if reading is not None:
                readings[registration.symbol] = reading
    return readings


def _prepare_symbol_frame(symbol: str, frame: pd.DataFrame, issues: list[YFinanceProxyIssue]) -> _PreparedFrame | None:
    if not isinstance(frame.index, pd.DatetimeIndex):
        issues.append(_issue("INVALID_INDEX", symbol, "symbol dataframe must use a DatetimeIndex"))
        return None
    if frame.index.tz is None:
        issues.append(_issue("NAIVE_TIMESTAMP", symbol, "symbol dataframe timestamps must be timezone-aware"))
        return None
    data = frame.copy()
    data.index = data.index.tz_convert("UTC")
    data = data.sort_index()
    if "Close" not in data.columns:
        issues.append(_issue("MISSING_CLOSE_COLUMN", symbol, f"Source response for {symbol} does not contain a Close column"))
        return None
    close = pd.to_numeric(data["Close"], errors="coerce")
    data["Close"] = close
    data["_usable_close"] = close.map(_valid_close)
    duplicate_times = data.index[data.index.duplicated()].unique()
    for timestamp in duplicate_times:
        rows = data.loc[[timestamp]]
        usable = sorted({float(value) for value in rows["Close"] if _valid_close(value)})
        if len(usable) > 1:
            issues.append(_issue("CONFLICTING_DUPLICATE_TIMESTAMP", symbol, "duplicate timestamp has conflicting close values"))
            return None
    data = data[~data.index.duplicated(keep="first")]
    invalid = frozenset(data.index[~data["_usable_close"]])
    return _PreparedFrame(frame=data, invalid_close_timestamps=invalid)


def _download_yfinance(**kwargs: Any) -> pd.DataFrame:
    import yfinance as yf

    return yf.download(**kwargs)


def _default_registry_tuple() -> tuple[ProxySymbolRegistration, ...]:
    return tuple(build_proxy_registry(None).values())


def _issue(issue_type: str, symbol: str, message: str, window: str | None = None, details: dict[str, object] | None = None) -> YFinanceProxyIssue:
    return YFinanceProxyIssue(issue_type=issue_type, symbol=symbol, window=window, message=message, details={} if details is None else details)


def _valid_close(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _positive_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise YFinanceProxyError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise YFinanceProxyError(f"{field_name} must be positive and finite")
    return number


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise YFinanceProxyError(f"{field_name} must be a positive int")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise YFinanceProxyError(f"{field_name} must be a non-negative int")
    return value


def _normalize_symbol(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise YFinanceProxyError("symbol must be a non-empty string")
    return value.strip().upper()


__all__ = [
    "ContextIndicatorWriter",
    "DIGEST_PREFIX_HEX_LENGTH",
    "ProxyIndicatorReading",
    "ProxySymbolRegistration",
    "YFinanceProxyCollectionResult",
    "YFinanceProxyCollectionStatus",
    "YFinanceProxyCollector",
    "YFinanceProxyConfig",
    "YFinanceProxyError",
    "YFinanceProxyIssue",
    "build_proxy_registry",
    "cache_indicator_name",
    "deterministic_context_indicator_id",
    "get_proxy_indicator",
    "get_sector_proxy_indicators",
]
