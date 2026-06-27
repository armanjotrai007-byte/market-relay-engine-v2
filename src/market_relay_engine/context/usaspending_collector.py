"""One-shot USAspending contract-award evidence collector."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import errno
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests
import yaml

from market_relay_engine.common.config import repo_root
from market_relay_engine.common.time import ensure_timezone_aware_utc, to_utc_iso, utc_now
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    ContextStateUpdateResult,
    ContextStateUpdateStatus,
    make_ticker_context_entry,
)
from market_relay_engine.contracts.context import ContextIndicatorSnapshot
from market_relay_engine.questdb.jsonl_fallback import EmergencyJSONLLedgerFallback


SOURCE_NAME = "usaspending_awards_v1"
INDICATOR_NAME = "usaspending_contract_award_event_v1"
WINDOW_NAME = "recent_award_discovery"
SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
LAST_UPDATED_URL = "https://api.usaspending.gov/api/v2/awards/last_updated/"
AWARD_DETAIL_URL_TEMPLATE = "https://api.usaspending.gov/api/v2/awards/{award_id}/"
AWARD_FUNDING_URL = "https://api.usaspending.gov/api/v2/awards/funding/"
MAPPING_VERSION = "usaspending_recipient_map_v1"
CHECKPOINT_SCHEMA_VERSION = "usaspending_award_checkpoint_v1"
RECIPIENT_DISCOVERY_METHOD = "text_search_then_exact_uei_verification"
RESEARCH_HORIZON_SECONDS = 86_400
SEEN_EVENT_RETENTION_CALENDAR_DAYS = 45
CONTRACT_AWARD_TYPE_CODES = ("A", "B", "C", "D")
SEARCH_FIELDS = (
    "Award ID",
    "generated_internal_id",
    "Recipient UEI",
    "Recipient Name",
    "Last Modified Date",
    "Base Obligation Date",
    "Award Amount",
    "Contract Award Type",
    "NAICS",
    "PSC",
)
FUNDING_EVIDENCE_FIELDS = (
    "transaction_obligated_amount",
    "reporting_fiscal_year",
    "reporting_fiscal_quarter",
    "reporting_fiscal_month",
    "awarding_agency_name",
    "funding_agency_name",
    "federal_account",
    "account_title",
    "program_activity_code",
    "program_activity_name",
    "object_class",
    "object_class_name",
)
NEW_YORK = ZoneInfo("America/New_York")


class USAspendingCollectorError(RuntimeError):
    """Raised when USAspending collection cannot proceed safely."""


class USAspendingCollectorBusyError(USAspendingCollectorError):
    """Raised when another enabled USAspending collection owns the checkpoint lock."""


class USAspendingCollectionStatus(str, Enum):
    DISABLED = "DISABLED"
    FAILED = "FAILED"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    SUCCESS = "SUCCESS"


class USAspendingEventClassification(str, Enum):
    NEW_AWARD_DISCOVERED = "NEW_AWARD_DISCOVERED"
    AWARD_REVISION_DISCOVERED = "AWARD_REVISION_DISCOVERED"
    LATE_OR_BACKFILL_DISCOVERY = "LATE_OR_BACKFILL_DISCOVERY"


@dataclass(frozen=True, kw_only=True)
class USAspendingConfig:
    enabled: bool = False
    api_key_required: bool = False
    purpose: str = (
        "Ticker-linked U.S. government contract award discovery for future profitability research."
    )
    feeds_memory_cache: bool = True
    writes_questdb_ledger: bool = True
    used_in_per_tick_loop: bool = False
    timeout_seconds: float = 10.0
    intended_poll_interval_seconds: int = 900
    discovery_last_modified_lookback_calendar_days: int = 14
    source_last_updated_max_age_calendar_days: int = 3
    search_limit_per_recipient: int = 100
    max_award_details_per_recipient_per_run: int = 50
    funding_limit_per_award: int = 100
    revision_recheck_calendar_days: int = 45
    max_revision_rechecks_per_run: int = 50
    late_discovery_calendar_days: int = 2
    award_registry_retention_calendar_days: int = 180
    checkpoint_path: str = "data/usaspending/award_checkpoint.json"
    recipient_map_path: str = "config/usaspending_recipient_ticker_map.yaml"
    contract_awards_only: bool = True

    def __post_init__(self) -> None:
        for field_name in ("enabled", "writes_questdb_ledger"):
            if not isinstance(getattr(self, field_name), bool):
                raise USAspendingCollectorError(f"{field_name} must be bool")
        if self.api_key_required is not False:
            raise USAspendingCollectorError("api_key_required must be false")
        if self.feeds_memory_cache is not True:
            raise USAspendingCollectorError("feeds_memory_cache must be true")
        if self.used_in_per_tick_loop is not False:
            raise USAspendingCollectorError("used_in_per_tick_loop must be false")
        if self.contract_awards_only is not True:
            raise USAspendingCollectorError("contract_awards_only must be true")
        object.__setattr__(self, "purpose", _required_string(self.purpose, "purpose"))
        object.__setattr__(
            self,
            "timeout_seconds",
            _positive_float(self.timeout_seconds, "timeout_seconds"),
        )
        object.__setattr__(
            self,
            "intended_poll_interval_seconds",
            _bounded_int(
                self.intended_poll_interval_seconds,
                "intended_poll_interval_seconds",
                minimum=60,
                maximum=86_400,
            ),
        )
        _set_bounded_int(self, "discovery_last_modified_lookback_calendar_days", 1, 31)
        _set_bounded_int(self, "source_last_updated_max_age_calendar_days", 0, 14)
        _set_bounded_int(self, "search_limit_per_recipient", 1, 500)
        _set_bounded_int(self, "max_award_details_per_recipient_per_run", 1, 100)
        _set_bounded_int(self, "funding_limit_per_award", 1, 100)
        _set_bounded_int(self, "revision_recheck_calendar_days", 1, 90)
        _set_bounded_int(self, "max_revision_rechecks_per_run", 1, 100)
        _set_bounded_int(self, "late_discovery_calendar_days", 0, 14)
        _set_bounded_int(self, "award_registry_retention_calendar_days", 45, 730)
        object.__setattr__(
            self,
            "checkpoint_path",
            _relative_path_string(self.checkpoint_path, "checkpoint_path"),
        )
        object.__setattr__(
            self,
            "recipient_map_path",
            _relative_path_string(self.recipient_map_path, "recipient_map_path"),
        )

    @classmethod
    def from_repository_config(
        cls, context_sources: Mapping[str, Any]
    ) -> "USAspendingConfig":
        structured = _required_mapping(context_sources, "structured_sources")
        source = _required_mapping(structured, "usaspending")
        required_fields = {
            "enabled",
            "api_key_required",
            "purpose",
            "feeds_memory_cache",
            "writes_questdb_ledger",
            "used_in_per_tick_loop",
            "timeout_seconds",
            "intended_poll_interval_seconds",
            "discovery_last_modified_lookback_calendar_days",
            "source_last_updated_max_age_calendar_days",
            "search_limit_per_recipient",
            "max_award_details_per_recipient_per_run",
            "funding_limit_per_award",
            "revision_recheck_calendar_days",
            "max_revision_rechecks_per_run",
            "late_discovery_calendar_days",
            "award_registry_retention_calendar_days",
            "checkpoint_path",
            "recipient_map_path",
            "contract_awards_only",
        }
        missing = sorted(required_fields.difference(source))
        if missing:
            raise USAspendingCollectorError(
                f"missing required USAspending configuration field: {missing[0]}"
            )
        unexpected = sorted(set(source).difference(required_fields))
        if unexpected:
            raise USAspendingCollectorError(
                f"unexpected USAspending configuration field: {unexpected[0]}"
            )
        return cls(**{field_name: source[field_name] for field_name in required_fields})


@dataclass(frozen=True, kw_only=True)
class USAspendingRecipientMapping:
    recipient_uei: str
    recipient_name: str
    ticker: str
    issuer_name: str
    mapping_confidence: str
    economic_beneficiary: str
    active: bool
    mapping_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recipient_uei",
            _required_string(self.recipient_uei, "recipient_uei"),
        )
        object.__setattr__(
            self,
            "recipient_name",
            _required_string(self.recipient_name, "recipient_name"),
        )
        ticker = _required_string(self.ticker, "ticker").upper()
        if not ticker.isascii() or not re.fullmatch(r"[A-Z0-9.\-]+", ticker):
            raise USAspendingCollectorError("ticker must be uppercase ASCII and non-empty")
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(
            self,
            "issuer_name",
            _required_string(self.issuer_name, "issuer_name"),
        )
        if self.mapping_confidence != "confirmed":
            raise USAspendingCollectorError("mapping_confidence must equal confirmed")
        if self.economic_beneficiary != "prime_recipient":
            raise USAspendingCollectorError("economic_beneficiary must equal prime_recipient")
        if not isinstance(self.active, bool):
            raise USAspendingCollectorError("active must be bool")
        if self.mapping_version != MAPPING_VERSION:
            raise USAspendingCollectorError(
                f"mapping_version must equal {MAPPING_VERSION}"
            )


@dataclass(frozen=True, kw_only=True)
class USAspendingAwardCandidate:
    search_detail_lookup_id: str
    award_id: str | None
    recipient_uei: str
    recipient_name: str | None
    last_modified_date: str | None
    base_obligation_date: str | None
    award_amount: float | None
    contract_award_type: str | None
    naics: str | None
    psc: str | None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class USAspendingAwardEvent:
    canonical_award_id: str
    generated_unique_award_id: str
    search_detail_lookup_id: str
    ticker: str
    issuer_name: str
    recipient_uei: str
    recipient_name: str | None
    mapping_version: str
    mapping_confidence: str
    event_classification: USAspendingEventClassification
    context_indicator_id: str
    semantic_event_fingerprint: str
    semantic_event_identity: dict[str, object]
    cache_entry_name: str
    source_business_date: date | None
    source_event_time: datetime | None
    collector_observed_at: datetime
    event_first_observed_at: datetime
    research_horizon_ends_at: datetime
    award_type: str | None
    award_type_description: str | None
    award_category: str | None
    award_description: str | None
    date_signed: str | None
    action_date: str | None
    award_last_modified_date: str | None
    period_of_performance_start: str | None
    period_of_performance_end: str | None
    total_obligation_usd: float | None
    base_exercised_options_usd: float | None
    base_and_all_options_usd: float | None
    awarding_agency_name: str | None
    awarding_agency_code: str | None
    funding_agency_name: str | None
    funding_agency_code: str | None
    naics_code: str | None
    naics_description: str | None
    psc_code: str | None
    psc_description: str | None
    funding_records_returned_count: int
    funding_has_next_page: bool
    funding_page_complete: bool
    funding_record_evidence: tuple[dict[str, object], ...]
    funding_evidence_fingerprint: str


@dataclass(frozen=True, kw_only=True)
class USAspendingIssue:
    issue_type: str
    message: str
    recipient_uei: str | None = None
    ticker: str | None = None
    award_identifier: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class USAspendingCollectionResult:
    status: USAspendingCollectionStatus
    collector_observed_at: datetime
    coverage_complete: bool = True
    source_last_updated_date: date | None = None
    source_health_age_calendar_days: int | None = None
    recipient_discovery_method: str = RECIPIENT_DISCOVERY_METHOD
    recipient_search_is_complete_coverage: bool = False
    rejected_search_candidate_count: int = 0
    indicator_snapshots: tuple[ContextIndicatorSnapshot, ...] = ()
    cache_update_results: tuple[ContextStateUpdateResult, ...] = ()
    ledger_write_results: tuple[object, ...] = ()
    issues: tuple[USAspendingIssue, ...] = ()


class USAspendingClient(Protocol):
    def fetch_last_updated(self) -> Mapping[str, object]:
        ...

    def search_spending_by_award(
        self,
        *,
        recipient_uei: str,
        start_date: str,
        end_date: str,
        limit: int,
    ) -> Mapping[str, object]:
        ...

    def fetch_award_detail(self, award_id: str) -> Mapping[str, object]:
        ...

    def fetch_award_funding(self, award_id: str, *, limit: int) -> Mapping[str, object]:
        ...


class USAspendingCheckpointStore(Protocol):
    def acquire_lock(self) -> None:
        ...

    def release_lock(self) -> None:
        ...

    def read(self) -> dict[str, object]:
        ...

    def write(self, checkpoint: Mapping[str, object]) -> None:
        ...


class USAspendingLedgerWriter(Protocol):
    def write_context_indicator_snapshot(
        self,
        snapshot: ContextIndicatorSnapshot,
        **kwargs: Any,
    ) -> object | None:
        ...


class USAspendingEmergencyLedgerFallback(Protocol):
    def append_record(
        self,
        *,
        record_type: str,
        target_table: str,
        record_id: str,
        event_time: datetime,
        source: str,
        ticker_or_sector: str,
        primary_write_failure: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> object:
        ...


class USAspendingHTTPClient:
    """Small official USAspending API client with no credentials."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        request_get: Callable[..., Any] = requests.get,
        request_post: Callable[..., Any] = requests.post,
    ) -> None:
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.request_get = request_get
        self.request_post = request_post

    def fetch_last_updated(self) -> Mapping[str, object]:
        return self._get_json(LAST_UPDATED_URL)

    def search_spending_by_award(
        self,
        *,
        recipient_uei: str,
        start_date: str,
        end_date: str,
        limit: int,
    ) -> Mapping[str, object]:
        body = {
            "subawards": False,
            "spending_level": "awards",
            "limit": limit,
            "page": 1,
            "sort": "Last Modified Date",
            "order": "desc",
            "filters": {
                "award_type_codes": list(CONTRACT_AWARD_TYPE_CODES),
                "recipient_search_text": [recipient_uei],
                "time_period": [
                    {
                        "start_date": start_date,
                        "end_date": end_date,
                        "date_type": "last_modified_date",
                    }
                ],
            },
            "fields": list(SEARCH_FIELDS),
        }
        return self._post_json(SEARCH_URL, body)

    def fetch_award_detail(self, award_id: str) -> Mapping[str, object]:
        safe_award_id = _required_string(award_id, "award_id")
        return self._get_json(AWARD_DETAIL_URL_TEMPLATE.format(award_id=safe_award_id))

    def fetch_award_funding(self, award_id: str, *, limit: int) -> Mapping[str, object]:
        body = {
            "award_id": _required_string(award_id, "award_id"),
            "page": 1,
            "limit": _bounded_int(limit, "limit", minimum=1, maximum=100),
            "sort": "reporting_fiscal_date",
            "order": "desc",
        }
        return self._post_json(AWARD_FUNDING_URL, body)

    def _get_json(self, url: str) -> Mapping[str, object]:
        try:
            response = self.request_get(url, timeout=self.timeout_seconds)
        except requests.RequestException:
            raise USAspendingCollectorError("official USAspending GET request failed") from None
        return _response_json(response, url)

    def _post_json(self, url: str, body: Mapping[str, object]) -> Mapping[str, object]:
        try:
            response = self.request_post(url, json=dict(body), timeout=self.timeout_seconds)
        except requests.RequestException:
            raise USAspendingCollectorError("official USAspending POST request failed") from None
        return _response_json(response, url)


class JSONUSAspendingCheckpointStore:
    """JSON checkpoint store with same-directory atomic replacement and lock file."""

    def __init__(self, path: str | Path) -> None:
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = repo_root() / resolved
        self.path = resolved
        self.lock_path = resolved.with_name(f"{resolved.name}.lock")
        self._lock_fd: int | None = None

    def acquire_lock(self) -> None:
        if self._lock_fd is not None:
            raise self._busy_error()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            fd = os.open(str(self.lock_path), flags, 0o644)
        except OSError as exc:
            raise USAspendingCollectorError("USAspending checkpoint lock open failed") from exc
        try:
            if not _try_lock_fd(fd):
                raise self._busy_error()
            _write_lock_owner(fd, self.lock_path)
            self._lock_fd = fd
        except Exception:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            os.close(fd)
            raise

    def release_lock(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        if fd is not None:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            finally:
                os.close(fd)

    def _busy_error(self) -> USAspendingCollectorBusyError:
        message = f"USAspending checkpoint lock is held: {self.lock_path}"
        diagnostics = _read_lock_owner_diagnostics(self.lock_path)
        if diagnostics is not None:
            message = f"{message}; owner={diagnostics}"
        return USAspendingCollectorBusyError(message)

    def read(self) -> dict[str, object]:
        if not self.path.exists():
            return _empty_checkpoint()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise USAspendingCollectorError("USAspending checkpoint is unreadable") from exc
        if not isinstance(loaded, dict):
            raise USAspendingCollectorError("USAspending checkpoint must be a JSON object")
        return _normalize_checkpoint(loaded)

    def write(self, checkpoint: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            _normalize_checkpoint(dict(checkpoint)),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        tmp_path = self.path.with_name(f".{self.path.name}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except OSError as exc:
            raise USAspendingCollectorError("USAspending checkpoint persistence failed") from exc
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


_WINDOWS_LOCK_BYTE_OFFSET = 4096


def _try_lock_fd(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_BYTE_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _write_lock_owner(fd: int, lock_path: Path) -> None:
    owner = {
        "pid": os.getpid(),
        "acquired_at_utc": to_utc_iso(datetime.now(UTC)),
        "lock_path": str(lock_path),
    }
    payload = json.dumps(owner, ensure_ascii=True, sort_keys=True)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload.encode("utf-8"))
    os.fsync(fd)


def _read_lock_owner_diagnostics(lock_path: Path) -> str | None:
    try:
        raw = lock_path.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return None
    text = raw.strip()
    if not text:
        return "<empty>"
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(loaded, Mapping):
        return json.dumps(loaded, ensure_ascii=True, sort_keys=True)
    return text


class USAspendingCollector:
    """Collect one bounded set of USAspending award evidence when invoked."""

    def __init__(
        self,
        *,
        cache: ContextStateCache,
        config: USAspendingConfig,
        client: USAspendingClient | None = None,
        ledger_writer: USAspendingLedgerWriter | None = None,
        emergency_ledger_fallback: USAspendingEmergencyLedgerFallback | None = None,
        checkpoint_store: USAspendingCheckpointStore | None = None,
        recipient_mappings: Sequence[USAspendingRecipientMapping] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.cache = cache
        self.config = config
        self.client = client or USAspendingHTTPClient(timeout_seconds=config.timeout_seconds)
        self.ledger_writer = ledger_writer
        self.emergency_ledger_fallback = emergency_ledger_fallback
        self.checkpoint_store = checkpoint_store or JSONUSAspendingCheckpointStore(
            config.checkpoint_path
        )
        self.recipient_mappings = None if recipient_mappings is None else tuple(recipient_mappings)
        self.clock = clock

    def collect(
        self,
        *,
        evaluation_time: datetime | None = None,
        write_questdb: bool = False,
        questdb_required: bool = False,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> USAspendingCollectionResult:
        observed_at = ensure_timezone_aware_utc(evaluation_time or self.clock())
        if not self.config.enabled:
            return USAspendingCollectionResult(
                status=USAspendingCollectionStatus.DISABLED,
                collector_observed_at=observed_at,
            )

        mappings = self._load_active_mappings()
        self.checkpoint_store.acquire_lock()
        try:
            checkpoint = self.checkpoint_store.read()
            result = self._collect_locked(
                observed_at=observed_at,
                mappings=mappings,
                checkpoint=checkpoint,
                write_questdb=write_questdb,
                questdb_required=questdb_required,
                run_id=run_id,
                session_id=session_id,
            )
            return result
        finally:
            self.checkpoint_store.release_lock()

    def _load_active_mappings(self) -> tuple[USAspendingRecipientMapping, ...]:
        mappings = (
            load_recipient_mappings(self.config.recipient_map_path)
            if self.recipient_mappings is None
            else tuple(self.recipient_mappings)
        )
        active = tuple(mapping for mapping in mappings if mapping.active)
        if not active:
            raise USAspendingCollectorError(
                "enabled USAspending collection requires at least one active confirmed mapping"
            )
        return active

    def _collect_locked(
        self,
        *,
        observed_at: datetime,
        mappings: tuple[USAspendingRecipientMapping, ...],
        checkpoint: dict[str, object],
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> USAspendingCollectionResult:
        issues: list[USAspendingIssue] = []
        snapshots: list[ContextIndicatorSnapshot] = []
        cache_results: list[ContextStateUpdateResult] = []
        ledger_results: list[object] = []
        coverage_complete = True
        rejected_search_candidate_count = 0
        source_last_updated_date, source_age, source_current = self._source_health(
            observed_at,
            issues,
        )
        if issues:
            coverage_complete = False

        start_date, end_date = discovery_window(
            observed_at,
            self.config.discovery_last_modified_lookback_calendar_days,
        )
        recipient_path_failures = 0
        processed_canonical_ids: set[str] = set()
        active_by_uei = {mapping.recipient_uei: mapping for mapping in mappings}

        for mapping in mappings:
            try:
                payload = self.client.search_spending_by_award(
                    recipient_uei=mapping.recipient_uei,
                    start_date=start_date,
                    end_date=end_date,
                    limit=self.config.search_limit_per_recipient,
                )
            except Exception as exc:  # noqa: BLE001 - source adapter boundary.
                recipient_path_failures += 1
                coverage_complete = False
                issues.append(
                    USAspendingIssue(
                        issue_type="RECIPIENT_DISCOVERY_FAILED",
                        message="official USAspending award search failed",
                        recipient_uei=mapping.recipient_uei,
                        ticker=mapping.ticker,
                        details={"error_type": type(exc).__name__},
                    )
                )
                continue

            metadata = _mapping_or_empty(payload.get("page_metadata"))
            if metadata.get("hasNext") is True:
                coverage_complete = False
                issues.append(
                    USAspendingIssue(
                        issue_type="SEARCH_TRUNCATED",
                        message="bounded USAspending award search has additional pages",
                        recipient_uei=mapping.recipient_uei,
                        ticker=mapping.ticker,
                    )
                )
            candidates, rejected = _candidates_from_search(payload, mapping)
            rejected_search_candidate_count += rejected
            candidates = tuple(sorted(candidates, key=_candidate_sort_key, reverse=True))
            if len(candidates) > self.config.max_award_details_per_recipient_per_run:
                coverage_complete = False
                issues.append(
                    USAspendingIssue(
                        issue_type="CANDIDATE_ENRICHMENT_CAP_REACHED",
                        message="exact-UEI candidates exceeded configured enrichment cap",
                        recipient_uei=mapping.recipient_uei,
                        ticker=mapping.ticker,
                        details={"candidate_count": len(candidates)},
                    )
                )
                candidates = candidates[: self.config.max_award_details_per_recipient_per_run]

            for candidate in candidates:
                accepted = self._process_candidate(
                    candidate=candidate,
                    mapping=mapping,
                    observed_at=observed_at,
                    checkpoint=checkpoint,
                    issues=issues,
                    snapshots=snapshots,
                    cache_results=cache_results,
                    ledger_results=ledger_results,
                    write_questdb=write_questdb,
                    questdb_required=questdb_required,
                    run_id=run_id,
                    session_id=session_id,
                )
                coverage_complete = coverage_complete and accepted.coverage_complete
                if accepted.canonical_award_id is not None:
                    processed_canonical_ids.add(accepted.canonical_award_id)

        revision_outcome = self._run_revision_rechecks(
            observed_at=observed_at,
            active_by_uei=active_by_uei,
            processed_canonical_ids=processed_canonical_ids,
            checkpoint=checkpoint,
            issues=issues,
            snapshots=snapshots,
            cache_results=cache_results,
            ledger_results=ledger_results,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )
        coverage_complete = coverage_complete and revision_outcome.coverage_complete

        failed_all_paths = recipient_path_failures == len(mappings)
        checkpoint_must_persist = not failed_all_paths or revision_outcome.checkpoint_changed
        if checkpoint_must_persist:
            try:
                self._persist_checkpoint(
                    checkpoint,
                    observed_at=observed_at,
                    source_last_updated_date=source_last_updated_date,
                    advance_discovery_metadata=not failed_all_paths,
                    prune=(
                        not failed_all_paths
                        and coverage_complete
                        and not issues
                        and source_current is True
                    ),
                )
            except USAspendingCollectorError as exc:
                coverage_complete = False
                issues.append(
                    USAspendingIssue(
                        issue_type="CHECKPOINT_PERSISTENCE_FAILED",
                        message=str(exc),
                    )
                )

        status = _status_from_run(
            failed_all_paths=failed_all_paths,
            source_current=source_current,
            coverage_complete=coverage_complete,
            issues=issues,
        )
        return USAspendingCollectionResult(
            status=status,
            collector_observed_at=observed_at,
            coverage_complete=coverage_complete,
            source_last_updated_date=source_last_updated_date,
            source_health_age_calendar_days=source_age,
            rejected_search_candidate_count=rejected_search_candidate_count,
            indicator_snapshots=tuple(snapshots),
            cache_update_results=tuple(cache_results),
            ledger_write_results=tuple(ledger_results),
            issues=tuple(issues),
        )

    def _source_health(
        self,
        observed_at: datetime,
        issues: list[USAspendingIssue],
    ) -> tuple[date | None, int | None, bool | None]:
        try:
            payload = self.client.fetch_last_updated()
        except Exception as exc:  # noqa: BLE001 - source adapter boundary.
            issues.append(
                USAspendingIssue(
                    issue_type="SOURCE_LAST_UPDATED_FAILED",
                    message="official USAspending last-updated request failed",
                    details={"error_type": type(exc).__name__},
                )
            )
            return None, None, None
        value = payload.get("last_updated")
        if not isinstance(value, str) or not value.strip():
            issues.append(
                USAspendingIssue(
                    issue_type="SOURCE_LAST_UPDATED_EMPTY",
                    message="official USAspending last-updated response is empty",
                )
            )
            return None, None, None
        try:
            source_date = parse_source_last_updated_date(value)
        except USAspendingCollectorError:
            issues.append(
                USAspendingIssue(
                    issue_type="SOURCE_LAST_UPDATED_INVALID",
                    message="official USAspending last-updated date is invalid",
                    details={"last_updated": value},
                )
            )
            return None, None, None
        evaluation_date = observed_at.astimezone(NEW_YORK).date()
        age = (evaluation_date - source_date).days
        if age < 0:
            issues.append(
                USAspendingIssue(
                    issue_type="SOURCE_LAST_UPDATED_FUTURE",
                    message="official USAspending last-updated date is in the future",
                    details={"last_updated": source_date.isoformat()},
                )
            )
            return source_date, age, False
        current = age <= self.config.source_last_updated_max_age_calendar_days
        return source_date, age, current

    def _process_candidate(
        self,
        *,
        candidate: USAspendingAwardCandidate,
        mapping: USAspendingRecipientMapping,
        observed_at: datetime,
        checkpoint: dict[str, object],
        issues: list[USAspendingIssue],
        snapshots: list[ContextIndicatorSnapshot],
        cache_results: list[ContextStateUpdateResult],
        ledger_results: list[object],
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> "_ProcessOutcome":
        if not candidate.search_detail_lookup_id:
            issues.append(
                USAspendingIssue(
                    issue_type="AWARD_DETAIL_LOOKUP_ID_UNAVAILABLE",
                    message="search result did not return a verified award-detail lookup id",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=candidate.award_id,
                )
            )
            return _ProcessOutcome(False, candidate.award_id, False)
        try:
            detail = self.client.fetch_award_detail(candidate.search_detail_lookup_id)
        except Exception as exc:  # noqa: BLE001 - source adapter boundary.
            issues.append(
                USAspendingIssue(
                    issue_type="AWARD_ENRICHMENT_FAILED",
                    message="official USAspending award detail request failed",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=candidate.search_detail_lookup_id,
                    details={"error_type": type(exc).__name__},
                )
            )
            return _ProcessOutcome(False, None, False)
        canonical_award_id = _string_or_none(detail.get("generated_unique_award_id"))
        if canonical_award_id is None:
            issues.append(
                USAspendingIssue(
                    issue_type="MISSING_CANONICAL_AWARD_ID",
                    message="award detail did not return generated_unique_award_id",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=candidate.search_detail_lookup_id,
                )
            )
            return _ProcessOutcome(False, None, False)
        detail_uei = _detail_recipient_uei(detail)
        if detail_uei != mapping.recipient_uei:
            issues.append(
                USAspendingIssue(
                    issue_type="DETAIL_RECIPIENT_UEI_MISMATCH",
                    message="award detail recipient UEI did not match configured mapping",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=canonical_award_id,
                    details={"detail_recipient_uei": detail_uei},
                )
            )
            return _ProcessOutcome(False, canonical_award_id, False)
        try:
            funding = self.client.fetch_award_funding(
                canonical_award_id,
                limit=self.config.funding_limit_per_award,
            )
        except Exception as exc:  # noqa: BLE001 - source adapter boundary.
            issues.append(
                USAspendingIssue(
                    issue_type="AWARD_ENRICHMENT_FAILED",
                    message="official USAspending award funding request failed",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=canonical_award_id,
                    details={"error_type": type(exc).__name__},
                )
            )
            return _ProcessOutcome(False, canonical_award_id, False)
        return self._accept_event(
            candidate=candidate,
            mapping=mapping,
            detail=detail,
            funding=funding,
            observed_at=observed_at,
            checkpoint=checkpoint,
            issues=issues,
            snapshots=snapshots,
            cache_results=cache_results,
            ledger_results=ledger_results,
            write_questdb=write_questdb,
            questdb_required=questdb_required,
            run_id=run_id,
            session_id=session_id,
        )

    def _accept_event(
        self,
        *,
        candidate: USAspendingAwardCandidate,
        mapping: USAspendingRecipientMapping,
        detail: Mapping[str, object],
        funding: Mapping[str, object],
        observed_at: datetime,
        checkpoint: dict[str, object],
        issues: list[USAspendingIssue],
        snapshots: list[ContextIndicatorSnapshot],
        cache_results: list[ContextStateUpdateResult],
        ledger_results: list[object],
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> "_ProcessOutcome":
        event, funding_complete = _build_event(
            candidate=candidate,
            mapping=mapping,
            detail=detail,
            funding=funding,
            observed_at=observed_at,
            checkpoint=checkpoint,
            config=self.config,
        )
        if event is None:
            issues.append(
                USAspendingIssue(
                    issue_type="IDV_OR_UNSUPPORTED_AWARD_SUPPRESSED",
                    message="award detail did not describe a supported prime contract award",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=_string_or_none(detail.get("generated_unique_award_id")),
                )
            )
            return _ProcessOutcome(
                False,
                _string_or_none(detail.get("generated_unique_award_id")),
                False,
            )
        if not funding_complete:
            issues.append(
                USAspendingIssue(
                    issue_type="FUNDING_TRUNCATED",
                    message="bounded USAspending funding response has additional pages",
                    recipient_uei=mapping.recipient_uei,
                    ticker=mapping.ticker,
                    award_identifier=event.canonical_award_id,
                )
            )

        seen = _seen_events(checkpoint)
        existing_seen = seen.get(event.semantic_event_fingerprint)
        if existing_seen is not None:
            checkpoint_changed = isinstance(existing_seen, dict)
            _update_seen_last_observed(existing_seen, observed_at)
            update = self._rehydrate_cache_if_needed(existing_seen, observed_at)
            if update is not None:
                cache_results.append(update)
            return _ProcessOutcome(
                funding_complete,
                event.canonical_award_id,
                checkpoint_changed,
            )

        snapshot, cache_update = _snapshot_and_cache_entry(self.cache, event)
        cache_results.append(cache_update)
        durable_ledger_write_requested = write_questdb and self.config.writes_questdb_ledger
        durable_ledger_write_succeeded = False
        cache_state_is_writer_eligible = cache_update.status in {
            ContextStateUpdateStatus.WRITTEN,
            ContextStateUpdateStatus.REPLACED,
            ContextStateUpdateStatus.IGNORED_DUPLICATE,
        }
        if durable_ledger_write_requested:
            if self.ledger_writer is None:
                fallback_succeeded = self._append_emergency_ledger_fallback(
                    snapshot=snapshot,
                    event=event,
                    failure_code="QUESTDB_CONTEXT_INDICATOR_WRITER_UNAVAILABLE",
                    failure_type=None,
                    run_id=run_id,
                    session_id=session_id,
                    questdb_required=questdb_required,
                    issues=issues,
                    ledger_results=ledger_results,
                )
                issues.append(
                    USAspendingIssue(
                        issue_type="LEDGER_WRITER_UNAVAILABLE",
                        message="QuestDB context indicator write was requested but no writer was provided",
                        ticker=event.ticker,
                        award_identifier=event.canonical_award_id,
                        details={
                            "failure_code": "QUESTDB_CONTEXT_INDICATOR_WRITER_UNAVAILABLE",
                            "emergency_fallback_status": "WRITTEN"
                            if fallback_succeeded
                            else "FAILED",
                        },
                    )
                )
                if questdb_required:
                    suffix = "" if fallback_succeeded else "; emergency JSONL fallback failed"
                    raise USAspendingCollectorError(
                        f"QuestDB writes are required but no writer was provided{suffix}"
                    )
            elif not cache_state_is_writer_eligible:
                issues.append(
                    USAspendingIssue(
                        issue_type="LEDGER_WRITE_NOT_ELIGIBLE",
                        message="QuestDB context indicator write was not eligible for this cache update",
                        ticker=event.ticker,
                        award_identifier=event.canonical_award_id,
                        details={"cache_update_status": cache_update.status.value},
                    )
                )
                if questdb_required:
                    raise USAspendingCollectorError(
                        "QuestDB context indicator write was not eligible"
                    )
            else:
                try:
                    result = self.ledger_writer.write_context_indicator_snapshot(
                        snapshot,
                        run_id=run_id,
                        session_id=session_id,
                    )
                    durable_ledger_write_succeeded = True
                    if result is not None:
                        ledger_results.append(result)
                except Exception as exc:  # noqa: BLE001 - writer protocol boundary.
                    fallback_succeeded = self._append_emergency_ledger_fallback(
                        snapshot=snapshot,
                        event=event,
                        failure_code="QUESTDB_CONTEXT_INDICATOR_WRITE_FAILED",
                        failure_type=type(exc).__name__,
                        run_id=run_id,
                        session_id=session_id,
                        questdb_required=questdb_required,
                        issues=issues,
                        ledger_results=ledger_results,
                    )
                    issues.append(
                        USAspendingIssue(
                            issue_type="LEDGER_WRITE_FAILED",
                            message="QuestDB context indicator write failed",
                            ticker=event.ticker,
                            award_identifier=event.canonical_award_id,
                            details={
                                "error_type": type(exc).__name__,
                                "failure_code": "QUESTDB_CONTEXT_INDICATOR_WRITE_FAILED",
                                "emergency_fallback_status": "WRITTEN"
                                if fallback_succeeded
                                else "FAILED",
                            },
                        )
                    )
                    if questdb_required:
                        suffix = "" if fallback_succeeded else "; emergency JSONL fallback failed"
                        raise USAspendingCollectorError(
                            f"QuestDB context indicator write failed{suffix}"
                        ) from None
        checkpoint_event_is_safe_to_persist = durable_ledger_write_succeeded
        event_is_safe_for_result = (
            not durable_ledger_write_requested or durable_ledger_write_succeeded
        )
        if event_is_safe_for_result:
            snapshots.append(snapshot)
        if checkpoint_event_is_safe_to_persist:
            _record_event_in_checkpoint(checkpoint, event, observed_at)
        return _ProcessOutcome(
            funding_complete and event_is_safe_for_result,
            event.canonical_award_id,
            checkpoint_event_is_safe_to_persist,
        )

    def _append_emergency_ledger_fallback(
        self,
        *,
        snapshot: ContextIndicatorSnapshot,
        event: USAspendingAwardEvent,
        failure_code: str,
        failure_type: str | None,
        run_id: str | None,
        session_id: str | None,
        questdb_required: bool,
        issues: list[USAspendingIssue],
        ledger_results: list[object],
    ) -> bool:
        fallback = self.emergency_ledger_fallback
        if fallback is None:
            fallback = EmergencyJSONLLedgerFallback()
            self.emergency_ledger_fallback = fallback
        try:
            result = fallback.append_record(
                record_type="context_indicator_snapshot",
                target_table="context_indicator_snapshots",
                record_id=snapshot.context_indicator_id,
                event_time=snapshot.snapshot_time,
                source=snapshot.source,
                ticker_or_sector=snapshot.ticker_or_sector,
                primary_write_failure={
                    "failure_code": failure_code,
                    "failure_type": failure_type,
                    "target_table": "context_indicator_snapshots",
                },
                payload={
                    "context_indicator_snapshot": snapshot,
                    "write_request": {
                        "run_id": run_id,
                        "session_id": session_id,
                        "questdb_required": questdb_required,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - fallback protocol boundary.
            issues.append(
                USAspendingIssue(
                    issue_type="EMERGENCY_LEDGER_FALLBACK_FAILED",
                    message="emergency JSONL ledger fallback write failed",
                    ticker=event.ticker,
                    award_identifier=event.canonical_award_id,
                    details={
                        "error_type": type(exc).__name__,
                        "failure_code": "EMERGENCY_JSONL_FALLBACK_WRITE_FAILED",
                    },
                )
            )
            return False
        ledger_results.append(result)
        issues.append(
            USAspendingIssue(
                issue_type="EMERGENCY_LEDGER_FALLBACK_WRITTEN",
                message=(
                    "QuestDB context indicator write did not complete; event was "
                    "appended to emergency JSONL fallback"
                ),
                ticker=event.ticker,
                award_identifier=event.canonical_award_id,
                details={
                    "path": str(getattr(result, "path", "")),
                    "record_id": str(getattr(result, "record_id", snapshot.context_indicator_id)),
                    "bytes_written": getattr(result, "bytes_written", None),
                    "fallback_written_at": to_utc_iso(getattr(result, "written_at")),
                },
            )
        )
        return True

    def _rehydrate_cache_if_needed(
        self,
        seen_record: Mapping[str, object],
        observed_at: datetime,
    ) -> ContextStateUpdateResult | None:
        cache_entry_name = _string_or_none(seen_record.get("cache_entry_name"))
        ticker = _string_or_none(seen_record.get("ticker"))
        details = seen_record.get("details")
        value = _string_or_none(seen_record.get("event_classification"))
        event_first_observed_at = _parse_datetime_or_none(
            seen_record.get("event_first_observed_at")
        )
        source_event_time = _parse_datetime_or_none(seen_record.get("source_event_time"))
        if (
            cache_entry_name is None
            or ticker is None
            or value is None
            or event_first_observed_at is None
            or not isinstance(details, dict)
        ):
            return None
        if self.cache.get_ticker(ticker, cache_entry_name, now=observed_at) is not None:
            return None
        return self.cache.update(
            make_ticker_context_entry(
                ticker=ticker,
                name=cache_entry_name,
                value=value,
                updated_at=event_first_observed_at,
                severity="INFO",
                source=SOURCE_NAME,
                source_event_time=source_event_time,
                valid_until=None,
                details=dict(details),
            )
        )

    def _run_revision_rechecks(
        self,
        *,
        observed_at: datetime,
        active_by_uei: Mapping[str, USAspendingRecipientMapping],
        processed_canonical_ids: set[str],
        checkpoint: dict[str, object],
        issues: list[USAspendingIssue],
        snapshots: list[ContextIndicatorSnapshot],
        cache_results: list[ContextStateUpdateResult],
        ledger_results: list[object],
        write_questdb: bool,
        questdb_required: bool,
        run_id: str | None,
        session_id: str | None,
    ) -> "_RevisionRecheckOutcome":
        registry = _award_registry(checkpoint)
        cutoff = observed_at - timedelta(days=self.config.revision_recheck_calendar_days)
        eligible: list[Mapping[str, object]] = []
        for record in registry.values():
            canonical_id = _string_or_none(record.get("canonical_award_id"))
            recipient_uei = _string_or_none(record.get("recipient_uei"))
            first_observed = _parse_datetime_or_none(record.get("award_first_observed_at"))
            if (
                canonical_id is None
                or canonical_id in processed_canonical_ids
                or recipient_uei not in active_by_uei
                or first_observed is None
                or first_observed < cutoff
            ):
                continue
            eligible.append(record)
        eligible.sort(
            key=lambda item: (
                str(item.get("last_revision_checked_at") or ""),
                str(item.get("award_first_observed_at") or ""),
                str(item.get("canonical_award_id") or ""),
            )
        )
        complete = True
        checkpoint_changed = False
        if len(eligible) > self.config.max_revision_rechecks_per_run:
            complete = False
            issues.append(
                USAspendingIssue(
                    issue_type="REVISION_RECHECK_CAP_REACHED",
                    message="eligible revision rechecks exceeded configured cap",
                    details={"eligible_count": len(eligible)},
                )
            )
            eligible = eligible[: self.config.max_revision_rechecks_per_run]
        for record in eligible:
            canonical_id = str(record["canonical_award_id"])
            mapping = active_by_uei[str(record["recipient_uei"])]
            candidate = USAspendingAwardCandidate(
                search_detail_lookup_id=canonical_id,
                award_id=canonical_id,
                recipient_uei=mapping.recipient_uei,
                recipient_name=mapping.recipient_name,
                last_modified_date=None,
                base_obligation_date=None,
                award_amount=None,
                contract_award_type=None,
                naics=None,
                psc=None,
            )
            outcome = self._process_candidate(
                candidate=candidate,
                mapping=mapping,
                observed_at=observed_at,
                checkpoint=checkpoint,
                issues=issues,
                snapshots=snapshots,
                cache_results=cache_results,
                ledger_results=ledger_results,
                write_questdb=write_questdb,
                questdb_required=questdb_required,
                run_id=run_id,
                session_id=session_id,
            )
            complete = complete and outcome.coverage_complete
            checkpoint_changed = checkpoint_changed or outcome.checkpoint_changed
            if outcome.checkpoint_changed and outcome.canonical_award_id in registry:
                registry[outcome.canonical_award_id]["last_revision_checked_at"] = to_utc_iso(
                    observed_at
                )
                checkpoint_changed = True
        return _RevisionRecheckOutcome(complete, checkpoint_changed)

    def _persist_checkpoint(
        self,
        checkpoint: dict[str, object],
        *,
        observed_at: datetime,
        source_last_updated_date: date | None,
        advance_discovery_metadata: bool,
        prune: bool,
    ) -> None:
        if advance_discovery_metadata:
            checkpoint["last_successful_collection_at"] = to_utc_iso(observed_at)
            checkpoint["source_last_updated_date"] = (
                None if source_last_updated_date is None else source_last_updated_date.isoformat()
            )
        if prune:
            _prune_checkpoint(
                checkpoint,
                observed_at=observed_at,
                award_registry_retention_days=self.config.award_registry_retention_calendar_days,
            )
        self.checkpoint_store.write(checkpoint)


@dataclass(frozen=True)
class _ProcessOutcome:
    coverage_complete: bool
    canonical_award_id: str | None
    checkpoint_changed: bool


@dataclass(frozen=True)
class _RevisionRecheckOutcome:
    coverage_complete: bool
    checkpoint_changed: bool


def load_recipient_mappings(path: str | Path) -> tuple[USAspendingRecipientMapping, ...]:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = repo_root() / resolved
    if not resolved.is_file():
        raise USAspendingCollectorError(f"recipient map file not found: {resolved}")
    try:
        loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise USAspendingCollectorError("recipient map YAML is invalid") from exc
    if not isinstance(loaded, Mapping):
        raise USAspendingCollectorError("recipient map must be a mapping")
    if loaded.get("mapping_version") != MAPPING_VERSION:
        raise USAspendingCollectorError(f"mapping_version must equal {MAPPING_VERSION}")
    recipients = loaded.get("recipients")
    if not isinstance(recipients, list):
        raise USAspendingCollectorError("recipients must be a list")
    mappings: list[USAspendingRecipientMapping] = []
    seen_uei: set[str] = set()
    active_uei: set[str] = set()
    for raw in recipients:
        if not isinstance(raw, Mapping):
            raise USAspendingCollectorError("each recipient mapping must be a mapping")
        mapping = USAspendingRecipientMapping(**dict(raw))
        if mapping.recipient_uei in seen_uei:
            raise USAspendingCollectorError("duplicate recipient_uei")
        if mapping.active and mapping.recipient_uei in active_uei:
            raise USAspendingCollectorError("duplicate active recipient_uei mapping")
        seen_uei.add(mapping.recipient_uei)
        if mapping.active:
            active_uei.add(mapping.recipient_uei)
        mappings.append(mapping)
    return tuple(mappings)


def discovery_window(observed_at: datetime, lookback_days: int) -> tuple[str, str]:
    checked = ensure_timezone_aware_utc(observed_at)
    lookback = _bounded_int(
        lookback_days,
        "discovery_last_modified_lookback_calendar_days",
        minimum=1,
        maximum=31,
    )
    end = checked.astimezone(NEW_YORK).date()
    start = end - timedelta(days=lookback - 1)
    return start.isoformat(), end.isoformat()


def cache_entry_name(ticker: str, canonical_award_id: str) -> str:
    return (
        f"usaspending:contract_award:"
        f"{_required_string(ticker, 'ticker').upper()}:"
        f"{_required_string(canonical_award_id, 'canonical_award_id')}"
    )


def _response_json(response: Any, url: str) -> Mapping[str, object]:
    status = getattr(response, "status_code", None)
    if status != 200:
        raise USAspendingCollectorError(f"official USAspending endpoint returned HTTP {status}")
    try:
        payload = response.json()
    except ValueError:
        raise USAspendingCollectorError("official USAspending endpoint returned invalid JSON") from None
    if not isinstance(payload, Mapping):
        raise USAspendingCollectorError(f"official USAspending endpoint returned non-object JSON: {url}")
    return payload


def _candidates_from_search(
    payload: Mapping[str, object],
    mapping: USAspendingRecipientMapping,
) -> tuple[tuple[USAspendingAwardCandidate, ...], int]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise USAspendingCollectorError("USAspending award search response has no results list")
    candidates: list[USAspendingAwardCandidate] = []
    rejected = 0
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        recipient_uei = _string_or_none(row.get("Recipient UEI"))
        if recipient_uei != mapping.recipient_uei:
            rejected += 1
            continue
        lookup_id = _string_or_none(row.get("generated_internal_id"))
        if lookup_id is None:
            internal_id = row.get("internal_id")
            lookup_id = str(internal_id) if isinstance(internal_id, int) else None
        if lookup_id is None:
            candidates.append(
                USAspendingAwardCandidate(
                    search_detail_lookup_id="",
                    award_id=_string_or_none(row.get("Award ID")),
                    recipient_uei=recipient_uei,
                    recipient_name=_string_or_none(row.get("Recipient Name")),
                    last_modified_date=_date_text_or_none(row.get("Last Modified Date")),
                    base_obligation_date=_date_text_or_none(row.get("Base Obligation Date")),
                    award_amount=_number_or_none(row.get("Award Amount")),
                    contract_award_type=_string_or_none(row.get("Contract Award Type")),
                    naics=_string_or_none(row.get("NAICS")),
                    psc=_string_or_none(row.get("PSC")),
                    raw=row,
                )
            )
            continue
        candidates.append(
            USAspendingAwardCandidate(
                search_detail_lookup_id=lookup_id,
                award_id=_string_or_none(row.get("Award ID")),
                recipient_uei=recipient_uei,
                recipient_name=_string_or_none(row.get("Recipient Name")),
                last_modified_date=_date_text_or_none(row.get("Last Modified Date")),
                base_obligation_date=_date_text_or_none(row.get("Base Obligation Date")),
                award_amount=_number_or_none(row.get("Award Amount")),
                contract_award_type=_string_or_none(row.get("Contract Award Type")),
                naics=_string_or_none(row.get("NAICS")),
                psc=_string_or_none(row.get("PSC")),
                raw=row,
            )
        )
    return tuple(candidates), rejected


def _candidate_sort_key(candidate: USAspendingAwardCandidate) -> tuple[str, str, str, str]:
    return (
        "" if candidate.last_modified_date is None else candidate.last_modified_date,
        "" if candidate.base_obligation_date is None else candidate.base_obligation_date,
        candidate.search_detail_lookup_id,
        "" if candidate.award_id is None else candidate.award_id,
    )


def _build_event(
    *,
    candidate: USAspendingAwardCandidate,
    mapping: USAspendingRecipientMapping,
    detail: Mapping[str, object],
    funding: Mapping[str, object],
    observed_at: datetime,
    checkpoint: Mapping[str, object],
    config: USAspendingConfig,
) -> tuple[USAspendingAwardEvent | None, bool]:
    lookup_id = _required_string(
        candidate.search_detail_lookup_id,
        "search_detail_lookup_id",
    )
    canonical_award_id = _required_string(
        detail.get("generated_unique_award_id"),
        "generated_unique_award_id",
    )
    award_type = _string_or_none(detail.get("type"))
    category = _string_or_none(detail.get("category"))
    if category not in {None, "contract"} or award_type not in CONTRACT_AWARD_TYPE_CODES:
        return None, True

    funding_evidence, funding_has_next, funding_complete, funding_fingerprint = (
        _funding_evidence(funding)
    )
    source_business_date = _source_business_date(detail)
    source_event_time = (
        None
        if source_business_date is None
        else datetime.combine(source_business_date, time.min, UTC)
    )
    registry_record = _award_registry(checkpoint).get(canonical_award_id)
    stored_classification = _stored_or_new_classification(
        registry_record,
        observed_at=observed_at,
        source_business_date=source_business_date,
        config=config,
    )
    semantic_identity = _semantic_identity(
        canonical_award_id=canonical_award_id,
        mapping=mapping,
        classification=stored_classification,
        candidate=candidate,
        detail=detail,
        source_business_date=source_business_date,
        funding_page_complete=funding_complete,
        funding_records_returned_count=len(funding_evidence),
        funding_evidence_fingerprint=funding_fingerprint,
    )
    digest = _digest_payload(semantic_identity)
    event_fingerprint = f"usaspending_award_event_{digest}"
    if (
        registry_record is not None
        and event_fingerprint != registry_record.get("latest_event_fingerprint")
    ):
        stored_classification = USAspendingEventClassification.AWARD_REVISION_DISCOVERED
        semantic_identity = _semantic_identity(
            canonical_award_id=canonical_award_id,
            mapping=mapping,
            classification=stored_classification,
            candidate=candidate,
            detail=detail,
            source_business_date=source_business_date,
            funding_page_complete=funding_complete,
            funding_records_returned_count=len(funding_evidence),
            funding_evidence_fingerprint=funding_fingerprint,
        )
        digest = _digest_payload(semantic_identity)
        event_fingerprint = f"usaspending_award_event_{digest}"
    context_indicator_id = f"context_indicator_{digest}"

    seen_record = _seen_events(checkpoint).get(event_fingerprint)
    if seen_record is not None:
        first_observed = _parse_datetime_or_none(seen_record.get("event_first_observed_at"))
        research_horizon_ends_at = _parse_datetime_or_none(
            seen_record.get("research_horizon_ends_at")
        )
    else:
        first_observed = None
        research_horizon_ends_at = None
    if first_observed is None:
        first_observed = observed_at
    if research_horizon_ends_at is None:
        research_horizon_ends_at = first_observed + timedelta(seconds=RESEARCH_HORIZON_SECONDS)

    return (
        USAspendingAwardEvent(
            canonical_award_id=canonical_award_id,
            generated_unique_award_id=canonical_award_id,
            search_detail_lookup_id=lookup_id,
            ticker=mapping.ticker,
            issuer_name=mapping.issuer_name,
            recipient_uei=mapping.recipient_uei,
            recipient_name=_detail_recipient_name(detail) or candidate.recipient_name,
            mapping_version=mapping.mapping_version,
            mapping_confidence=mapping.mapping_confidence,
            event_classification=stored_classification,
            context_indicator_id=context_indicator_id,
            semantic_event_fingerprint=event_fingerprint,
            semantic_event_identity=semantic_identity,
            cache_entry_name=cache_entry_name(mapping.ticker, canonical_award_id),
            source_business_date=source_business_date,
            source_event_time=source_event_time,
            collector_observed_at=observed_at,
            event_first_observed_at=first_observed,
            research_horizon_ends_at=research_horizon_ends_at,
            award_type=award_type,
            award_type_description=_string_or_none(detail.get("type_description")),
            award_category=category,
            award_description=_string_or_none(detail.get("description")),
            date_signed=_date_text_or_none(detail.get("date_signed")),
            action_date=_date_text_or_none(detail.get("action_date")),
            award_last_modified_date=_award_last_modified_date(detail, candidate),
            period_of_performance_start=_date_text_or_none(
                detail.get("period_of_performance_start")
            )
            or _date_text_or_none(detail.get("period_of_performance_start_date")),
            period_of_performance_end=_date_text_or_none(
                detail.get("period_of_performance_end")
            )
            or _date_text_or_none(detail.get("period_of_performance_current_end_date")),
            total_obligation_usd=_number_or_none(detail.get("total_obligation")),
            base_exercised_options_usd=_number_or_none(
                detail.get("base_exercised_options")
            ),
            base_and_all_options_usd=_number_or_none(detail.get("base_and_all_options")),
            awarding_agency_name=_agency_field(detail, "awarding_agency", "name"),
            awarding_agency_code=_agency_field(detail, "awarding_agency", "code"),
            funding_agency_name=_agency_field(detail, "funding_agency", "name"),
            funding_agency_code=_agency_field(detail, "funding_agency", "code"),
            naics_code=_latest_contract_data_field(detail, "naics") or candidate.naics,
            naics_description=_latest_contract_data_field(detail, "naics_description"),
            psc_code=_latest_contract_data_field(detail, "product_or_service_code")
            or candidate.psc,
            psc_description=_latest_contract_data_field(
                detail, "product_or_service_description"
            ),
            funding_records_returned_count=len(funding_evidence),
            funding_has_next_page=funding_has_next,
            funding_page_complete=funding_complete,
            funding_record_evidence=tuple(funding_evidence),
            funding_evidence_fingerprint=funding_fingerprint,
        ),
        funding_complete,
    )


def _stored_or_new_classification(
    registry_record: Mapping[str, object] | None,
    *,
    observed_at: datetime,
    source_business_date: date | None,
    config: USAspendingConfig,
) -> USAspendingEventClassification:
    if registry_record is not None:
        stored = _string_or_none(registry_record.get("event_classification"))
        if stored in USAspendingEventClassification._value2member_map_:
            return USAspendingEventClassification(stored)
    if source_business_date is None:
        return USAspendingEventClassification.LATE_OR_BACKFILL_DISCOVERY
    observed_date = observed_at.astimezone(NEW_YORK).date()
    if (observed_date - source_business_date).days <= config.late_discovery_calendar_days:
        return USAspendingEventClassification.NEW_AWARD_DISCOVERED
    return USAspendingEventClassification.LATE_OR_BACKFILL_DISCOVERY


def _semantic_identity(
    *,
    canonical_award_id: str,
    mapping: USAspendingRecipientMapping,
    classification: USAspendingEventClassification,
    candidate: USAspendingAwardCandidate,
    detail: Mapping[str, object],
    source_business_date: date | None,
    funding_page_complete: bool,
    funding_records_returned_count: int,
    funding_evidence_fingerprint: str,
) -> dict[str, object]:
    return {
        "source": SOURCE_NAME,
        "indicator_name": INDICATOR_NAME,
        "ticker": mapping.ticker,
        "recipient_uei": mapping.recipient_uei,
        "generated_unique_award_id": canonical_award_id,
        "event_classification": classification.value,
        "mapping_version": mapping.mapping_version,
        "award_type": _string_or_none(detail.get("type")),
        "source_business_date": None
        if source_business_date is None
        else source_business_date.isoformat(),
        "total_obligation_usd": _canonical_number(detail.get("total_obligation")),
        "base_exercised_options_usd": _canonical_number(
            detail.get("base_exercised_options")
        ),
        "base_and_all_options_usd": _canonical_number(
            detail.get("base_and_all_options")
        ),
        "award_last_modified_date": _award_last_modified_date(detail, candidate),
        "funding_page_complete": funding_page_complete,
        "funding_records_returned_count": funding_records_returned_count,
        "funding_evidence_fingerprint": funding_evidence_fingerprint,
    }


def _snapshot_and_cache_entry(
    cache: ContextStateCache,
    event: USAspendingAwardEvent,
) -> tuple[ContextIndicatorSnapshot, ContextStateUpdateResult]:
    details = _event_details(event)
    snapshot = ContextIndicatorSnapshot(
        snapshot_time=event.event_first_observed_at,
        source=SOURCE_NAME,
        ticker_or_sector=event.ticker,
        indicator_name=INDICATOR_NAME,
        value=event.event_classification.value,
        context_indicator_id=event.context_indicator_id,
        window=WINDOW_NAME,
        units="category",
        freshness_seconds=None,
        source_event_time=event.source_event_time,
        details=details,
    )
    update = cache.update(
        make_ticker_context_entry(
            ticker=event.ticker,
            name=event.cache_entry_name,
            value=event.event_classification.value,
            updated_at=event.event_first_observed_at,
            severity="INFO",
            source=SOURCE_NAME,
            source_event_time=event.source_event_time,
            valid_until=None,
            details=details,
        )
    )
    return snapshot, update


def _event_details(event: USAspendingAwardEvent) -> dict[str, object]:
    return {
        "source": SOURCE_NAME,
        "canonical_award_id": event.canonical_award_id,
        "generated_unique_award_id": event.generated_unique_award_id,
        "search_detail_lookup_id": event.search_detail_lookup_id,
        "context_indicator_id": event.context_indicator_id,
        "semantic_event_fingerprint": event.semantic_event_fingerprint,
        "cache_entry_name": event.cache_entry_name,
        "ticker": event.ticker,
        "issuer_name": event.issuer_name,
        "recipient_uei": event.recipient_uei,
        "recipient_name": event.recipient_name,
        "mapping_version": event.mapping_version,
        "mapping_confidence": event.mapping_confidence,
        "event_classification": event.event_classification.value,
        "award_type": event.award_type,
        "award_type_description": event.award_type_description,
        "award_category": event.award_category,
        "award_description": event.award_description,
        "date_signed": event.date_signed,
        "action_date": event.action_date,
        "source_business_date": None
        if event.source_business_date is None
        else event.source_business_date.isoformat(),
        "source_event_time_basis": "source_business_date_utc_midnight_convention",
        "award_last_modified_date": event.award_last_modified_date,
        "period_of_performance_start": event.period_of_performance_start,
        "period_of_performance_end": event.period_of_performance_end,
        "total_obligation_usd": event.total_obligation_usd,
        "base_exercised_options_usd": event.base_exercised_options_usd,
        "base_and_all_options_usd": event.base_and_all_options_usd,
        "amount_semantics": "award_total_not_incremental",
        "base_and_all_options_semantics": "not_funded_obligation",
        "funding_transaction_semantics": "bounded_records_not_current_transaction_signal",
        "awarding_agency_name": event.awarding_agency_name,
        "awarding_agency_code": event.awarding_agency_code,
        "funding_agency_name": event.funding_agency_name,
        "funding_agency_code": event.funding_agency_code,
        "naics_code": event.naics_code,
        "naics_description": event.naics_description,
        "psc_code": event.psc_code,
        "psc_description": event.psc_description,
        "funding_records_returned_count": event.funding_records_returned_count,
        "funding_has_next_page": event.funding_has_next_page,
        "funding_page_complete": event.funding_page_complete,
        "funding_record_evidence": list(event.funding_record_evidence),
        "funding_evidence_fingerprint": event.funding_evidence_fingerprint,
        "collector_observed_at": to_utc_iso(event.collector_observed_at),
        "event_first_observed_at": to_utc_iso(event.event_first_observed_at),
        "research_horizon_ends_at": to_utc_iso(event.research_horizon_ends_at),
        "research_horizon_seconds": RESEARCH_HORIZON_SECONDS,
        "cache_retention_policy": "bounded_process_memory_only",
        "availability_basis": "collector_observed",
        "historical_action_date_asof_eligible": False,
        "forward_outcome_anchor_time": to_utc_iso(event.collector_observed_at),
        "forward_outcome_study_eligible": True,
        "source_last_updated_is_precise_publication_time": False,
        "recipient_discovery_method": RECIPIENT_DISCOVERY_METHOD,
        "recipient_search_is_complete_coverage": False,
    }


def _record_event_in_checkpoint(
    checkpoint: dict[str, object],
    event: USAspendingAwardEvent,
    observed_at: datetime,
) -> None:
    seen = _seen_events(checkpoint)
    details = _event_details(event)
    seen[event.semantic_event_fingerprint] = {
        "event_fingerprint": event.semantic_event_fingerprint,
        "event_first_observed_at": to_utc_iso(event.event_first_observed_at),
        "last_observed_at": to_utc_iso(observed_at),
        "award_identifier": event.canonical_award_id,
        "canonical_award_id": event.canonical_award_id,
        "generated_unique_award_id": event.generated_unique_award_id,
        "ticker": event.ticker,
        "recipient_uei": event.recipient_uei,
        "event_classification": event.event_classification.value,
        "context_indicator_id": event.context_indicator_id,
        "cache_entry_name": event.cache_entry_name,
        "source_event_time": None
        if event.source_event_time is None
        else to_utc_iso(event.source_event_time),
        "research_horizon_ends_at": to_utc_iso(event.research_horizon_ends_at),
        "semantic_event_identity": dict(event.semantic_event_identity),
        "details": details,
    }
    registry = _award_registry(checkpoint)
    previous = registry.get(event.canonical_award_id, {})
    award_first_observed = previous.get("award_first_observed_at") or to_utc_iso(
        event.event_first_observed_at
    )
    registry[event.canonical_award_id] = {
        "canonical_award_id": event.canonical_award_id,
        "generated_unique_award_id": event.generated_unique_award_id,
        "latest_event_fingerprint": event.semantic_event_fingerprint,
        "latest_semantic_identity": dict(event.semantic_event_identity),
        "recipient_uei": event.recipient_uei,
        "ticker": event.ticker,
        "mapping_version": event.mapping_version,
        "award_first_observed_at": award_first_observed,
        "last_observed_at": to_utc_iso(observed_at),
        "last_revision_checked_at": previous.get("last_revision_checked_at"),
        "event_classification": event.event_classification.value,
    }


def _update_seen_last_observed(record: Mapping[str, object], observed_at: datetime) -> None:
    if isinstance(record, dict):
        record["last_observed_at"] = to_utc_iso(observed_at)


def _funding_evidence(
    funding: Mapping[str, object],
) -> tuple[list[dict[str, object]], bool, bool, str]:
    raw_results = funding.get("results")
    if not isinstance(raw_results, list):
        raise USAspendingCollectorError("USAspending funding response has no results list")
    metadata = _mapping_or_empty(funding.get("page_metadata"))
    has_next = metadata.get("hasNext") is True
    evidence: list[dict[str, object]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        record: dict[str, object] = {}
        for field_name in FUNDING_EVIDENCE_FIELDS:
            value = raw.get(field_name)
            safe_value = _json_safe_value_or_none(value)
            if safe_value is not None or value is None:
                record[field_name] = safe_value
        evidence.append(record)
    complete = not has_next
    fingerprint_payload = {
        "funding_page_complete": complete,
        "funding_has_next_page": has_next,
        "funding_records_returned_count": len(evidence),
        "funding_record_evidence": sorted(
            evidence,
            key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True),
        ),
    }
    return evidence, has_next, complete, f"funding_evidence_{_digest_payload(fingerprint_payload)}"


def _source_business_date(detail: Mapping[str, object]) -> date | None:
    action_date = _date_text_or_none(detail.get("action_date"))
    if action_date is not None:
        return _parse_date(action_date)
    signed = _date_text_or_none(detail.get("date_signed"))
    if signed is not None:
        return _parse_date(signed)
    return None


def _award_last_modified_date(
    detail: Mapping[str, object],
    candidate: USAspendingAwardCandidate | None,
) -> str | None:
    period_of_performance = _mapping_or_empty(detail.get("period_of_performance"))
    return (
        _date_text_or_none(detail.get("last_modified_date"))
        or _date_text_or_none(detail.get("last_modified"))
        or _date_text_or_none(period_of_performance.get("last_modified_date"))
        or _date_text_or_none(period_of_performance.get("last_modified"))
        or (None if candidate is None else candidate.last_modified_date)
    )


def _detail_recipient_uei(detail: Mapping[str, object]) -> str | None:
    recipient = _mapping_or_empty(detail.get("recipient"))
    return (
        _string_or_none(recipient.get("recipient_uei"))
        or _string_or_none(recipient.get("uei"))
        or _string_or_none(detail.get("recipient_uei"))
    )


def _detail_recipient_name(detail: Mapping[str, object]) -> str | None:
    recipient = _mapping_or_empty(detail.get("recipient"))
    return (
        _string_or_none(recipient.get("recipient_name"))
        or _string_or_none(recipient.get("name"))
        or _string_or_none(detail.get("recipient_name"))
    )


def _agency_field(detail: Mapping[str, object], object_name: str, field_name: str) -> str | None:
    agency = _mapping_or_empty(detail.get(object_name))
    subtier = _mapping_or_empty(agency.get("subtier_agency"))
    toptier = _mapping_or_empty(agency.get("toptier_agency"))
    direct = (
        _string_or_none(agency.get(field_name))
        or _string_or_none(agency.get(f"agency_{field_name}"))
        or _string_or_none(detail.get(f"{object_name}_{field_name}"))
    )
    if field_name == "name":
        return (
            _string_or_none(subtier.get("name"))
            or _string_or_none(toptier.get("name"))
            or direct
        )
    if field_name == "code":
        return (
            _string_or_none(subtier.get("subtier_code"))
            or _string_or_none(subtier.get("code"))
            or _string_or_none(toptier.get("toptier_code"))
            or _string_or_none(toptier.get("code"))
            or direct
        )
    return direct


def _latest_contract_data_field(detail: Mapping[str, object], field_name: str) -> str | None:
    latest = _mapping_or_empty(detail.get("latest_transaction_contract_data"))
    return _string_or_none(latest.get(field_name))


def _empty_checkpoint() -> dict[str, object]:
    return {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "source_last_updated_date": None,
        "last_successful_collection_at": None,
        "seen_event_fingerprints": {},
        "award_registry": {},
    }


def _normalize_checkpoint(checkpoint: dict[str, object]) -> dict[str, object]:
    normalized = _empty_checkpoint()
    normalized.update(checkpoint)
    if normalized.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        normalized["checkpoint_schema_version"] = CHECKPOINT_SCHEMA_VERSION
    if not isinstance(normalized.get("seen_event_fingerprints"), dict):
        normalized["seen_event_fingerprints"] = {}
    if not isinstance(normalized.get("award_registry"), dict):
        normalized["award_registry"] = {}
    return normalized


def _seen_events(checkpoint: Mapping[str, object]) -> dict[str, dict[str, object]]:
    seen = checkpoint.get("seen_event_fingerprints")
    if not isinstance(seen, dict):
        raise USAspendingCollectorError("checkpoint seen_event_fingerprints must be a mapping")
    return seen  # type: ignore[return-value]


def _award_registry(checkpoint: Mapping[str, object]) -> dict[str, dict[str, object]]:
    registry = checkpoint.get("award_registry")
    if not isinstance(registry, dict):
        raise USAspendingCollectorError("checkpoint award_registry must be a mapping")
    return registry  # type: ignore[return-value]


def _prune_checkpoint(
    checkpoint: dict[str, object],
    *,
    observed_at: datetime,
    award_registry_retention_days: int,
) -> None:
    seen = _seen_events(checkpoint)
    seen_cutoff = observed_at - timedelta(days=SEEN_EVENT_RETENTION_CALENDAR_DAYS)
    for key, record in list(seen.items()):
        first_observed = _parse_datetime_or_none(record.get("event_first_observed_at"))
        if first_observed is not None and first_observed < seen_cutoff:
            del seen[key]
    registry = _award_registry(checkpoint)
    registry_cutoff = observed_at - timedelta(days=award_registry_retention_days)
    for key, record in list(registry.items()):
        first_observed = _parse_datetime_or_none(record.get("award_first_observed_at"))
        if first_observed is not None and first_observed < registry_cutoff:
            del registry[key]


def _status_from_run(
    *,
    failed_all_paths: bool,
    source_current: bool | None,
    coverage_complete: bool,
    issues: Sequence[USAspendingIssue],
) -> USAspendingCollectionStatus:
    if failed_all_paths:
        return USAspendingCollectionStatus.FAILED
    if issues or not coverage_complete or source_current is None:
        return USAspendingCollectionStatus.PARTIAL
    if source_current is False:
        return USAspendingCollectionStatus.STALE
    return USAspendingCollectionStatus.SUCCESS


def _set_bounded_int(instance: object, field_name: str, minimum: int, maximum: int) -> None:
    object.__setattr__(
        instance,
        field_name,
        _bounded_int(getattr(instance, field_name), field_name, minimum=minimum, maximum=maximum),
    )


def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise USAspendingCollectorError(f"{key} must be a mapping")
    return value


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _json_safe_value_or_none(value: object) -> object | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): safe
            for key, child in value.items()
            if (safe := _json_safe_value_or_none(child)) is not None or child is None
        }
    if isinstance(value, list):
        return [_json_safe_value_or_none(child) for child in value]
    return None


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise USAspendingCollectorError(f"{field_name} must be a non-empty string")
    return value.strip()


def _relative_path_string(value: object, field_name: str) -> str:
    text = _required_string(value, field_name)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise USAspendingCollectorError(f"{field_name} must be a relative path")
    return text.replace("\\", "/")


def _string_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _date_text_or_none(value: object) -> str | None:
    text = _string_or_none(value)
    if text is None:
        return None
    candidate = text[:10]
    try:
        return _parse_date(candidate).isoformat()
    except USAspendingCollectorError:
        return None


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise USAspendingCollectorError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        raise USAspendingCollectorError("date must be YYYY-MM-DD") from None
    return parsed


def parse_source_last_updated_date(value: object) -> date:
    if not isinstance(value, str):
        raise USAspendingCollectorError("last_updated date must be YYYY-MM-DD or MM/DD/YYYY")
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return _parse_date(text)
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
        try:
            return datetime.strptime(text, "%m/%d/%Y").date()
        except ValueError:
            raise USAspendingCollectorError(
                "last_updated date must be YYYY-MM-DD or MM/DD/YYYY"
            ) from None
    raise USAspendingCollectorError("last_updated date must be YYYY-MM-DD or MM/DD/YYYY")


def _parse_datetime_or_none(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return ensure_timezone_aware_utc(datetime.fromisoformat(text))
    except (TypeError, ValueError):
        return None


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _canonical_number(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise USAspendingCollectorError(f"{field_name} must be positive and finite")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise USAspendingCollectorError(f"{field_name} must be positive and finite") from None
    if not math.isfinite(number) or number <= 0:
        raise USAspendingCollectorError(f"{field_name} must be positive and finite")
    return number


def _bounded_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise USAspendingCollectorError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise USAspendingCollectorError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def _digest_payload(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "AWARD_DETAIL_URL_TEMPLATE",
    "AWARD_FUNDING_URL",
    "CONTRACT_AWARD_TYPE_CODES",
    "INDICATOR_NAME",
    "LAST_UPDATED_URL",
    "MAPPING_VERSION",
    "SEARCH_FIELDS",
    "SEARCH_URL",
    "SOURCE_NAME",
    "USAspendingAwardCandidate",
    "USAspendingAwardEvent",
    "USAspendingCheckpointStore",
    "USAspendingClient",
    "USAspendingCollectionResult",
    "USAspendingCollectionStatus",
    "USAspendingCollector",
    "USAspendingCollectorBusyError",
    "USAspendingCollectorError",
    "USAspendingConfig",
    "USAspendingEventClassification",
    "USAspendingHTTPClient",
    "USAspendingIssue",
    "USAspendingLedgerWriter",
    "USAspendingRecipientMapping",
    "cache_entry_name",
    "discovery_window",
    "load_recipient_mappings",
]
