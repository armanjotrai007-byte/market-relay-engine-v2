"""Bounded official HTTP source collectors for external research events.

The collectors in this module only receive, validate, archive, and normalize
source documents.  They deliberately do not invoke Gemini, QuestDB, risk, or
execution code.  The immutable archive is the restart-safe hand-off to later
classification and research preparation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import re
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urljoin, urlsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
import requests

from market_relay_engine.common.time import (
    ensure_timezone_aware_utc,
    parse_utc_iso,
    to_utc_iso,
    utc_now,
)
from market_relay_engine.context.external_event_archive import (
    CoverageInterval,
    CoverageStatus,
    ExternalEventArchive,
    ExternalSourceRevision,
    LifecycleState,
    SourceCoverage,
    source_revision_id,
)
from market_relay_engine.context.external_normalization import (
    HTML_NORMALIZER_VERSION,
    PDF_EXTRACTOR_VERSION,
    ExternalNormalizationError,
    canonicalize_url,
    extract_article_html,
    extract_pdf_text,
    normalize_html_fragment,
)


LMT_SOURCE = "lockheed_martin_rss"
PLTR_SOURCE = "palantir_ir"
EARNINGS_SOURCE = "company_earnings"
LMT_BOOTSTRAP_HASH_VERSION = "rss_bootstrap_discovery_v1"
PLTR_BOOTSTRAP_HASH_VERSION = "pltr_bootstrap_discovery_v1"
EARNINGS_BOOTSTRAP_HASH_VERSION = "earnings_bootstrap_discovery_v1"


class ExternalSourceError(RuntimeError):
    """Raised when an official source cannot be proven safe to consume."""


@dataclass(frozen=True, kw_only=True)
class ExternalHTTPSettings:
    user_agent: str
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 8.0
    max_response_bytes: int = 5_000_000
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ExternalSourceError("HTTP user agent is required")
        if self.timeout_seconds <= 0:
            raise ExternalSourceError("HTTP timeout must be positive")
        if self.max_retries < 0:
            raise ExternalSourceError("HTTP max_retries must be non-negative")
        if self.retry_base_delay_seconds < 0 or self.retry_max_delay_seconds < 0:
            raise ExternalSourceError("HTTP retry delays must be non-negative")
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ExternalSourceError("HTTP base retry delay exceeds maximum")
        if self.max_response_bytes < 1:
            raise ExternalSourceError("HTTP response limit must be positive")
        if (
            isinstance(self.max_redirects, bool)
            or not isinstance(self.max_redirects, int)
            or self.max_redirects < 0
        ):
            raise ExternalSourceError("HTTP max_redirects must be non-negative")


@dataclass(frozen=True, kw_only=True)
class HTTPFetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    observed_at: datetime

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304

    @property
    def content_type(self) -> str | None:
        value = _header(self.headers, "Content-Type")
        return None if value is None else value.split(";", 1)[0].strip().lower()


class BoundedHTTPClient:
    """Sequential HTTP GET client with bounded retries and response reads."""

    def __init__(
        self,
        settings: ExternalHTTPSettings,
        *,
        session: requests.Session | None = None,
        now: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._session = session or requests.Session()
        self._now = now
        self._sleeper = sleeper
        self._deadline_monotonic = deadline_monotonic
        self._monotonic = monotonic

    def get(
        self,
        url: str,
        *,
        conditional_headers: Mapping[str, str] | None = None,
        allowed_domains: Iterable[str],
        accepted_content_types: Iterable[str] = (),
    ) -> HTTPFetchResult:
        requested_url = _canonical_source_url(url)
        allowed = {value.lower().rstrip(".") for value in allowed_domains}
        _require_safe_source_url(requested_url, allowed)
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if conditional_headers:
            for name in ("If-None-Match", "If-Modified-Since"):
                value = _header(conditional_headers, name)
                if value:
                    headers[name] = value

        for attempt in range(self.settings.max_retries + 1):
            response: Any | None = None
            try:
                response = self._open_following_safe_redirects(
                    requested_url,
                    headers=headers,
                    allowed_domains=allowed,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.settings.max_retries:
                    raise ExternalSourceError("external HTTP transport failed") from exc
                self._sleep_retry(self._retry_delay(attempt))
                continue

            try:
                status = int(response.status_code)
                final_url = _canonical_source_url(
                    str(getattr(response, "url", requested_url) or requested_url)
                )
                _require_safe_source_url(final_url, allowed)
                response_headers = {
                    str(key): str(value) for key, value in response.headers.items()
                }

                if status == 304:
                    return HTTPFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        status_code=status,
                        headers=response_headers,
                        content=b"",
                        observed_at=ensure_timezone_aware_utc(self._now()),
                    )
                if status == 429:
                    if attempt >= self.settings.max_retries:
                        raise ExternalSourceError("external source returned HTTP 429")
                    retry_after = _retry_after_seconds(
                        response_headers, now=self._now()
                    )
                    self._sleep_retry(
                        self._retry_delay(attempt)
                        if retry_after is None
                        else retry_after
                    )
                    continue
                if status in {500, 502, 503, 504}:
                    if attempt >= self.settings.max_retries:
                        raise ExternalSourceError(
                            f"external source returned HTTP {status}"
                        )
                    self._sleep_retry(self._retry_delay(attempt))
                    continue
                if status >= 400:
                    raise ExternalSourceError(
                        f"external source returned HTTP {status}"
                    )

                content = self._read_bounded(response, response_headers)
                accepted = tuple(value.lower() for value in accepted_content_types)
                content_type = _header(response_headers, "Content-Type")
                media_type = (
                    ""
                    if content_type is None
                    else content_type.split(";", 1)[0].strip().lower()
                )
                if accepted and not any(
                    media_type == value or media_type.startswith(value)
                    for value in accepted
                ):
                    raise ExternalSourceError(
                        "external source returned an unexpected content type"
                    )
                return HTTPFetchResult(
                    requested_url=requested_url,
                    final_url=final_url,
                    status_code=status,
                    headers=response_headers,
                    content=content,
                    observed_at=ensure_timezone_aware_utc(self._now()),
                )
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        raise ExternalSourceError("external HTTP retry budget exhausted")

    def _open_following_safe_redirects(
        self,
        requested_url: str,
        *,
        headers: Mapping[str, str],
        allowed_domains: set[str],
    ) -> Any:
        current_url = requested_url
        redirects = 0
        while True:
            response = self._session.get(
                current_url,
                headers=dict(headers),
                timeout=self._remaining_timeout(),
                allow_redirects=False,
                stream=True,
            )
            status = int(response.status_code)
            if status not in {301, 302, 303, 307, 308}:
                return response
            try:
                location = _header(response.headers, "Location")
                if location is None or not location.strip():
                    raise ExternalSourceError(
                        "external redirect is missing a Location header"
                    )
                if redirects >= self.settings.max_redirects:
                    raise ExternalSourceError(
                        "external source exceeded the redirect limit"
                    )
                target = _canonical_source_url(
                    urljoin(current_url, location.strip())
                )
                _require_safe_source_url(target, allowed_domains)
                if (
                    urlsplit(current_url).scheme.lower() == "https"
                    and urlsplit(target).scheme.lower() != "https"
                ):
                    raise ExternalSourceError(
                        "external redirect attempted an HTTPS downgrade"
                    )
                current_url = target
                redirects += 1
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    def _read_bounded(
        self, response: Any, headers: Mapping[str, str]
    ) -> bytes:
        if self._deadline_monotonic is not None:
            self._remaining_timeout()
        content_length = _header(headers, "Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self.settings.max_response_bytes:
                    raise ExternalSourceError(
                        "external response exceeds configured size limit"
                    )
            except ValueError as exc:
                raise ExternalSourceError(
                    "external response has invalid Content-Length"
                ) from exc
        chunks: list[bytes] = []
        size = 0
        iterator = getattr(response, "iter_content", None)
        values = (
            iterator(chunk_size=64 * 1024)
            if callable(iterator)
            else (getattr(response, "content", b""),)
        )
        for chunk in values:
            if self._deadline_monotonic is not None:
                self._remaining_timeout()
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                raise ExternalSourceError("external response yielded non-byte content")
            size += len(chunk)
            if size > self.settings.max_response_bytes:
                raise ExternalSourceError(
                    "external response exceeds configured size limit"
                )
            chunks.append(chunk)
        if self._deadline_monotonic is not None:
            self._remaining_timeout()
        return b"".join(chunks)

    def _retry_delay(self, attempt: int) -> float:
        return min(
            self.settings.retry_base_delay_seconds * (2**attempt),
            self.settings.retry_max_delay_seconds,
        )

    def _remaining_timeout(self) -> float:
        if self._deadline_monotonic is None:
            return self.settings.timeout_seconds
        remaining = self._deadline_monotonic - self._monotonic()
        if remaining <= 0:
            raise ExternalSourceError("external HTTP operation deadline expired")
        return min(self.settings.timeout_seconds, remaining)

    def _sleep_retry(self, delay: float) -> None:
        if delay > self.settings.retry_max_delay_seconds:
            raise ExternalSourceError(
                "external Retry-After exceeds configured maximum"
            )
        if self._deadline_monotonic is not None:
            remaining = self._deadline_monotonic - self._monotonic()
            if remaining <= 0 or delay >= remaining:
                raise ExternalSourceError(
                    "external HTTP retry exceeds operation deadline"
                )
        if delay > 0:
            self._sleeper(delay)


@dataclass(frozen=True, kw_only=True)
class FeedItem:
    identity: str
    title: str
    url: str
    published_at: datetime | None
    updated_at: datetime | None
    guid: str | None
    description: str | None


@dataclass(frozen=True, kw_only=True)
class ParsedFeed:
    items: tuple[FeedItem, ...]
    format: str
    truncated: bool


def parse_feed(
    content: bytes,
    *,
    max_items: int,
    base_url: str,
) -> ParsedFeed:
    """Parse bounded RSS 2.0 or Atom without resolving XML entities."""
    if max_items < 1:
        raise ExternalSourceError("feed item limit must be positive")
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ExternalSourceError("feed XML declarations are unsafe")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ExternalSourceError("official feed returned malformed XML") from exc
    root_name = _local_name(root.tag).lower()
    if root_name in {"rss", "rdf"}:
        entries = [value for value in root.iter() if _local_name(value.tag) == "item"]
        parser = _parse_rss_item
        format_name = "RSS"
    elif root_name == "feed":
        entries = [value for value in root if _local_name(value.tag) == "entry"]
        parser = _parse_atom_entry
        format_name = "ATOM"
    else:
        raise ExternalSourceError("official feed root is neither RSS nor Atom")
    parsed = tuple(parser(value, base_url=base_url) for value in entries[:max_items])
    return ParsedFeed(
        items=parsed,
        format=format_name,
        truncated=len(entries) > max_items,
    )


@dataclass(frozen=True, kw_only=True)
class SourceCollectionResult:
    source: str
    observed_at: datetime
    not_modified: bool
    discovered_count: int
    new_count: int
    duplicate_count: int
    revision_count: int
    skipped_count: int
    checkpoint_generation: int
    revision_ids: tuple[str, ...] = field(default_factory=tuple)
    pending_count: int = 0


@dataclass(frozen=True, kw_only=True)
class SourceHealthStatus:
    """Safe, persistent operational state for one HTTP source instance."""

    source: str
    enabled: bool
    last_successful_poll_at: datetime | None
    last_source_record_identity: str | None
    last_system_receipt_at: datetime | None
    last_source_published_at: datetime | None
    last_failure_at: datetime | None
    failure_category: str | None
    consecutive_failure_count: int
    duplicate_count: int
    new_record_count: int
    pending_classification_count: int
    completed_classification_count: int
    parser_extraction_version: str
    checkpoint_generation: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "enabled": self.enabled,
            "last_successful_poll_at": _optional_utc_iso(
                self.last_successful_poll_at
            ),
            "last_source_record_identity": self.last_source_record_identity,
            "last_system_receipt_at": _optional_utc_iso(
                self.last_system_receipt_at
            ),
            "last_source_published_at": _optional_utc_iso(
                self.last_source_published_at
            ),
            "last_failure_at": _optional_utc_iso(self.last_failure_at),
            "failure_category": self.failure_category,
            "consecutive_failure_count": self.consecutive_failure_count,
            "duplicate_count": self.duplicate_count,
            "new_record_count": self.new_record_count,
            "pending_classification_count": self.pending_classification_count,
            "completed_classification_count": self.completed_classification_count,
            "parser_extraction_version": self.parser_extraction_version,
            "checkpoint_generation": self.checkpoint_generation,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "SourceHealthStatus":
        source = str(value.get("source", "")).strip()
        parser_version = str(value.get("parser_extraction_version", "")).strip()
        if not source or not parser_version:
            raise ExternalSourceError("source health identity is invalid")
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ExternalSourceError("source health enablement is invalid")
        counts: dict[str, int] = {}
        for name in (
            "consecutive_failure_count",
            "duplicate_count",
            "new_record_count",
            "pending_classification_count",
            "completed_classification_count",
            "checkpoint_generation",
        ):
            raw = value.get(name, 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ExternalSourceError("source health count is invalid")
            counts[name] = raw
        identity = value.get("last_source_record_identity")
        failure = value.get("failure_category")
        if identity is not None and not isinstance(identity, str):
            raise ExternalSourceError("source health record identity is invalid")
        if failure is not None and not isinstance(failure, str):
            raise ExternalSourceError("source health failure category is invalid")
        return cls(
            source=source,
            enabled=enabled,
            last_successful_poll_at=_parse_optional_timestamp(
                value.get("last_successful_poll_at")
            ),
            last_source_record_identity=identity,
            last_system_receipt_at=_parse_optional_timestamp(
                value.get("last_system_receipt_at")
            ),
            last_source_published_at=_parse_optional_timestamp(
                value.get("last_source_published_at")
            ),
            last_failure_at=_parse_optional_timestamp(value.get("last_failure_at")),
            failure_category=failure,
            consecutive_failure_count=counts["consecutive_failure_count"],
            duplicate_count=counts["duplicate_count"],
            new_record_count=counts["new_record_count"],
            pending_classification_count=counts["pending_classification_count"],
            completed_classification_count=counts[
                "completed_classification_count"
            ],
            parser_extraction_version=parser_version,
            checkpoint_generation=counts["checkpoint_generation"],
        )


@dataclass(frozen=True, kw_only=True)
class _PendingDiscoveryBatch:
    """Safe mutable-checkpoint reference to one immutable discovery response."""

    kind: str
    object_hash: str
    final_url: str
    observed_at: datetime
    item_ids: tuple[str, ...]
    year: int | None = None
    ticker: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"LMT_FEED", "PLTR_RELEASE_LIST", "EARNINGS_INDEX"}:
            raise ExternalSourceError("pending discovery kind is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.object_hash) is None:
            raise ExternalSourceError("pending discovery object hash is invalid")
        canonical_url = canonicalize_url(self.final_url)
        object.__setattr__(self, "final_url", canonical_url)
        object.__setattr__(
            self, "observed_at", ensure_timezone_aware_utc(self.observed_at)
        )
        normalized_ids = tuple(
            dict.fromkeys(_required_pending_id(value) for value in self.item_ids)
        )
        if not normalized_ids:
            raise ExternalSourceError("pending discovery batch must contain item IDs")
        object.__setattr__(self, "item_ids", normalized_ids)
        if self.year is not None and not (2000 <= self.year <= 9999):
            raise ExternalSourceError("pending discovery year is invalid")
        if self.ticker is not None:
            ticker = self.ticker.upper()
            if ticker not in {"PLTR", "LMT"}:
                raise ExternalSourceError("pending discovery ticker is invalid")
            object.__setattr__(self, "ticker", ticker)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_hash": self.object_hash,
            "final_url": self.final_url,
            "observed_at": to_utc_iso(self.observed_at),
            "item_ids": list(self.item_ids),
            "year": self.year,
            "ticker": self.ticker,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "_PendingDiscoveryBatch":
        item_ids = value.get("item_ids")
        if not isinstance(item_ids, list):
            raise ExternalSourceError("pending discovery item IDs have invalid shape")
        year = value.get("year")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
            raise ExternalSourceError("pending discovery year has invalid type")
        ticker = value.get("ticker")
        if ticker is not None and not isinstance(ticker, str):
            raise ExternalSourceError("pending discovery ticker has invalid type")
        try:
            observed_at = parse_utc_iso(str(value["observed_at"]))
        except (KeyError, ValueError) as exc:
            raise ExternalSourceError(
                "pending discovery observation time is invalid"
            ) from exc
        return cls(
            kind=str(value.get("kind", "")),
            object_hash=str(value.get("object_hash", "")),
            final_url=str(value.get("final_url", "")),
            observed_at=observed_at,
            item_ids=tuple(str(item) for item in item_ids),
            year=year,
            ticker=ticker,
        )

    def with_item_ids(self, item_ids: Iterable[str]) -> "_PendingDiscoveryBatch | None":
        values = tuple(item_ids)
        if not values:
            return None
        return _PendingDiscoveryBatch(
            kind=self.kind,
            object_hash=self.object_hash,
            final_url=self.final_url,
            observed_at=self.observed_at,
            item_ids=values,
            year=self.year,
            ticker=self.ticker,
        )


@dataclass(frozen=True, kw_only=True)
class LockheedMartinRSSSettings:
    feed_url: str = (
        "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    )
    source: str = LMT_SOURCE
    fixed_ticker: str = "LMT"
    max_feed_items: int = 100
    official_domains: tuple[str, ...] = (
        "news.lockheedmartin.com",
        "lockheedmartin.com",
        "www.lockheedmartin.com",
    )
    article_selectors: tuple[str, ...] = (
        "div.wd_body.wd_news_body.fr-view",
        "div.wd_news_body",
        "article",
    )
    adapter_version: str = "lmt_rss_adapter_v1"
    extractor_version: str = "lmt_article_html_v1"


class LockheedMartinRSSAdapter:
    """LMT-specific adapter over the reusable RSS/Atom and HTTP seams."""

    def __init__(
        self,
        *,
        client: BoundedHTTPClient,
        archive: ExternalEventArchive,
        settings: LockheedMartinRSSSettings | None = None,
        now: Callable[[], datetime] = utc_now,
        enabled: bool = True,
    ) -> None:
        self.client = client
        self.archive = archive
        self.settings = settings or LockheedMartinRSSSettings()
        self._now = now
        self._enabled = bool(enabled)

    def collect_once(
        self,
        *,
        max_items: int,
        establish_checkpoint: bool = False,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        backfill: bool = False,
    ) -> SourceCollectionResult:
        checkpoint_key = (
            self.settings.source
            if not backfill
            else f"{self.settings.source}:backfill"
        )
        try:
            result = self._collect_once_impl(
                max_items=max_items,
                establish_checkpoint=establish_checkpoint,
                start_time=start_time,
                end_time=end_time,
                backfill=backfill,
            )
        except Exception as exc:
            _record_health_failure(
                self.archive,
                source=self.settings.source,
                health_key=_health_key(checkpoint_key),
                enabled=self._enabled,
                parser_extraction_version=_version_label(
                    self.settings.adapter_version,
                    self.settings.extractor_version,
                    "rss_atom_xml_v1",
                ),
                failed_at=self._now(),
                error=exc,
            )
            raise
        _record_health_success(
            self.archive,
            source=self.settings.source,
            collection_checkpoint_key=checkpoint_key,
            health_key=_health_key(checkpoint_key),
            enabled=self._enabled,
            parser_extraction_version=_version_label(
                self.settings.adapter_version,
                self.settings.extractor_version,
                "rss_atom_xml_v1",
            ),
            result=result,
        )
        return result

    def get_health(self, *, backfill: bool = False) -> SourceHealthStatus | None:
        checkpoint_key = (
            self.settings.source
            if not backfill
            else f"{self.settings.source}:backfill"
        )
        return _load_health(self.archive, _health_key(checkpoint_key))

    def _collect_once_impl(
        self,
        *,
        max_items: int,
        establish_checkpoint: bool = False,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        backfill: bool = False,
    ) -> SourceCollectionResult:
        start_time, end_time = _validated_time_range(start_time, end_time)
        if backfill and (start_time is None or end_time is None):
            raise ExternalSourceError(
                "LMT backfill requires explicit start_time and end_time"
            )
        limit = _bounded_limit(max_items, self.settings.max_feed_items)
        checkpoint_key = (
            self.settings.source
            if not backfill
            else f"{self.settings.source}:backfill"
        )
        checkpoint = self.archive.get_checkpoint(checkpoint_key) or {}
        pending_batches = _load_pending_batches(
            checkpoint, expected_kind="LMT_FEED"
        )
        if establish_checkpoint and pending_batches:
            raise ExternalSourceError(
                "LMT checkpoint establishment cannot discard pending discoveries"
            )
        response = self.client.get(
            self.settings.feed_url,
            conditional_headers=(
                _conditional_headers(checkpoint) if not backfill else None
            ),
            allowed_domains=self.settings.official_domains,
            accepted_content_types=("application/rss+xml", "application/xml", "text/xml"),
        )
        cutoff = _parse_optional_timestamp(checkpoint.get("bootstrap_cutoff"))
        bootstrap_item_hashes = _checkpoint_hash_map(
            checkpoint, "bootstrap_item_hashes"
        )
        _require_bootstrap_hash_version(
            checkpoint,
            hashes=bootstrap_item_hashes,
            version_key="bootstrap_item_hash_version",
            expected=LMT_BOOTSTRAP_HASH_VERSION,
        )
        current_item_ids: tuple[str, ...] = ()
        skipped = 0
        bootstrap_cutoff = cutoff
        if response.not_modified:
            _require_conditional_checkpoint(checkpoint)
        else:
            feed_hash = self.archive.archive_object(
                response.content,
                extension="xml",
                content_type=response.content_type,
            )
            try:
                parsed = parse_feed(
                    response.content,
                    max_items=self.settings.max_feed_items,
                    base_url=response.final_url,
                )
                candidates = _filter_feed_items(
                    parsed.items, start_time=start_time, end_time=end_time
                )
                for item in candidates:
                    if item.published_at is None:
                        raise ExternalSourceError(
                            "LMT feed item is missing a publication timestamp"
                        )
                    _require_allowed_domain(
                        item.url, set(self.settings.official_domains)
                    )
            except (ExternalSourceError, ExternalNormalizationError):
                _publish_rejected_http_observation(
                    self.archive,
                    source=self.settings.source,
                    response=response,
                    raw_object_hash=feed_hash,
                    stage="FEED_PARSE",
                    failure_category="PARSER_SCHEMA_DRIFT",
                )
                raise
            current_item_ids = tuple(item.identity for item in candidates)
            self.archive.publish_observation(
                source=self.settings.source,
                payload={
                    "kind": "FEED_POLL",
                    "feed_object_hash": feed_hash,
                    "requested_url": response.requested_url,
                    "final_url": response.final_url,
                    "system_observed_at": to_utc_iso(response.observed_at),
                    "feed_format": parsed.format,
                    "item_count": len(candidates),
                    "truncated": parsed.truncated,
                },
            )
            if establish_checkpoint:
                for item in candidates[:limit]:
                    self.archive.publish_observation(
                        source=self.settings.source,
                        payload=_discovery_observation(item, response.observed_at),
                    )
                skipped = len(candidates)
                bootstrap_item_hashes = {
                    **bootstrap_item_hashes,
                    **{
                        item.identity: _feed_item_discovery_hash(item)
                        for item in candidates
                    },
                }
                bootstrap_cutoff = max(
                    (item.published_at for item in candidates),
                    default=cutoff,
                )
            else:
                eligible: list[FeedItem] = []
                for item in candidates:
                    assert item.published_at is not None
                    baseline_hash = bootstrap_item_hashes.get(item.identity)
                    if (
                        not _fact_exists(
                            self.archive, self.settings.source, item.identity
                        )
                        and (
                            (
                                baseline_hash is not None
                                and baseline_hash == _feed_item_discovery_hash(item)
                            )
                            or (
                                baseline_hash is None
                                and cutoff is not None
                                and item.published_at <= cutoff
                            )
                        )
                    ):
                        skipped += 1
                        continue
                    eligible.append(item)
                if eligible:
                    pending_batches = _append_pending_batch(
                        pending_batches,
                        _PendingDiscoveryBatch(
                            kind="LMT_FEED",
                            object_hash=feed_hash,
                            final_url=response.final_url,
                            observed_at=response.observed_at,
                            item_ids=tuple(item.identity for item in eligible),
                        ),
                    )

        staged_checkpoint = _checkpoint_payload(
            checkpoint,
            response,
            item_ids=current_item_ids,
            bootstrap_cutoff=bootstrap_cutoff,
            extra={
                "bootstrap_item_hashes": bootstrap_item_hashes,
                "bootstrap_item_hash_version": LMT_BOOTSTRAP_HASH_VERSION,
                "pending_batches": _pending_batch_payloads(pending_batches),
            },
        )
        generation = self.archive.update_checkpoint(
            checkpoint_key, staged_checkpoint
        )
        if establish_checkpoint:
            _record_one_shot_live_coverage(
                self.archive,
                source=self.settings.source,
                observed_at=response.observed_at,
            )
            return SourceCollectionResult(
                source=self.settings.source,
                observed_at=response.observed_at,
                not_modified=response.not_modified,
                discovered_count=min(len(current_item_ids), limit),
                new_count=0,
                duplicate_count=0,
                revision_count=0,
                skipped_count=skipped,
                checkpoint_generation=generation,
                pending_count=0,
            )

        revision_ids: list[str] = []
        duplicates = 0
        acquired = 0
        remaining_batches: list[_PendingDiscoveryBatch] = []
        for batch_index, batch in enumerate(pending_batches):
            items = _load_lmt_pending_items(
                self.archive,
                batch,
                settings=self.settings,
            )
            remaining_ids: list[str] = []
            for item in items:
                if acquired >= limit:
                    remaining_ids.append(item.identity)
                    continue
                article = self.client.get(
                    item.url,
                    allowed_domains=self.settings.official_domains,
                    accepted_content_types=("text/html", "application/xhtml+xml"),
                )
                # Preserve the official response before source-specific
                # extraction.  A parser drift/empty-body failure must not lose
                # the bytes that explain the failed observation.
                raw_object_hash = self.archive.archive_object(
                    article.content,
                    extension="html",
                    content_type=article.content_type,
                )
                try:
                    normalized = extract_article_html(
                        article.content,
                        selectors=self.settings.article_selectors,
                    )
                except ExternalNormalizationError as exc:
                    _publish_rejected_http_observation(
                        self.archive,
                        source=self.settings.source,
                        response=article,
                        raw_object_hash=raw_object_hash,
                        stage="ARTICLE_EXTRACTION",
                        failure_category="EMPTY_OR_INVALID_EXTRACTION",
                        source_fact_id=item.identity,
                    )
                    raise ExternalSourceError(
                        "official article extraction failed"
                    ) from exc
                revision, duplicate = _archive_document_revision(
                    archive=self.archive,
                    source=self.settings.source,
                    source_fact_id=item.identity,
                    source_type="OFFICIAL_COMPANY_NEWS",
                    source_platform="LOCKHEED_MARTIN_NEWSROOM",
                    source_uri=article.final_url,
                    source_title=item.title,
                    source_published_at=item.published_at,
                    source_updated_at=item.updated_at,
                    observed_at=article.observed_at,
                    raw_content=article.content,
                    extension="html",
                    content_type=article.content_type,
                    normalized_text=normalized,
                    fixed_tickers=(self.settings.fixed_ticker,),
                    adapter_version=self.settings.adapter_version,
                    extractor_version=self.settings.extractor_version,
                    normalizer_version=HTML_NORMALIZER_VERSION,
                    authoritative_revision_sequence=None,
                    archived_at=self._now(),
                    collection_mode=("BACKFILL" if backfill else "LIVE_SYSTEM"),
                )
                self.archive.publish_observation(
                    source=self.settings.source,
                    payload={
                        **_discovery_observation(item, article.observed_at),
                        "raw_object_hash": revision.raw_object_hash,
                        "source_revision_id": revision.source_revision_id,
                        "duplicate": duplicate,
                        "discovered_url": item.url,
                        "final_url": article.final_url,
                    },
                    source_revision_id=revision.source_revision_id,
                    observed_at=article.observed_at,
                )
                if duplicate:
                    duplicates += 1
                else:
                    revision_ids.append(revision.source_revision_id)
                acquired += 1
            remaining = batch.with_item_ids(remaining_ids)
            if remaining is not None:
                remaining_batches.append(remaining)
            if acquired >= limit:
                remaining_batches.extend(pending_batches[batch_index + 1 :])
                break

        final_checkpoint = {
            **staged_checkpoint,
            "pending_batches": _pending_batch_payloads(remaining_batches),
        }
        generation = self.archive.update_checkpoint(
            checkpoint_key,
            final_checkpoint,
        )
        if backfill:
            assert start_time is not None and end_time is not None
            _record_partial_backfill_coverage(
                self.archive,
                source=self.settings.source,
                start=start_time,
                end=end_time,
                observed_at=response.observed_at,
            )
        else:
            _record_one_shot_live_coverage(
                self.archive,
                source=self.settings.source,
                observed_at=response.observed_at,
            )
        return SourceCollectionResult(
            source=self.settings.source,
            observed_at=response.observed_at,
            not_modified=response.not_modified,
            discovered_count=acquired,
            new_count=len(revision_ids),
            duplicate_count=duplicates,
            revision_count=len(revision_ids),
            skipped_count=skipped,
            checkpoint_generation=generation,
            revision_ids=tuple(revision_ids),
            pending_count=_pending_batch_count(remaining_batches),
        )

    def collect_backfill(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        max_items: int,
    ) -> SourceCollectionResult:
        """Process only currently advertised feed items inside a bounded range.

        The RSS feed isn't a historical archive.  A successful call therefore
        does not by itself prove complete coverage of the requested interval.
        """
        return self.collect_once(
            max_items=max_items,
            start_time=start_time,
            end_time=end_time,
            backfill=True,
        )


@dataclass(frozen=True, kw_only=True)
class PalantirRelease:
    press_release_id: str
    revision_number: int
    headline: str
    body_html: str
    canonical_url: str
    published_at: datetime


@dataclass(frozen=True, kw_only=True)
class PalantirIRSettings:
    endpoint_template: str = (
        "https://investors.palantir.com/feed/PressRelease.svc/"
        "GetPressReleaseList?languageId=1&bodyType=1&year={year}"
        "&includeTags=true&pressReleaseDateFilter=1"
    )
    source: str = PLTR_SOURCE
    fixed_ticker: str = "PLTR"
    max_items: int = 100
    official_domains: tuple[str, ...] = (
        "investors.palantir.com",
        "www.palantir.com",
        "palantir.com",
    )
    adapter_version: str = "pltr_ir_json_adapter_v1"
    extractor_version: str = "pltr_ir_body_html_v1"


class PalantirIRAdapter:
    """Strict adapter for the official JSON endpoint used by Palantir IR."""

    def __init__(
        self,
        *,
        client: BoundedHTTPClient,
        archive: ExternalEventArchive,
        settings: PalantirIRSettings | None = None,
        now: Callable[[], datetime] = utc_now,
        enabled: bool = True,
    ) -> None:
        self.client = client
        self.archive = archive
        self.settings = settings or PalantirIRSettings()
        self._now = now
        self._enabled = bool(enabled)

    def collect_once(
        self,
        *,
        year: int,
        max_items: int,
        establish_checkpoint: bool = False,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        backfill: bool = False,
    ) -> SourceCollectionResult:
        checkpoint_key = (
            f"{self.settings.source}:{year}"
            f"{':backfill' if backfill else ''}"
        )
        try:
            result = self._collect_once_impl(
                year=year,
                max_items=max_items,
                establish_checkpoint=establish_checkpoint,
                start_time=start_time,
                end_time=end_time,
                backfill=backfill,
            )
        except Exception as exc:
            _record_health_failure(
                self.archive,
                source=self.settings.source,
                health_key=_health_key(checkpoint_key),
                enabled=self._enabled,
                parser_extraction_version=_version_label(
                    self.settings.adapter_version,
                    self.settings.extractor_version,
                ),
                failed_at=self._now(),
                error=exc,
            )
            raise
        _record_health_success(
            self.archive,
            source=self.settings.source,
            collection_checkpoint_key=checkpoint_key,
            health_key=_health_key(checkpoint_key),
            enabled=self._enabled,
            parser_extraction_version=_version_label(
                self.settings.adapter_version,
                self.settings.extractor_version,
            ),
            result=result,
        )
        return result

    def get_health(
        self, *, year: int, backfill: bool = False
    ) -> SourceHealthStatus | None:
        checkpoint_key = (
            f"{self.settings.source}:{year}"
            f"{':backfill' if backfill else ''}"
        )
        return _load_health(self.archive, _health_key(checkpoint_key))

    def _collect_once_impl(
        self,
        *,
        year: int,
        max_items: int,
        establish_checkpoint: bool = False,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        backfill: bool = False,
    ) -> SourceCollectionResult:
        start_time, end_time = _validated_time_range(start_time, end_time)
        if backfill and (start_time is None or end_time is None):
            raise ExternalSourceError(
                "Palantir backfill requires explicit start_time and end_time"
            )
        limit = _bounded_limit(max_items, self.settings.max_items)
        response, releases, checkpoint = self._fetch_releases(
            year=year,
            use_conditional=not backfill,
            checkpoint_suffix=":backfill" if backfill else "",
        )
        checkpoint_key = (
            f"{self.settings.source}:{year}"
            f"{':backfill' if backfill else ''}"
        )
        pending_batches = _load_pending_batches(
            checkpoint, expected_kind="PLTR_RELEASE_LIST"
        )
        if establish_checkpoint and pending_batches:
            raise ExternalSourceError(
                "Palantir checkpoint establishment cannot discard pending discoveries"
        )
        cutoff = _parse_optional_timestamp(checkpoint.get("bootstrap_cutoff"))
        bootstrap_release_hashes = _checkpoint_hash_map(
            checkpoint, "bootstrap_release_hashes"
        )
        _require_bootstrap_hash_version(
            checkpoint,
            hashes=bootstrap_release_hashes,
            version_key="bootstrap_release_hash_version",
            expected=PLTR_BOOTSTRAP_HASH_VERSION,
        )
        current_item_ids: tuple[str, ...] = ()
        skipped = 0
        bootstrap_cutoff = cutoff
        if response.not_modified:
            _require_conditional_checkpoint(checkpoint)
        else:
            candidates = tuple(
                value
                for value in releases
                if _timestamp_in_range(
                    value.published_at, start_time=start_time, end_time=end_time
                )
            )
            current_item_ids = tuple(
                value.press_release_id for value in candidates
            )
            if establish_checkpoint:
                for release in candidates[:limit]:
                    self._publish_release_observation(
                        release,
                        response=response,
                        revision=None,
                        duplicate=False,
                    )
                skipped = len(candidates)
                bootstrap_release_hashes = {
                    **bootstrap_release_hashes,
                    **{
                        release.press_release_id: _palantir_release_discovery_hash(
                            release
                        )
                        for release in candidates
                    },
                }
                bootstrap_cutoff = max(
                    (value.published_at for value in candidates),
                    default=cutoff,
                )
            else:
                eligible: list[PalantirRelease] = []
                for release in candidates:
                    baseline_hash = bootstrap_release_hashes.get(
                        release.press_release_id
                    )
                    if (
                        not _fact_exists(
                            self.archive,
                            self.settings.source,
                            release.press_release_id,
                        )
                        and (
                            (
                                baseline_hash is not None
                                and baseline_hash
                                == _palantir_release_discovery_hash(release)
                            )
                            or (
                                baseline_hash is None
                                and cutoff is not None
                                and release.published_at <= cutoff
                            )
                        )
                    ):
                        skipped += 1
                        continue
                    eligible.append(release)
                if eligible:
                    pending_batches = _append_pending_batch(
                        pending_batches,
                        _PendingDiscoveryBatch(
                            kind="PLTR_RELEASE_LIST",
                            object_hash=sha256(response.content).hexdigest(),
                            final_url=response.final_url,
                            observed_at=response.observed_at,
                            item_ids=tuple(
                                value.press_release_id for value in eligible
                            ),
                            year=year,
                        ),
                    )

        last_nonzero_count = (
            int(checkpoint.get("last_nonzero_count", 0))
            if response.not_modified
            else len(releases)
        )
        staged_checkpoint = _checkpoint_payload(
            checkpoint,
            response,
            item_ids=current_item_ids,
            bootstrap_cutoff=bootstrap_cutoff,
            extra={
                "last_nonzero_count": last_nonzero_count,
                "year": year,
                "bootstrap_release_hashes": bootstrap_release_hashes,
                "bootstrap_release_hash_version": PLTR_BOOTSTRAP_HASH_VERSION,
                "pending_batches": _pending_batch_payloads(pending_batches),
            },
        )
        generation = self.archive.update_checkpoint(
            checkpoint_key, staged_checkpoint
        )
        if establish_checkpoint:
            _record_one_shot_live_coverage(
                self.archive,
                source=self.settings.source,
                observed_at=response.observed_at,
            )
            return SourceCollectionResult(
                source=self.settings.source,
                observed_at=response.observed_at,
                not_modified=response.not_modified,
                discovered_count=min(len(current_item_ids), limit),
                new_count=0,
                duplicate_count=0,
                revision_count=0,
                skipped_count=skipped,
                checkpoint_generation=generation,
                pending_count=0,
            )

        revision_ids: list[str] = []
        duplicates = 0
        acquired = 0
        remaining_batches: list[_PendingDiscoveryBatch] = []
        for batch_index, batch in enumerate(pending_batches):
            batch_releases, batch_response = _load_pltr_pending_releases(
                self.archive,
                batch,
                settings=self.settings,
            )
            remaining_ids: list[str] = []
            for release in batch_releases:
                if acquired >= limit:
                    remaining_ids.append(release.press_release_id)
                    continue
                revision, duplicate = self.archive_release(
                    release,
                    response=batch_response,
                    source=self.settings.source,
                    source_fact_id=release.press_release_id,
                    collection_mode=("BACKFILL" if backfill else "LIVE_SYSTEM"),
                )
                if duplicate:
                    duplicates += 1
                else:
                    revision_ids.append(revision.source_revision_id)
                acquired += 1
            remaining = batch.with_item_ids(remaining_ids)
            if remaining is not None:
                remaining_batches.append(remaining)
            if acquired >= limit:
                remaining_batches.extend(pending_batches[batch_index + 1 :])
                break

        final_checkpoint = {
            **staged_checkpoint,
            "pending_batches": _pending_batch_payloads(remaining_batches),
        }
        generation = self.archive.update_checkpoint(
            checkpoint_key,
            final_checkpoint,
        )
        if backfill:
            assert start_time is not None and end_time is not None
            _record_partial_backfill_coverage(
                self.archive,
                source=self.settings.source,
                start=start_time,
                end=end_time,
                observed_at=response.observed_at,
            )
        else:
            _record_one_shot_live_coverage(
                self.archive,
                source=self.settings.source,
                observed_at=response.observed_at,
            )
        return SourceCollectionResult(
            source=self.settings.source,
            observed_at=response.observed_at,
            not_modified=response.not_modified,
            discovered_count=acquired,
            new_count=len(revision_ids),
            duplicate_count=duplicates,
            revision_count=len(revision_ids),
            skipped_count=skipped,
            checkpoint_generation=generation,
            revision_ids=tuple(revision_ids),
            pending_count=_pending_batch_count(remaining_batches),
        )

    def collect_backfill(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        max_items: int,
    ) -> SourceCollectionResult:
        start_time, end_time = _validated_time_range(start_time, end_time)
        assert start_time is not None and end_time is not None
        remaining = _bounded_limit(max_items, self.settings.max_items)
        results: list[SourceCollectionResult] = []
        for year in range(end_time.year, start_time.year - 1, -1):
            if remaining <= 0:
                break
            result = self.collect_once(
                year=year,
                max_items=remaining,
                start_time=start_time,
                end_time=end_time,
                backfill=True,
            )
            results.append(result)
            # One bounded operation unit is consumed even when a year is empty
            # or every discovered release is already archived.  This prevents
            # a broad date range plus max_items=1 from issuing thousands of
            # requests while still allowing one response to consume multiple
            # item units when explicitly requested.
            remaining -= max(1, result.discovered_count)
        if not results:
            raise ExternalSourceError("Palantir backfill produced no bounded request")
        return SourceCollectionResult(
            source=self.settings.source,
            observed_at=max(value.observed_at for value in results),
            not_modified=all(value.not_modified for value in results),
            discovered_count=sum(value.discovered_count for value in results),
            new_count=sum(value.new_count for value in results),
            duplicate_count=sum(value.duplicate_count for value in results),
            revision_count=sum(value.revision_count for value in results),
            skipped_count=sum(value.skipped_count for value in results),
            checkpoint_generation=results[-1].checkpoint_generation,
            revision_ids=tuple(
                revision_id
                for value in results
                for revision_id in value.revision_ids
            ),
            pending_count=sum(value.pending_count for value in results),
        )

    def find_release(self, *, year: int, canonical_url: str) -> PalantirRelease:
        release, _response = self.find_release_with_response(
            year=year, canonical_url=canonical_url
        )
        return release

    def find_release_with_response(
        self, *, year: int, canonical_url: str
    ) -> tuple[PalantirRelease, HTTPFetchResult]:
        response, releases, _checkpoint = self._fetch_releases(
            year=year, use_conditional=False
        )
        target = _palantir_release_url_key(canonical_url)
        matches = [
            value
            for value in releases
            if _palantir_release_url_key(value.canonical_url) == target
        ]
        if len(matches) != 1:
            raise ExternalSourceError(
                "Palantir earnings URL did not map to exactly one official release"
            )
        return matches[0], response

    def archive_release(
        self,
        release: PalantirRelease,
        *,
        response: HTTPFetchResult,
        source: str,
        source_fact_id: str,
        earnings_package_id: str | None = None,
        adapter_version: str | None = None,
        collection_mode: str = "LIVE_SYSTEM",
    ) -> tuple[ExternalSourceRevision, bool]:
        raw = release.body_html.encode("utf-8")
        raw_object_hash = self.archive.archive_object(
            raw, extension="html", content_type="text/html"
        )
        try:
            normalized = normalize_html_fragment(release.body_html)
        except ExternalNormalizationError:
            _publish_rejected_http_observation(
                self.archive,
                source=source,
                response=response,
                raw_object_hash=raw_object_hash,
                stage="RELEASE_EXTRACTION",
                failure_category="EMPTY_OR_INVALID_EXTRACTION",
                source_fact_id=source_fact_id,
            )
            raise
        if not normalized:
            _publish_rejected_http_observation(
                self.archive,
                source=source,
                response=response,
                raw_object_hash=raw_object_hash,
                stage="RELEASE_EXTRACTION",
                failure_category="EMPTY_OR_INVALID_EXTRACTION",
                source_fact_id=source_fact_id,
            )
            raise ExternalSourceError("Palantir release body normalized to empty text")
        revision, duplicate = _archive_document_revision(
            archive=self.archive,
            source=source,
            source_fact_id=source_fact_id,
            source_type=(
                "OFFICIAL_COMPANY_NEWS"
                if earnings_package_id is None
                else "OFFICIAL_EARNINGS_RELEASE"
            ),
            source_platform="PALANTIR_INVESTOR_RELATIONS",
            source_uri=release.canonical_url,
            source_title=release.headline,
            source_published_at=release.published_at,
            source_updated_at=None,
            observed_at=response.observed_at,
            raw_content=raw,
            extension="html",
            content_type="text/html",
            normalized_text=normalized,
            fixed_tickers=(self.settings.fixed_ticker,),
            adapter_version=adapter_version or self.settings.adapter_version,
            extractor_version=self.settings.extractor_version,
            normalizer_version=HTML_NORMALIZER_VERSION,
            authoritative_revision_sequence=release.revision_number,
            archived_at=self._now(),
            correlation_group_id=(
                None
                if earnings_package_id is None
                else f"earnings:{earnings_package_id}"
            ),
            relationship_types=(
                ()
                if earnings_package_id is None
                else ("SAME_EARNINGS_OCCURRENCE",)
            ),
            earnings_package_id=earnings_package_id,
            collection_mode=collection_mode,
        )
        self._publish_release_observation(
            release,
            response=response,
            revision=revision,
            duplicate=duplicate,
            observation_source=source,
        )
        return revision, duplicate

    def _fetch_releases(
        self,
        *,
        year: int,
        use_conditional: bool = True,
        checkpoint_suffix: str = "",
    ) -> tuple[HTTPFetchResult, tuple[PalantirRelease, ...], dict[str, Any]]:
        if year < 2000 or year > 9999:
            raise ExternalSourceError("Palantir release year is invalid")
        checkpoint = self.archive.get_checkpoint(
            f"{self.settings.source}:{year}{checkpoint_suffix}"
        ) or {}
        response = self.client.get(
            self.settings.endpoint_template.format(year=year),
            conditional_headers=(
                _conditional_headers(checkpoint) if use_conditional else None
            ),
            allowed_domains=self.settings.official_domains,
            accepted_content_types=("application/json", "text/json"),
        )
        if response.not_modified:
            return response, (), checkpoint
        object_hash = self.archive.archive_object(
            response.content,
            extension="json",
            content_type=response.content_type,
        )
        try:
            releases = parse_palantir_release_list(
                response.content,
                official_domains=self.settings.official_domains,
                max_items=self.settings.max_items,
            )
            previous_nonzero = int(checkpoint.get("last_nonzero_count", 0))
            if not releases and previous_nonzero > 0:
                raise ExternalSourceError(
                    "Palantir release endpoint unexpectedly returned zero results"
                )
        except (ExternalSourceError, ExternalNormalizationError):
            _publish_rejected_http_observation(
                self.archive,
                source=self.settings.source,
                response=response,
                raw_object_hash=object_hash,
                stage="INDEX_PARSE",
                failure_category="PARSER_SCHEMA_DRIFT",
            )
            raise
        self.archive.publish_observation(
            source=self.settings.source,
            payload={
                "kind": "INDEX_POLL",
                "index_object_hash": object_hash,
                "requested_url": response.requested_url,
                "final_url": response.final_url,
                "system_observed_at": to_utc_iso(response.observed_at),
                "release_count": len(releases),
            },
        )
        return response, releases, checkpoint

    def _publish_release_observation(
        self,
        release: PalantirRelease,
        *,
        response: HTTPFetchResult,
        revision: ExternalSourceRevision | None,
        duplicate: bool,
        observation_source: str | None = None,
    ) -> None:
        self.archive.publish_observation(
            source=observation_source or self.settings.source,
            payload={
                "kind": "RELEASE_DISCOVERY",
                "press_release_id": release.press_release_id,
                "revision_number": release.revision_number,
                "canonical_url": release.canonical_url,
                "source_published_at": to_utc_iso(release.published_at),
                "system_observed_at": to_utc_iso(response.observed_at),
                "source_revision_id": (
                    None if revision is None else revision.source_revision_id
                ),
                "raw_object_hash": (
                    None if revision is None else revision.raw_object_hash
                ),
                "duplicate": duplicate,
            },
            source_revision_id=(
                None if revision is None else revision.source_revision_id
            ),
            observed_at=response.observed_at,
        )


def parse_palantir_release_list(
    content: bytes,
    *,
    official_domains: Iterable[str],
    max_items: int = 1_000,
) -> tuple[PalantirRelease, ...]:
    try:
        root = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalSourceError("Palantir endpoint returned invalid JSON") from exc
    if not isinstance(root, Mapping) or set(root) != {"GetPressReleaseListResult"}:
        raise ExternalSourceError("Palantir endpoint schema root changed")
    values = root["GetPressReleaseListResult"]
    if not isinstance(values, list):
        raise ExternalSourceError("Palantir release list schema changed")
    if max_items < 1:
        raise ExternalSourceError("Palantir item limit must be positive")
    if len(values) > max_items:
        raise ExternalSourceError(
            "Palantir release list exceeds the configured item limit"
        )
    releases: list[PalantirRelease] = []
    seen_ids: set[str] = set()
    allowed = {value.lower() for value in official_domains}
    for raw in values:
        if not isinstance(raw, Mapping):
            raise ExternalSourceError("Palantir release item is not an object")
        required = {
            "PressReleaseId",
            "RevisionNumber",
            "Headline",
            "Body",
            "LinkToDetailPage",
            "PressReleaseDate",
        }
        if not required.issubset(raw):
            raise ExternalSourceError("Palantir release item is missing required fields")
        press_id = str(raw["PressReleaseId"]).strip()
        headline = raw["Headline"]
        body = raw["Body"]
        path = raw["LinkToDetailPage"]
        if not press_id or not isinstance(headline, str) or not headline.strip():
            raise ExternalSourceError("Palantir release identity or headline is invalid")
        if not isinstance(body, str) or not normalize_html_fragment(body):
            raise ExternalSourceError("Palantir release body is empty")
        if not isinstance(path, str) or not path.strip():
            raise ExternalSourceError("Palantir release URL is invalid")
        revision_number = raw["RevisionNumber"]
        if isinstance(revision_number, bool) or not isinstance(
            revision_number, int
        ):
            raise ExternalSourceError("Palantir revision number is invalid")
        if revision_number < 1:
            raise ExternalSourceError("Palantir revision number must be positive")
        canonical = canonicalize_url(
            urljoin("https://investors.palantir.com/", path)
        )
        _require_allowed_domain(canonical, allowed)
        published = _parse_palantir_datetime(raw["PressReleaseDate"])
        if press_id in seen_ids:
            raise ExternalSourceError("Palantir endpoint repeated a release ID")
        seen_ids.add(press_id)
        releases.append(
            PalantirRelease(
                press_release_id=press_id,
                revision_number=revision_number,
                headline=headline.strip(),
                body_html=body,
                canonical_url=canonical,
                published_at=published,
            )
        )
    releases.sort(
        key=lambda value: (value.published_at, value.press_release_id),
        reverse=True,
    )
    return tuple(releases)


@dataclass(frozen=True, kw_only=True)
class EarningsPackage:
    ticker: str
    fiscal_year: int
    fiscal_quarter: int
    primary_url: str
    supporting_urls: tuple[str, ...] = field(default_factory=tuple)
    supporting_document_roles: tuple[tuple[str, str], ...] = field(
        default_factory=tuple
    )

    @property
    def package_id(self) -> str:
        return f"{self.ticker}:{self.fiscal_year}:Q{self.fiscal_quarter}"


@dataclass(frozen=True, kw_only=True)
class _ArchivedEarningsPrimary:
    revision: ExternalSourceRevision
    duplicate: bool
    content_type: str


@dataclass(frozen=True, kw_only=True)
class EarningsSettings:
    pltr_events_url: str = "https://investors.palantir.com/events.html"
    lmt_results_url: str = (
        "https://investors.lockheedmartin.com/financial-information/"
        "quarterly-results/"
    )
    source: str = EARNINGS_SOURCE
    max_items: int = 20
    pltr_domains: tuple[str, ...] = (
        "investors.palantir.com",
        "www.palantir.com",
        "palantir.com",
    )
    lmt_domains: tuple[str, ...] = (
        "investors.lockheedmartin.com",
        "www.lockheedmartin.com",
        "lockheedmartin.com",
    )
    lmt_article_selectors: tuple[str, ...] = (
        "div.wd_body.wd_news_body.fr-view",
        "div.wd_news_body",
        "article",
        "main",
    )
    adapter_version: str = "company_earnings_adapter_v1"
    pdf_extraction_version: str = PDF_EXTRACTOR_VERSION
    max_pdf_pages: int = 200
    max_pdf_text_characters: int = 1_000_000

    def __post_init__(self) -> None:
        if self.pdf_extraction_version != PDF_EXTRACTOR_VERSION:
            raise ExternalSourceError("earnings PDF extraction version is unsupported")
        if self.max_pdf_pages < 1 or self.max_pdf_text_characters < 1_000:
            raise ExternalSourceError("earnings PDF extraction bounds are invalid")


class EarningsDiscoveryAdapter:
    """Discover and archive official PLTR/LMT earnings releases only."""

    def __init__(
        self,
        *,
        client: BoundedHTTPClient,
        archive: ExternalEventArchive,
        palantir_ir: PalantirIRAdapter,
        settings: EarningsSettings | None = None,
        now: Callable[[], datetime] = utc_now,
        enabled: bool = True,
    ) -> None:
        self.client = client
        self.archive = archive
        self.palantir_ir = palantir_ir
        self.settings = settings or EarningsSettings()
        self._now = now
        self._enabled = bool(enabled)

    def discover(
        self,
        *,
        ticker: str,
        max_items: int,
        use_conditional: bool = True,
        checkpoint_suffix: str = "",
    ) -> tuple[HTTPFetchResult, tuple[EarningsPackage, ...]]:
        ticker = ticker.upper()
        if ticker not in {"PLTR", "LMT"}:
            raise ExternalSourceError("earnings ticker must be PLTR or LMT")
        limit = _bounded_limit(max_items, self.settings.max_items)
        url = (
            self.settings.pltr_events_url
            if ticker == "PLTR"
            else self.settings.lmt_results_url
        )
        domains = (
            self.settings.pltr_domains
            if ticker == "PLTR"
            else self.settings.lmt_domains
        )
        checkpoint = self.archive.get_checkpoint(
            f"{self.settings.source}:{ticker}{checkpoint_suffix}"
        ) or {}
        response = self.client.get(
            url,
            conditional_headers=(
                _conditional_headers(checkpoint) if use_conditional else None
            ),
            allowed_domains=domains,
            accepted_content_types=("text/html", "application/xhtml+xml"),
        )
        if response.not_modified:
            _require_conditional_checkpoint(checkpoint)
            return response, ()
        page_hash = self.archive.archive_object(
            response.content,
            extension="html",
            content_type=response.content_type,
        )
        try:
            packages = discover_earnings_packages(
                response.content,
                ticker=ticker,
                base_url=response.final_url,
                official_domains=domains,
            )
            if not packages:
                raise ExternalSourceError(
                    "earnings page returned no provable official release packages"
                )
        except (ExternalSourceError, ExternalNormalizationError):
            _publish_rejected_http_observation(
                self.archive,
                source=self.settings.source,
                response=response,
                raw_object_hash=page_hash,
                stage="EARNINGS_INDEX_PARSE",
                failure_category="PARSER_SCHEMA_DRIFT",
                source_fact_id=ticker,
            )
            raise
        self.archive.publish_observation(
            source=self.settings.source,
            payload={
                "kind": "EARNINGS_INDEX_POLL",
                "ticker": ticker,
                "page_object_hash": page_hash,
                "requested_url": response.requested_url,
                "final_url": response.final_url,
                "system_observed_at": to_utc_iso(response.observed_at),
                "package_count": len(packages),
            },
        )
        return response, packages[:limit]

    def collect_once(
        self,
        *,
        ticker: str,
        max_items: int,
        establish_checkpoint: bool = False,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        backfill: bool = False,
    ) -> SourceCollectionResult:
        ticker = ticker.upper()
        source_key = (
            f"{self.settings.source}:{ticker}"
            f"{':backfill' if backfill else ''}"
        )
        try:
            result = self._collect_once_impl(
                ticker=ticker,
                max_items=max_items,
                establish_checkpoint=establish_checkpoint,
                start_time=start_time,
                end_time=end_time,
                backfill=backfill,
            )
        except Exception as exc:
            _record_health_failure(
                self.archive,
                source=self.settings.source,
                health_key=_health_key(source_key),
                enabled=self._enabled,
                parser_extraction_version=_version_label(
                    self.settings.adapter_version,
                    self.settings.pdf_extraction_version,
                    "lmt_earnings_html_v1",
                ),
                failed_at=self._now(),
                error=exc,
            )
            raise
        _record_health_success(
            self.archive,
            source=self.settings.source,
            collection_checkpoint_key=source_key,
            health_key=_health_key(source_key),
            enabled=self._enabled,
            parser_extraction_version=_version_label(
                self.settings.adapter_version,
                self.settings.pdf_extraction_version,
                "lmt_earnings_html_v1",
            ),
            result=result,
        )
        return result

    def get_health(
        self, *, ticker: str, backfill: bool = False
    ) -> SourceHealthStatus | None:
        ticker = ticker.upper()
        source_key = (
            f"{self.settings.source}:{ticker}"
            f"{':backfill' if backfill else ''}"
        )
        return _load_health(self.archive, _health_key(source_key))

    def _collect_once_impl(
        self,
        *,
        ticker: str,
        max_items: int,
        establish_checkpoint: bool = False,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        backfill: bool = False,
    ) -> SourceCollectionResult:
        ticker = ticker.upper()
        start_time, end_time = _validated_time_range(start_time, end_time)
        if backfill and (start_time is None or end_time is None):
            raise ExternalSourceError(
                "earnings backfill requires explicit start_time and end_time"
            )
        source_key = (
            f"{self.settings.source}:{ticker}"
            f"{':backfill' if backfill else ''}"
        )
        limit = _bounded_limit(max_items, self.settings.max_items)
        response, discovered_packages = self.discover(
            ticker=ticker,
            max_items=self.settings.max_items,
            use_conditional=not backfill,
            checkpoint_suffix=":backfill" if backfill else "",
        )
        packages = discovered_packages
        if backfill:
            assert start_time is not None and end_time is not None
            packages = tuple(
                value
                for value in discovered_packages
                if start_time.year - 1
                <= value.fiscal_year
                <= end_time.year
            )
        checkpoint = self.archive.get_checkpoint(source_key) or {}
        coverage_source = f"{self.settings.source}:{ticker}"
        pending_batches = _load_pending_batches(
            checkpoint, expected_kind="EARNINGS_INDEX"
        )
        if any(batch.ticker != ticker for batch in pending_batches):
            raise ExternalSourceError("earnings pending discovery ticker changed")
        if establish_checkpoint and pending_batches:
            raise ExternalSourceError(
                "earnings checkpoint establishment cannot discard pending discoveries"
            )
        bootstrap_package_ids = _checkpoint_string_ids(
            checkpoint, "bootstrap_package_ids"
        )
        bootstrap_package_hashes = _checkpoint_hash_map(
            checkpoint, "bootstrap_package_hashes"
        )
        _require_bootstrap_hash_version(
            checkpoint,
            hashes=bootstrap_package_hashes,
            version_key="bootstrap_package_hash_version",
            expected=EARNINGS_BOOTSTRAP_HASH_VERSION,
        )
        current_item_ids: tuple[str, ...] = ()
        skipped = 0
        if response.not_modified:
            _require_conditional_checkpoint(checkpoint)
        else:
            current_item_ids = tuple(value.package_id for value in packages)
            if establish_checkpoint:
                for package in packages[:limit]:
                    self._publish_discovered_earnings_package(package)
                    self._publish_package_observation(
                        package,
                        response.observed_at,
                        revision=None,
                        duplicate=False,
                    )
                bootstrap_package_ids = tuple(
                    dict.fromkeys((*bootstrap_package_ids, *current_item_ids))
                )
                bootstrap_package_hashes = {
                    **bootstrap_package_hashes,
                    **{
                        package.package_id: _earnings_package_discovery_hash(
                            package
                        )
                        for package in packages
                    },
                }
                skipped = len(packages)
            else:
                eligible: list[EarningsPackage] = []
                for package in packages:
                    fact_id = f"{package.package_id}:EARNINGS_RELEASE"
                    baseline_hash = bootstrap_package_hashes.get(
                        package.package_id
                    )
                    if (
                        not _fact_exists(
                            self.archive, self.settings.source, fact_id
                        )
                        and (
                            (
                                baseline_hash is not None
                                and baseline_hash
                                == _earnings_package_discovery_hash(package)
                            )
                            or (
                                baseline_hash is None
                                and package.package_id in bootstrap_package_ids
                            )
                        )
                    ):
                        skipped += 1
                        continue
                    eligible.append(package)
                if eligible:
                    pending_batches = _append_pending_batch(
                        pending_batches,
                        _PendingDiscoveryBatch(
                            kind="EARNINGS_INDEX",
                            object_hash=sha256(response.content).hexdigest(),
                            final_url=response.final_url,
                            observed_at=response.observed_at,
                            item_ids=tuple(
                                value.package_id for value in eligible
                            ),
                            ticker=ticker,
                        ),
                    )

        staged_checkpoint = _checkpoint_payload(
            checkpoint,
            response,
            item_ids=current_item_ids,
            extra={
                "ticker": ticker,
                "bootstrap_package_ids": list(bootstrap_package_ids),
                "bootstrap_package_hashes": bootstrap_package_hashes,
                "bootstrap_package_hash_version": EARNINGS_BOOTSTRAP_HASH_VERSION,
                "pending_batches": _pending_batch_payloads(pending_batches),
            },
        )
        generation = self.archive.update_checkpoint(source_key, staged_checkpoint)
        if establish_checkpoint:
            _record_one_shot_live_coverage(
                self.archive,
                source=coverage_source,
                observed_at=response.observed_at,
            )
            return SourceCollectionResult(
                source=self.settings.source,
                observed_at=response.observed_at,
                not_modified=response.not_modified,
                discovered_count=min(len(current_item_ids), limit),
                new_count=0,
                duplicate_count=0,
                revision_count=0,
                skipped_count=skipped,
                checkpoint_generation=generation,
                pending_count=0,
            )

        revision_ids: list[str] = []
        duplicates = 0
        acquired = 0
        remaining_batches: list[_PendingDiscoveryBatch] = []
        for batch_index, batch in enumerate(pending_batches):
            batch_packages = _load_earnings_pending_packages(
                self.archive,
                batch,
                settings=self.settings,
            )
            remaining_ids: list[str] = []
            for package in batch_packages:
                if acquired >= limit:
                    remaining_ids.append(package.package_id)
                    continue
                primary = self._archive_package_release(
                    package,
                    observed_at=batch.observed_at,
                    collection_mode=("BACKFILL" if backfill else "LIVE_SYSTEM"),
                )
                self._publish_package_observation(
                    package,
                    batch.observed_at,
                    revision=primary.revision,
                    duplicate=primary.duplicate,
                )
                self._publish_archived_earnings_package(package, primary)
                if primary.duplicate:
                    duplicates += 1
                else:
                    revision_ids.append(primary.revision.source_revision_id)
                acquired += 1
            remaining = batch.with_item_ids(remaining_ids)
            if remaining is not None:
                remaining_batches.append(remaining)
            if acquired >= limit:
                remaining_batches.extend(pending_batches[batch_index + 1 :])
                break

        final_checkpoint = {
            **staged_checkpoint,
            "pending_batches": _pending_batch_payloads(remaining_batches),
        }
        generation = self.archive.update_checkpoint(
            source_key,
            final_checkpoint,
        )
        if backfill:
            assert start_time is not None and end_time is not None
            _record_partial_backfill_coverage(
                self.archive,
                source=coverage_source,
                start=start_time,
                end=end_time,
                observed_at=response.observed_at,
            )
        else:
            _record_one_shot_live_coverage(
                self.archive,
                source=coverage_source,
                observed_at=response.observed_at,
            )
        return SourceCollectionResult(
            source=self.settings.source,
            observed_at=response.observed_at,
            not_modified=response.not_modified,
            discovered_count=acquired,
            new_count=len(revision_ids),
            duplicate_count=duplicates,
            revision_count=len(revision_ids),
            skipped_count=skipped,
            checkpoint_generation=generation,
            revision_ids=tuple(revision_ids),
            pending_count=_pending_batch_count(remaining_batches),
        )

    def collect_backfill(
        self,
        *,
        ticker: str,
        start_time: datetime,
        end_time: datetime,
        max_items: int,
    ) -> SourceCollectionResult:
        """Archive a bounded conservative fiscal-period candidate set.

        Fiscal-quarter labels aren't publication timestamps, so this operation
        deliberately records PARTIAL coverage and includes the prior fiscal
        year to avoid dropping Q4 results published early in the next year.
        """
        return self.collect_once(
            ticker=ticker,
            max_items=max_items,
            start_time=start_time,
            end_time=end_time,
            backfill=True,
        )

    def _archive_package_release(
        self,
        package: EarningsPackage,
        *,
        observed_at: datetime,
        collection_mode: str,
    ) -> _ArchivedEarningsPrimary:
        fact_id = f"{package.package_id}:EARNINGS_RELEASE"
        if package.ticker == "PLTR":
            release, release_response = self.palantir_ir.find_release_with_response(
                year=package.fiscal_year,
                canonical_url=package.primary_url,
            )
            synthetic_response = HTTPFetchResult(
                requested_url=package.primary_url,
                final_url=release.canonical_url,
                status_code=200,
                headers={"Content-Type": "text/html"},
                content=release.body_html.encode("utf-8"),
                observed_at=release_response.observed_at,
            )
            revision, duplicate = self.palantir_ir.archive_release(
                release,
                response=synthetic_response,
                source=self.settings.source,
                source_fact_id=fact_id,
                earnings_package_id=package.package_id,
                adapter_version=self.settings.adapter_version,
                collection_mode=collection_mode,
            )
            return _ArchivedEarningsPrimary(
                revision=revision,
                duplicate=duplicate,
                content_type="text/html",
            )
        response = self.client.get(
            package.primary_url,
            allowed_domains=self.settings.lmt_domains,
            accepted_content_types=(
                "application/pdf",
                "text/html",
                "application/xhtml+xml",
                "text/plain",
            ),
        )
        if response.content_type == "application/pdf":
            raw_object_hash = self.archive.archive_object(
                response.content,
                extension="pdf",
                content_type=response.content_type,
            )
            try:
                normalized = extract_pdf_text(
                    response.content,
                    max_pages=self.settings.max_pdf_pages,
                    max_characters=self.settings.max_pdf_text_characters,
                )
            except ExternalNormalizationError as exc:
                _publish_rejected_http_observation(
                    self.archive,
                    source=self.settings.source,
                    response=response,
                    raw_object_hash=raw_object_hash,
                    stage="EARNINGS_RELEASE_EXTRACTION",
                    failure_category="EMPTY_OR_INVALID_EXTRACTION",
                    source_fact_id=fact_id,
                )
                raise ExternalSourceError(
                    "earnings PDF extraction failed"
                ) from exc
            extension = "pdf"
            extractor_version = self.settings.pdf_extraction_version
        elif response.content_type == "text/plain":
            raw_object_hash = self.archive.archive_object(
                response.content,
                extension="txt",
                content_type=response.content_type,
            )
            try:
                normalized = response.content.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                _publish_rejected_http_observation(
                    self.archive,
                    source=self.settings.source,
                    response=response,
                    raw_object_hash=raw_object_hash,
                    stage="EARNINGS_RELEASE_EXTRACTION",
                    failure_category="EMPTY_OR_INVALID_EXTRACTION",
                    source_fact_id=fact_id,
                )
                raise ExternalSourceError(
                    "earnings plain-text release is not UTF-8"
                ) from exc
            extension = "txt"
            extractor_version = "external_plain_text_v1"
        else:
            raw_object_hash = self.archive.archive_object(
                response.content,
                extension="html",
                content_type=response.content_type,
            )
            try:
                normalized = extract_article_html(
                    response.content,
                    selectors=self.settings.lmt_article_selectors,
                )
            except ExternalNormalizationError as exc:
                _publish_rejected_http_observation(
                    self.archive,
                    source=self.settings.source,
                    response=response,
                    raw_object_hash=raw_object_hash,
                    stage="EARNINGS_RELEASE_EXTRACTION",
                    failure_category="EMPTY_OR_INVALID_EXTRACTION",
                    source_fact_id=fact_id,
                )
                raise ExternalSourceError(
                    "earnings article extraction failed"
                ) from exc
            extension = "html"
            extractor_version = "lmt_earnings_html_v1"
        if not normalized:
            _publish_rejected_http_observation(
                self.archive,
                source=self.settings.source,
                response=response,
                raw_object_hash=raw_object_hash,
                stage="EARNINGS_RELEASE_EXTRACTION",
                failure_category="EMPTY_OR_INVALID_EXTRACTION",
                source_fact_id=fact_id,
            )
            raise ExternalSourceError("earnings release extraction produced empty text")
        revision, duplicate = _archive_document_revision(
            archive=self.archive,
            source=self.settings.source,
            source_fact_id=fact_id,
            source_type="OFFICIAL_EARNINGS_RELEASE",
            source_platform="LOCKHEED_MARTIN_INVESTOR_RELATIONS",
            source_uri=response.final_url,
            source_title=(
                f"{package.ticker} {package.fiscal_year} Q{package.fiscal_quarter} "
                "Earnings Release"
            ),
            source_published_at=None,
            source_updated_at=None,
            observed_at=response.observed_at,
            raw_content=response.content,
            extension=extension,
            content_type=response.content_type,
            normalized_text=normalized,
            fixed_tickers=("LMT",),
            adapter_version=self.settings.adapter_version,
            extractor_version=extractor_version,
            normalizer_version=(
                self.settings.pdf_extraction_version
                if extension == "pdf"
                else (
                    "external_plain_text_v1"
                    if extension == "txt"
                    else HTML_NORMALIZER_VERSION
                )
            ),
            authoritative_revision_sequence=None,
            archived_at=self._now(),
            correlation_group_id=f"earnings:{package.package_id}",
            relationship_types=("SAME_EARNINGS_OCCURRENCE",),
            earnings_package_id=package.package_id,
            collection_mode=collection_mode,
        )
        return _ArchivedEarningsPrimary(
            revision=revision,
            duplicate=duplicate,
            content_type=response.content_type or "application/octet-stream",
        )

    def _publish_discovered_earnings_package(
        self, package: EarningsPackage
    ) -> None:
        self.archive.publish_earnings_package(
            ticker=package.ticker,
            fiscal_year=package.fiscal_year,
            fiscal_quarter=package.fiscal_quarter,
            payload={
                "package_id": package.package_id,
                "package_state": "DISCOVERED_NOT_ACQUIRED",
                "primary_document": {
                    "role": "EARNINGS_RELEASE",
                    "discovered_url": package.primary_url,
                    "acquired": False,
                },
                "supporting_documents": [
                    {
                        "role": role,
                        "url": url,
                        "acquired": False,
                        "classification_eligible": False,
                    }
                    for url, role in package.supporting_document_roles
                ],
                "supporting_links_acquired": False,
            },
        )

    def _publish_archived_earnings_package(
        self,
        package: EarningsPackage,
        primary: _ArchivedEarningsPrimary,
    ) -> None:
        revision = primary.revision
        self.archive.publish_earnings_package(
            ticker=package.ticker,
            fiscal_year=package.fiscal_year,
            fiscal_quarter=package.fiscal_quarter,
            payload={
                "package_id": package.package_id,
                "package_state": "PRIMARY_ARCHIVED",
                "primary_document": {
                    "role": "EARNINGS_RELEASE",
                    "source_fact_id": revision.source_fact_id,
                    "source_revision_id": revision.source_revision_id,
                    "discovered_url": package.primary_url,
                    "official_url": revision.source_uri,
                    "content_type": primary.content_type,
                    "raw_object_hash": revision.raw_object_hash,
                    "document_hash": revision.document_hash,
                    "normalized_text_hash": revision.normalized_text_hash,
                    "canonical_content_hash": revision.canonical_content_hash,
                    "source_title": revision.source_title,
                    "source_published_at": (
                        None
                        if revision.source_published_at is None
                        else to_utc_iso(revision.source_published_at)
                    ),
                    "system_observed_at": to_utc_iso(
                        revision.system_observed_at
                    ),
                    "source_available_at": (
                        None
                        if revision.source_available_at is None
                        else to_utc_iso(revision.source_available_at)
                    ),
                    "archived_at": to_utc_iso(revision.archived_at),
                    "normalized_at": (
                        None
                        if revision.normalized_at is None
                        else to_utc_iso(revision.normalized_at)
                    ),
                    "collection_mode": revision.collection_mode,
                    "acquired": True,
                    "classification_eligible": True,
                    "lineage": {
                        "source": revision.source,
                        "source_fact_id": revision.source_fact_id,
                        "source_revision_id": revision.source_revision_id,
                        "supersedes_revision_id": revision.supersedes_revision_id,
                        "correlation_group_id": revision.correlation_group_id,
                        "relationship_types": list(revision.relationship_types),
                    },
                },
                "supporting_documents": [
                    {
                        "role": role,
                        "url": url,
                        "acquired": False,
                        "classification_eligible": False,
                    }
                    for url, role in package.supporting_document_roles
                ],
                "supporting_links_acquired": False,
            },
        )

    def _publish_package_observation(
        self,
        package: EarningsPackage,
        observed_at: datetime,
        *,
        revision: ExternalSourceRevision | None,
        duplicate: bool,
    ) -> None:
        self.archive.publish_observation(
            source=self.settings.source,
            payload={
                "kind": "EARNINGS_PACKAGE",
                "package_id": package.package_id,
                "ticker": package.ticker,
                "fiscal_year": package.fiscal_year,
                "fiscal_quarter": package.fiscal_quarter,
                "primary_url": package.primary_url,
                "supporting_urls": list(package.supporting_urls),
                "system_observed_at": to_utc_iso(observed_at),
                "source_revision_id": (
                    None if revision is None else revision.source_revision_id
                ),
                "duplicate": duplicate,
            },
            source_revision_id=(
                None if revision is None else revision.source_revision_id
            ),
            observed_at=observed_at,
        )


def discover_earnings_packages(
    content: bytes,
    *,
    ticker: str,
    base_url: str,
    official_domains: Iterable[str],
) -> tuple[EarningsPackage, ...]:
    ticker = ticker.upper()
    if ticker not in {"PLTR", "LMT"}:
        raise ExternalSourceError("earnings ticker must be PLTR or LMT")
    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
    page_text = " ".join(soup.get_text(" ", strip=True).split()).lower()
    if any(value in page_text for value in ("access denied", "verify you are human", "sign in to continue")):
        raise ExternalSourceError("earnings source returned a challenge or error page")
    labels = (
        ("earnings release",)
        if ticker == "PLTR"
        else ("press release", "earnings release")
    )
    allowed = {value.lower() for value in official_domains}
    found: dict[str, EarningsPackage] = {}
    current_lmt_year: int | None = None
    for text_node in soup.find_all(string=True):
        heading = " ".join(str(text_node).split())
        parent = text_node.parent if isinstance(text_node.parent, Tag) else None
        if parent is None or " ".join(parent.get_text(" ", strip=True).split()) != heading:
            continue
        if ticker == "LMT" and re.fullmatch(r"20\d{2}", heading):
            current_lmt_year = int(heading)
            continue
        period = _parse_quarter_label(heading)
        if ticker == "LMT" and period is None and current_lmt_year is not None:
            quarter = _parse_standalone_quarter(heading)
            if quarter is not None:
                period = (current_lmt_year, quarter)
        if period is None:
            continue
        year, quarter = period
        primary: str | None = None
        supporting: list[str] = []
        supporting_roles: dict[str, str] = {}
        # Associate links by document order until the next explicit period/year
        # boundary.  Ascending to a broad common ancestor can leak Q2's release
        # into a Q1 package when Q1 has no release of its own.
        traversed = 0
        for element in text_node.next_elements:
            traversed += 1
            if traversed > 2_000:
                raise ExternalSourceError(
                    "earnings period link scan exceeded its structural bound"
                )
            if isinstance(element, str):
                boundary_text = " ".join(str(element).split())
                boundary_parent = (
                    element.parent if isinstance(element.parent, Tag) else None
                )
                if (
                    boundary_parent is not None
                    and " ".join(
                        boundary_parent.get_text(" ", strip=True).split()
                    )
                    == boundary_text
                    and _is_earnings_period_boundary(
                        boundary_text,
                        ticker=ticker,
                        current_lmt_year=current_lmt_year,
                    )
                ):
                    break
                continue
            if not isinstance(element, Tag) or element.name != "a":
                continue
            raw_url = str(element.get("href", "")).strip()
            if not raw_url:
                continue
            label = " ".join(
                element.get_text(" ", strip=True).lower().split()
            )
            try:
                url = canonicalize_url(urljoin(base_url, raw_url))
                _require_allowed_domain(url, allowed)
            except (ExternalNormalizationError, ExternalSourceError):
                continue
            if any(value == label or value in label for value in labels):
                if primary is not None and primary != url:
                    raise ExternalSourceError(
                        "earnings period exposes conflicting primary releases"
                    )
                primary = url
            else:
                supporting.append(url)
                supporting_roles[url] = _supporting_document_role(label)
        if primary is None:
            continue
        package = EarningsPackage(
            ticker=ticker,
            fiscal_year=year,
            fiscal_quarter=quarter,
            primary_url=primary,
            supporting_urls=tuple(sorted(set(supporting) - {primary})),
            supporting_document_roles=tuple(
                sorted(
                    (url, role)
                    for url, role in supporting_roles.items()
                    if url != primary
                )
            ),
        )
        existing = found.get(package.package_id)
        if existing is not None and existing.primary_url != primary:
            raise ExternalSourceError(
                "earnings page has conflicting primary releases for one quarter"
            )
        found[package.package_id] = package
    return tuple(
        sorted(
            found.values(),
            key=lambda value: (value.fiscal_year, value.fiscal_quarter),
            reverse=True,
        )
    )


class CollectOnce(Protocol):
    def collect_once(self, *, max_items: int) -> SourceCollectionResult: ...


class BoundedSourcePoller:
    """Explicit polling loop with injected sleep and a required stop bound."""

    def __init__(
        self,
        collector: CollectOnce,
        *,
        poll_interval_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
        coverage_archive: ExternalEventArchive | None = None,
        coverage_source: str | None = None,
        continuity_grace_multiplier: float = 2.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ExternalSourceError("poll interval must be positive")
        self.collector = collector
        self.poll_interval_seconds = poll_interval_seconds
        self._sleeper = sleeper
        inferred_archive = getattr(collector, "archive", None)
        inferred_settings = getattr(collector, "settings", None)
        inferred_source = getattr(inferred_settings, "source", None)
        self._coverage_archive = (
            coverage_archive
            if coverage_archive is not None
            else (
                inferred_archive
                if isinstance(inferred_archive, ExternalEventArchive)
                else None
            )
        )
        self._coverage_source = coverage_source or (
            str(inferred_source) if inferred_source is not None else None
        )
        if continuity_grace_multiplier < 1:
            raise ExternalSourceError(
                "poll continuity grace multiplier must be at least one"
            )
        self._continuity_bound = (
            poll_interval_seconds * continuity_grace_multiplier
        )

    def run(
        self,
        *,
        max_items: int,
        max_polls: int | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[SourceCollectionResult, ...]:
        if max_polls is None and should_stop is None:
            raise ExternalSourceError(
                "an unbounded poll loop requires an explicit stop callback"
            )
        if max_polls is not None and max_polls < 1:
            raise ExternalSourceError("max_polls must be positive")
        results: list[SourceCollectionResult] = []
        previous_observed_at: datetime | None = None
        while max_polls is None or len(results) < max_polls:
            if should_stop is not None and should_stop():
                break
            result = self.collector.collect_once(max_items=max_items)
            results.append(result)
            if (
                self._coverage_archive is not None
                and self._coverage_source is not None
            ):
                _extend_continuous_poll_coverage(
                    self._coverage_archive,
                    source=self._coverage_source,
                    previous_observed_at=previous_observed_at,
                    observed_at=result.observed_at,
                    continuity_bound_seconds=self._continuity_bound,
                )
            previous_observed_at = result.observed_at
            if max_polls is not None and len(results) >= max_polls:
                break
            if should_stop is not None and should_stop():
                break
            self._sleeper(self.poll_interval_seconds)
        return tuple(results)


def _archive_document_revision(
    *,
    archive: ExternalEventArchive,
    source: str,
    source_fact_id: str,
    source_type: str,
    source_platform: str,
    source_uri: str,
    source_title: str | None,
    source_published_at: datetime | None,
    source_updated_at: datetime | None,
    observed_at: datetime,
    raw_content: bytes,
    extension: str,
    content_type: str | None,
    normalized_text: str,
    fixed_tickers: Iterable[str],
    adapter_version: str,
    extractor_version: str,
    normalizer_version: str,
    authoritative_revision_sequence: int | None,
    archived_at: datetime,
    correlation_group_id: str | None = None,
    relationship_types: Iterable[str] = (),
    earnings_package_id: str | None = None,
    collection_mode: str = "LIVE_SYSTEM",
) -> tuple[ExternalSourceRevision, bool]:
    observed_at = ensure_timezone_aware_utc(observed_at)
    raw_hash = archive.archive_object(
        raw_content, extension=extension, content_type=content_type
    )
    normalized_hash = archive.archive_normalized_text(normalized_text)
    canonical_hash = sha256(
        json.dumps(
            {
                "raw_object_hash": raw_hash,
                "normalized_text_hash": normalized_hash,
                "source_uri": canonicalize_url(source_uri),
                "source_title": source_title,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    existing = sorted(
        (
            value
            for value in archive.iter_revisions(sources=(source,))
            if value.source_fact_id == source_fact_id
        ),
        key=lambda value: (value.revision_sequence, value.system_observed_at),
    )
    current = _current_source_revision(existing)
    current_is_exact = (
        current is not None
        and current.canonical_content_hash == canonical_hash
        and current.lifecycle_state
        in {LifecycleState.ACTIVE, LifecycleState.UPDATED}
    )
    lifecycle = LifecycleState.ACTIVE if current is None else LifecycleState.UPDATED
    if authoritative_revision_sequence is None:
        if current_is_exact:
            return current, True
        sequence = 1 if current is None else current.revision_sequence + 1
    else:
        if (
            isinstance(authoritative_revision_sequence, bool)
            or not isinstance(authoritative_revision_sequence, int)
            or authoritative_revision_sequence < 1
        ):
            raise ExternalSourceError("authoritative revision sequence is invalid")
        sequence = authoritative_revision_sequence
        if current is not None:
            if sequence < current.revision_sequence:
                raise ExternalSourceError(
                    "authoritative revision sequence is nonmonotonic"
                )
            if sequence == current.revision_sequence:
                if current_is_exact:
                    return current, True
                raise ExternalSourceError(
                    "authoritative revision sequence conflicts with current content"
                )
    declared_available_at = (
        source_published_at if current is None else source_updated_at
    )
    revision_available_at = (
        observed_at
        if declared_available_at is None
        else min(ensure_timezone_aware_utc(declared_available_at), observed_at)
    )
    if current is not None:
        # A later lifecycle revision cannot become effective before the current
        # lifecycle head.  The collector's observation remains the upper bound,
        # so a source clock that is ahead cannot defer bytes already fetched and
        # a stale/coarse source timestamp cannot move lifecycle state backwards.
        revision_available_at = max(
            revision_available_at,
            current.lifecycle_effective_at,
        )
    lifecycle_at = revision_available_at
    revision_id = _ordered_source_revision_id(
        source=source,
        source_fact_id=source_fact_id,
        canonical_content_hash=canonical_hash,
        lifecycle_state=lifecycle,
        adapter_version=adapter_version,
        revision_sequence=sequence,
        supersedes_revision_id=(
            None if current is None else current.source_revision_id
        ),
    )
    archived_at = ensure_timezone_aware_utc(archived_at)
    revision = ExternalSourceRevision(
        source=source,
        source_fact_id=source_fact_id,
        source_revision_id=revision_id,
        revision_sequence=sequence,
        supersedes_revision_id=(
            None if current is None else current.source_revision_id
        ),
        lifecycle_state=lifecycle,
        lifecycle_effective_at=lifecycle_at,
        system_observed_at=observed_at,
        source_available_at=revision_available_at,
        archived_at=archived_at,
        raw_object_hash=raw_hash,
        document_hash=raw_hash,
        normalized_text_hash=normalized_hash,
        canonical_content_hash=canonical_hash,
        source_type=source_type,
        source_platform=source_platform,
        source_uri=canonicalize_url(source_uri),
        source_title=source_title,
        source_published_at=source_published_at,
        source_updated_at=source_updated_at,
        affected_tickers=tuple(fixed_tickers),
        correlation_group_id=correlation_group_id,
        relationship_types=tuple(relationship_types),
        earnings_package_id=earnings_package_id,
        collection_mode=collection_mode,
        adapter_version=adapter_version,
        extractor_version=extractor_version,
        normalizer_version=normalizer_version,
    )
    revision = archive.publish_revision(revision)
    return revision, False


def _current_source_revision(
    revisions: Iterable[ExternalSourceRevision],
) -> ExternalSourceRevision | None:
    values = tuple(revisions)
    if not values:
        return None
    sequences: dict[int, str] = {}
    for revision in values:
        prior = sequences.get(revision.revision_sequence)
        if prior is not None and prior != revision.source_revision_id:
            raise ExternalSourceError("source lifecycle revision order is ambiguous")
        sequences[revision.revision_sequence] = revision.source_revision_id
    return max(
        values,
        key=lambda value: (
            value.revision_sequence,
            value.system_observed_at,
            value.source_revision_id,
        ),
    )


def _ordered_source_revision_id(
    *,
    source: str,
    source_fact_id: str,
    canonical_content_hash: str,
    lifecycle_state: LifecycleState,
    adapter_version: str,
    revision_sequence: int,
    supersedes_revision_id: str | None,
) -> str:
    """Bind immutable identity to one position in the lifecycle chain."""

    content_identity = source_revision_id(
        source=source,
        source_fact_id=source_fact_id,
        canonical_content_hash=canonical_content_hash,
        lifecycle_state=lifecycle_state,
        adapter_version=adapter_version,
    )
    return "revision_" + sha256(
        json.dumps(
            {
                "content_identity": content_identity,
                "revision_sequence": revision_sequence,
                "supersedes_revision_id": supersedes_revision_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _parse_rss_item(value: ET.Element, *, base_url: str) -> FeedItem:
    title = _child_text(value, "title")
    raw_url = _child_text(value, "link")
    guid = _child_text(value, "guid", required=False)
    if not title or not raw_url:
        raise ExternalSourceError("RSS item is missing title or link")
    url = canonicalize_url(urljoin(base_url, raw_url))
    identity = guid.strip() if guid and guid.strip() else url
    published = _parse_feed_datetime(_child_text(value, "pubDate", required=False))
    updated = _parse_feed_datetime(_child_text(value, "updated", required=False))
    return FeedItem(
        identity=identity,
        title=title.strip(),
        url=url,
        published_at=published,
        updated_at=updated,
        guid=None if not guid else guid.strip(),
        description=_child_text(value, "description", required=False),
    )


def _parse_atom_entry(value: ET.Element, *, base_url: str) -> FeedItem:
    title = _child_text(value, "title")
    identity = _child_text(value, "id")
    raw_url: str | None = None
    for child in value:
        if _local_name(child.tag) != "link":
            continue
        rel = str(child.attrib.get("rel", "alternate")).lower()
        href = child.attrib.get("href")
        if rel == "alternate" and isinstance(href, str) and href.strip():
            raw_url = href
            break
    if not title or not identity or not raw_url:
        raise ExternalSourceError("Atom entry is missing title, ID, or link")
    return FeedItem(
        identity=identity.strip(),
        title=title.strip(),
        url=canonicalize_url(urljoin(base_url, raw_url)),
        published_at=_parse_feed_datetime(
            _child_text(value, "published", required=False)
        ),
        updated_at=_parse_feed_datetime(
            _child_text(value, "updated", required=False)
        ),
        guid=identity.strip(),
        description=_child_text(value, "summary", required=False),
    )


def _child_text(
    value: ET.Element, name: str, *, required: bool = True
) -> str | None:
    for child in value:
        if _local_name(child.tag) == name:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    if required:
        raise ExternalSourceError(f"feed item is missing required {name}")
    return None


def _parse_feed_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            return parse_utc_iso(value)
        except ValueError as exc:
            raise ExternalSourceError("feed timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ExternalSourceError("feed timestamp has no timezone")
    return parsed.astimezone(UTC)


def _parse_palantir_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExternalSourceError("Palantir release timestamp is invalid")
    text = value.strip()
    try:
        return parse_utc_iso(text)
    except ValueError:
        try:
            local = datetime.strptime(text, "%m/%d/%Y %H:%M:%S").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
        except ValueError as exc:
            raise ExternalSourceError(
                "Palantir release timestamp format changed"
            ) from exc
        return local.astimezone(UTC)


def _parse_quarter_label(value: str) -> tuple[int, int] | None:
    normalized = " ".join(value.split())
    suffix = r"(?:\s+(?:earnings(?:\s+(?:results|materials))?|results))?"
    match = re.fullmatch(
        rf"Q([1-4])\s+(20\d{{2}}){suffix}",
        normalized,
        re.IGNORECASE,
    )
    if match is not None:
        return int(match.group(2)), int(match.group(1))
    names = {"first": 1, "second": 2, "third": 3, "fourth": 4}
    match = re.fullmatch(
        rf"(first|second|third|fourth)\s+quarter\s+(20\d{{2}}){suffix}",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return int(match.group(2)), names[match.group(1).lower()]


def _parse_standalone_quarter(value: str) -> int | None:
    match = re.fullmatch(r"Q([1-4])", " ".join(value.split()), re.IGNORECASE)
    return None if match is None else int(match.group(1))


def _is_earnings_period_boundary(
    value: str,
    *,
    ticker: str,
    current_lmt_year: int | None,
) -> bool:
    if _parse_quarter_label(value) is not None:
        return True
    if ticker == "LMT":
        if re.fullmatch(r"20\d{2}", value):
            return True
        if current_lmt_year is not None and _parse_standalone_quarter(value) is not None:
            return True
    return False


def _palantir_release_url_key(value: str) -> str:
    """Match the two official release URL forms exposed by Palantir's page/feed."""

    canonical = canonicalize_url(value)
    suffix = "/default.aspx"
    if canonical.lower().endswith(suffix):
        return canonical[: -len(suffix)]
    return canonical


def _supporting_document_role(label: str) -> str:
    normalized = " ".join(label.lower().split())
    if "webcast" in normalized:
        return "WEBCAST"
    if "podcast" in normalized or "audio" in normalized:
        return "AUDIO"
    if "presentation" in normalized or "slides" in normalized:
        return "PRESENTATION"
    if "letter" in normalized:
        return "SHAREHOLDER_LETTER"
    if "annual report" in normalized or "quarterly report" in normalized:
        return "REGULATORY_REPORT"
    if "table" in normalized or "excel" in normalized:
        return "FINANCIAL_TABLES"
    return "SUPPORTING_DOCUMENT"


def _required_pending_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalSourceError("pending discovery item ID is invalid")
    normalized = value.strip()
    if len(normalized) > 4096:
        raise ExternalSourceError("pending discovery item ID is too long")
    return normalized


def _checkpoint_string_ids(
    checkpoint: Mapping[str, Any], name: str
) -> tuple[str, ...]:
    value = checkpoint.get(name, [])
    if not isinstance(value, list):
        raise ExternalSourceError(f"source checkpoint {name} has invalid shape")
    return tuple(dict.fromkeys(_required_pending_id(item) for item in value))


def _load_pending_batches(
    checkpoint: Mapping[str, Any], *, expected_kind: str
) -> list[_PendingDiscoveryBatch]:
    raw = checkpoint.get("pending_batches", [])
    if not isinstance(raw, list):
        raise ExternalSourceError("source checkpoint pending batches have invalid shape")
    if len(raw) > 1_000:
        raise ExternalSourceError("source checkpoint has too many pending batches")
    batches: list[_PendingDiscoveryBatch] = []
    total_items = 0
    for value in raw:
        if not isinstance(value, Mapping):
            raise ExternalSourceError("source checkpoint pending batch is invalid")
        batch = _PendingDiscoveryBatch.from_payload(value)
        if batch.kind != expected_kind:
            raise ExternalSourceError("source checkpoint pending batch kind changed")
        total_items += len(batch.item_ids)
        if total_items > 10_000:
            raise ExternalSourceError("source checkpoint has too many pending items")
        batches.append(batch)
    return batches


def _append_pending_batch(
    batches: Sequence[_PendingDiscoveryBatch],
    incoming: _PendingDiscoveryBatch,
) -> list[_PendingDiscoveryBatch]:
    result = list(batches)
    for index, current in enumerate(result):
        if (
            current.kind,
            current.object_hash,
            current.final_url,
            current.year,
            current.ticker,
        ) != (
            incoming.kind,
            incoming.object_hash,
            incoming.final_url,
            incoming.year,
            incoming.ticker,
        ):
            continue
        # The existing IDs are the not-yet-acquired subset of this exact raw
        # response.  Re-unioning the full incoming set would requeue items
        # already completed on an earlier bounded call and could starve the
        # remaining records forever when a source does not provide validators.
        return result
    result.append(incoming)
    return result


def _pending_batch_payloads(
    batches: Iterable[_PendingDiscoveryBatch],
) -> list[dict[str, Any]]:
    return [value.to_payload() for value in batches]


def _pending_batch_count(batches: Iterable[_PendingDiscoveryBatch]) -> int:
    return sum(len(value.item_ids) for value in batches)


def _load_lmt_pending_items(
    archive: ExternalEventArchive,
    batch: _PendingDiscoveryBatch,
    *,
    settings: LockheedMartinRSSSettings,
) -> tuple[FeedItem, ...]:
    if batch.kind != "LMT_FEED":
        raise ExternalSourceError("pending LMT feed batch kind changed")
    _require_allowed_domain(batch.final_url, set(settings.official_domains))
    content = archive.read_object(batch.object_hash, filename="original.xml")
    parsed = parse_feed(
        content,
        max_items=settings.max_feed_items,
        base_url=batch.final_url,
    )
    values = _unique_pending_items(
        parsed.items,
        identity=lambda value: value.identity,
        source_name="LMT feed",
    )
    return tuple(
        _required_pending_item(values, item_id, source_name="LMT feed")
        for item_id in batch.item_ids
    )


def _load_pltr_pending_releases(
    archive: ExternalEventArchive,
    batch: _PendingDiscoveryBatch,
    *,
    settings: PalantirIRSettings,
) -> tuple[tuple[PalantirRelease, ...], HTTPFetchResult]:
    if batch.kind != "PLTR_RELEASE_LIST" or batch.year is None:
        raise ExternalSourceError("pending Palantir release batch is incomplete")
    _require_allowed_domain(batch.final_url, set(settings.official_domains))
    content = archive.read_object(batch.object_hash, filename="original.json")
    releases = parse_palantir_release_list(
        content,
        official_domains=settings.official_domains,
        max_items=settings.max_items,
    )
    values = _unique_pending_items(
        releases,
        identity=lambda value: value.press_release_id,
        source_name="Palantir release list",
    )
    selected = tuple(
        _required_pending_item(
            values, item_id, source_name="Palantir release list"
        )
        for item_id in batch.item_ids
    )
    return (
        selected,
        HTTPFetchResult(
            requested_url=batch.final_url,
            final_url=batch.final_url,
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=content,
            observed_at=batch.observed_at,
        ),
    )


def _load_earnings_pending_packages(
    archive: ExternalEventArchive,
    batch: _PendingDiscoveryBatch,
    *,
    settings: EarningsSettings,
) -> tuple[EarningsPackage, ...]:
    if batch.kind != "EARNINGS_INDEX" or batch.ticker is None:
        raise ExternalSourceError("pending earnings batch is incomplete")
    domains = (
        settings.pltr_domains if batch.ticker == "PLTR" else settings.lmt_domains
    )
    _require_allowed_domain(batch.final_url, set(domains))
    content = archive.read_object(batch.object_hash, filename="original.html")
    packages = discover_earnings_packages(
        content,
        ticker=batch.ticker,
        base_url=batch.final_url,
        official_domains=domains,
    )
    values = _unique_pending_items(
        packages,
        identity=lambda value: value.package_id,
        source_name="earnings index",
    )
    return tuple(
        _required_pending_item(values, item_id, source_name="earnings index")
        for item_id in batch.item_ids
    )


def _unique_pending_items(
    values: Iterable[Any],
    *,
    identity: Callable[[Any], str],
    source_name: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        item_id = _required_pending_id(identity(value))
        if item_id in result:
            raise ExternalSourceError(
                f"{source_name} repeated a pending source identity"
            )
        result[item_id] = value
    return result


def _required_pending_item(
    values: Mapping[str, Any], item_id: str, *, source_name: str
) -> Any:
    try:
        return values[item_id]
    except KeyError as exc:
        raise ExternalSourceError(
            f"{source_name} no longer contains a pending source identity"
        ) from exc


def _hash_payload(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _feed_item_discovery_hash(item: FeedItem) -> str:
    """Hash immutable feed claims, excluding local observation timestamps."""

    return _hash_payload(
        {
            "identity": item.identity,
            "title": item.title,
            "url": item.url,
            "published_at": _optional_utc_iso(item.published_at),
            "updated_at": _optional_utc_iso(item.updated_at),
            "description_hash": sha256(
                (item.description or "").encode("utf-8")
            ).hexdigest(),
            "version": LMT_BOOTSTRAP_HASH_VERSION,
        }
    )


def _palantir_release_discovery_hash(release: PalantirRelease) -> str:
    """Hash the exact official release fields available at discovery time."""

    return _hash_payload(
        {
            "press_release_id": release.press_release_id,
            "revision_number": release.revision_number,
            "headline": release.headline,
            "canonical_url": release.canonical_url,
            "published_at": to_utc_iso(release.published_at),
            "body_hash": sha256(release.body_html.encode("utf-8")).hexdigest(),
            "version": PLTR_BOOTSTRAP_HASH_VERSION,
        }
    )


def _earnings_package_discovery_hash(package: EarningsPackage) -> str:
    """Hash one package's discovered official links without fetching them."""

    return _hash_payload(
        {
            "package_id": package.package_id,
            "primary_url": package.primary_url,
            "supporting_document_roles": [
                [url, role]
                for url, role in sorted(package.supporting_document_roles)
            ],
            "version": EARNINGS_BOOTSTRAP_HASH_VERSION,
        }
    )


def _checkpoint_hash_map(
    checkpoint: Mapping[str, Any], name: str
) -> dict[str, str]:
    raw = checkpoint.get(name, {})
    if not isinstance(raw, Mapping):
        raise ExternalSourceError("source checkpoint bootstrap hashes are invalid")
    if len(raw) > 10_000:
        raise ExternalSourceError("source checkpoint has too many bootstrap hashes")
    result: dict[str, str] = {}
    for identity, digest in raw.items():
        normalized_identity = _required_pending_id(identity)
        normalized_digest = str(digest)
        if re.fullmatch(r"[0-9a-f]{64}", normalized_digest) is None:
            raise ExternalSourceError("source checkpoint bootstrap hash is invalid")
        result[normalized_identity] = normalized_digest
    return result


def _require_bootstrap_hash_version(
    checkpoint: Mapping[str, Any],
    *,
    hashes: Mapping[str, str],
    version_key: str,
    expected: str,
) -> None:
    actual = checkpoint.get(version_key)
    if hashes and actual != expected:
        raise ExternalSourceError("source checkpoint bootstrap hash version changed")
    if actual is not None and actual != expected:
        raise ExternalSourceError("source checkpoint bootstrap hash version changed")


def _publish_rejected_http_observation(
    archive: ExternalEventArchive,
    *,
    source: str,
    response: HTTPFetchResult,
    raw_object_hash: str,
    stage: str,
    failure_category: str,
    source_fact_id: str | None = None,
) -> str:
    """Publish body-free failure metadata pointing at immutable raw bytes."""

    if re.fullmatch(r"[0-9a-f]{64}", raw_object_hash) is None:
        raise ExternalSourceError("rejected HTTP object hash is invalid")
    return archive.publish_observation(
        source=source,
        payload={
            "kind": "REJECTED_HTTP_OBSERVATION",
            "stage": stage,
            "failure_category": failure_category,
            "raw_object_hash": raw_object_hash,
            "source_fact_id": source_fact_id,
            "requested_url": response.requested_url,
            "final_url": response.final_url,
            "http_status": response.status_code,
            "content_type": response.content_type,
            "system_observed_at": to_utc_iso(response.observed_at),
        },
    )


def _health_key(collection_checkpoint_key: str) -> str:
    return f"health:{collection_checkpoint_key}"


def _version_label(*values: str) -> str:
    return "+".join(value for value in values if value)


def _optional_utc_iso(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _load_health(
    archive: ExternalEventArchive, health_key: str
) -> SourceHealthStatus | None:
    payload = archive.get_checkpoint(health_key)
    return None if payload is None else SourceHealthStatus.from_payload(payload)


def _default_health(
    *, source: str, enabled: bool, parser_extraction_version: str
) -> SourceHealthStatus:
    return SourceHealthStatus(
        source=source,
        enabled=enabled,
        last_successful_poll_at=None,
        last_source_record_identity=None,
        last_system_receipt_at=None,
        last_source_published_at=None,
        last_failure_at=None,
        failure_category=None,
        consecutive_failure_count=0,
        duplicate_count=0,
        new_record_count=0,
        pending_classification_count=0,
        completed_classification_count=0,
        parser_extraction_version=parser_extraction_version,
        checkpoint_generation=0,
    )


def _classification_health_counts(
    archive: ExternalEventArchive,
    *,
    source: str,
    collection_checkpoint_key: str,
) -> tuple[int, int]:
    revisions = _health_revisions(
        archive,
        source=source,
        collection_checkpoint_key=collection_checkpoint_key,
    )
    completed = 0
    for revision in revisions:
        readiness_values = archive.iter_readiness(revision.source_revision_id)
        if any(
            value.get("classification_status") in {"VALID", "ABSTAINED"}
            for value in readiness_values
        ):
            completed += 1
    return len(revisions) - completed, completed


def _health_revisions(
    archive: ExternalEventArchive,
    *,
    source: str,
    collection_checkpoint_key: str,
) -> tuple[ExternalSourceRevision, ...]:
    expected_mode = (
        "BACKFILL"
        if collection_checkpoint_key.endswith(":backfill")
        else "LIVE_SYSTEM"
    )
    revisions = tuple(
        value
        for value in archive.iter_revisions(sources=(source,))
        if value.collection_mode == expected_mode
    )
    parts = collection_checkpoint_key.split(":")
    if source == EARNINGS_SOURCE and len(parts) >= 2:
        ticker = parts[1].upper()
        revisions = tuple(
            value for value in revisions if ticker in value.affected_tickers
        )
    elif source == PLTR_SOURCE and len(parts) >= 2 and parts[1].isdigit():
        year = int(parts[1])
        revisions = tuple(
            value
            for value in revisions
            if value.source_published_at is not None
            and value.source_published_at.year == year
        )
    return revisions


def _latest_source_health_identity(
    archive: ExternalEventArchive,
    *,
    source: str,
    collection_checkpoint_key: str,
    fallback_observed_at: datetime,
) -> tuple[str | None, datetime | None, datetime | None]:
    revisions = _health_revisions(
        archive,
        source=source,
        collection_checkpoint_key=collection_checkpoint_key,
    )
    if revisions:
        latest = max(
            revisions,
            key=lambda value: (
                value.system_observed_at,
                value.revision_sequence,
                value.source_revision_id,
            ),
        )
        return (
            latest.source_fact_id,
            latest.system_observed_at,
            latest.source_published_at,
        )
    checkpoint = archive.get_checkpoint(collection_checkpoint_key) or {}
    raw_ids = checkpoint.get("item_ids", [])
    identity = (
        str(raw_ids[-1])
        if isinstance(raw_ids, list) and raw_ids
        else None
    )
    published = _parse_optional_timestamp(checkpoint.get("bootstrap_cutoff"))
    return identity, fallback_observed_at, published


def _record_health_success(
    archive: ExternalEventArchive,
    *,
    source: str,
    collection_checkpoint_key: str,
    health_key: str,
    enabled: bool,
    parser_extraction_version: str,
    result: SourceCollectionResult,
) -> None:
    previous = _load_health(archive, health_key) or _default_health(
        source=source,
        enabled=enabled,
        parser_extraction_version=parser_extraction_version,
    )
    pending, completed = _classification_health_counts(
        archive,
        source=source,
        collection_checkpoint_key=collection_checkpoint_key,
    )
    identity, receipt_at, published_at = _latest_source_health_identity(
        archive,
        source=source,
        collection_checkpoint_key=collection_checkpoint_key,
        fallback_observed_at=result.observed_at,
    )
    status = SourceHealthStatus(
        source=source,
        enabled=enabled,
        last_successful_poll_at=result.observed_at,
        last_source_record_identity=identity,
        last_system_receipt_at=receipt_at,
        last_source_published_at=published_at,
        last_failure_at=previous.last_failure_at,
        failure_category=None,
        consecutive_failure_count=0,
        duplicate_count=previous.duplicate_count + result.duplicate_count,
        new_record_count=previous.new_record_count + result.new_count,
        pending_classification_count=pending,
        completed_classification_count=completed,
        parser_extraction_version=parser_extraction_version,
        checkpoint_generation=result.checkpoint_generation,
    )
    archive.update_checkpoint(health_key, status.to_payload())


def _record_health_failure(
    archive: ExternalEventArchive,
    *,
    source: str,
    health_key: str,
    collection_checkpoint_key: str | None = None,
    enabled: bool,
    parser_extraction_version: str,
    failed_at: datetime,
    error: Exception,
) -> None:
    previous = _load_health(archive, health_key) or _default_health(
        source=source,
        enabled=enabled,
        parser_extraction_version=parser_extraction_version,
    )
    pending, completed = _classification_health_counts(
        archive,
        source=source,
        collection_checkpoint_key=(
            collection_checkpoint_key
            if collection_checkpoint_key is not None
            else health_key.removeprefix("health:")
        ),
    )
    status = SourceHealthStatus(
        source=source,
        enabled=enabled,
        last_successful_poll_at=previous.last_successful_poll_at,
        last_source_record_identity=previous.last_source_record_identity,
        last_system_receipt_at=previous.last_system_receipt_at,
        last_source_published_at=previous.last_source_published_at,
        last_failure_at=ensure_timezone_aware_utc(failed_at),
        failure_category=_safe_failure_category(error),
        consecutive_failure_count=previous.consecutive_failure_count + 1,
        duplicate_count=previous.duplicate_count,
        new_record_count=previous.new_record_count,
        pending_classification_count=pending,
        completed_classification_count=completed,
        parser_extraction_version=parser_extraction_version,
        checkpoint_generation=previous.checkpoint_generation,
    )
    archive.update_checkpoint(health_key, status.to_payload())


def _safe_failure_category(error: Exception) -> str:
    if isinstance(error, requests.Timeout):
        return "HTTP_TIMEOUT"
    if isinstance(error, requests.ConnectionError):
        return "HTTP_CONNECTION"
    if isinstance(error, ExternalNormalizationError):
        return "EXTRACTION_FAILURE"
    if isinstance(error, ExternalSourceError):
        message = str(error).lower()
        if any(
            marker in message
            for marker in ("schema", "parse", "malformed", "zero results", "root")
        ):
            return "PARSER_SCHEMA_DRIFT"
        if any(marker in message for marker in ("extract", "empty", "utf-8")):
            return "EXTRACTION_FAILURE"
        if "timeout" in message:
            return "HTTP_TIMEOUT"
        return "SOURCE_VALIDATION_FAILURE"
    return "SOURCE_FAILURE"


def _conditional_headers(checkpoint: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    if checkpoint.get("etag"):
        result["If-None-Match"] = str(checkpoint["etag"])
    if checkpoint.get("last_modified"):
        result["If-Modified-Since"] = str(checkpoint["last_modified"])
    return result


def _require_conditional_checkpoint(checkpoint: Mapping[str, Any]) -> None:
    if not checkpoint.get("etag") and not checkpoint.get("last_modified"):
        raise ExternalSourceError(
            "external source returned HTTP 304 without conditional checkpoint state"
        )


def _checkpoint_payload(
    previous: Mapping[str, Any],
    response: HTTPFetchResult,
    *,
    item_ids: Iterable[str],
    bootstrap_cutoff: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(previous)
    payload.update({
        "etag": _header(response.headers, "ETag") or previous.get("etag"),
        "last_modified": _header(response.headers, "Last-Modified")
        or previous.get("last_modified"),
        "last_poll_at": to_utc_iso(response.observed_at),
        "last_status": response.status_code,
    })
    normalized_ids = sorted(set(item_ids))
    if normalized_ids or response.status_code != 304 or "item_ids" not in payload:
        payload["item_ids"] = normalized_ids
    if bootstrap_cutoff is not None:
        payload["bootstrap_cutoff"] = to_utc_iso(bootstrap_cutoff)
    elif previous.get("bootstrap_cutoff") is not None:
        payload["bootstrap_cutoff"] = previous["bootstrap_cutoff"]
    if extra:
        payload.update(extra)
    return payload


def _discovery_observation(item: FeedItem, observed_at: datetime) -> dict[str, Any]:
    return {
        "kind": "FEED_ITEM_DISCOVERY",
        "source_fact_id": item.identity,
        "canonical_url": item.url,
        "source_published_at": (
            None if item.published_at is None else to_utc_iso(item.published_at)
        ),
        "source_updated_at": (
            None if item.updated_at is None else to_utc_iso(item.updated_at)
        ),
        "system_observed_at": to_utc_iso(observed_at),
    }


def _fact_exists(archive: ExternalEventArchive, source: str, fact_id: str) -> bool:
    return any(
        value.source_fact_id == fact_id
        for value in archive.iter_revisions(sources=(source,))
    )


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _require_allowed_domain(url: str, allowed: set[str]) -> None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host not in allowed:
        raise ExternalSourceError("external URL leaves its reviewed official domain")


def _canonical_source_url(url: str) -> str:
    """Normalize a transport URL while keeping source errors at one boundary."""

    try:
        return canonicalize_url(url)
    except ExternalNormalizationError as exc:
        raise ExternalSourceError(
            "external URL is invalid or contains credentials"
        ) from exc


def _require_safe_source_url(url: str, allowed: set[str]) -> None:
    parts = urlsplit(url)
    _require_allowed_domain(url, allowed)
    if parts.username is not None or parts.password is not None:
        raise ExternalSourceError("external URL must not contain user credentials")
    sensitive_markers = (
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    )
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        normalized = key.lower().replace("-", "_")
        if any(marker in normalized for marker in sensitive_markers):
            raise ExternalSourceError(
                "external URL must not contain credential query parameters"
            )


def _retry_after_seconds(
    headers: Mapping[str, Any], *, now: datetime
) -> float | None:
    value = _header(headers, "Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(
                0.0,
                (target.astimezone(UTC) - ensure_timezone_aware_utc(now)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def _bounded_limit(requested: int, configured: int) -> int:
    if requested < 1:
        raise ExternalSourceError("source item limit must be positive")
    return min(requested, configured)


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_utc_iso(str(value))
    except ValueError as exc:
        raise ExternalSourceError("source checkpoint timestamp is invalid") from exc


def _validated_time_range(
    start_time: datetime | None, end_time: datetime | None
) -> tuple[datetime | None, datetime | None]:
    start = (
        None
        if start_time is None
        else ensure_timezone_aware_utc(start_time)
    )
    end = None if end_time is None else ensure_timezone_aware_utc(end_time)
    if start is not None and end is not None and end < start:
        raise ExternalSourceError("source time range end precedes start")
    return start, end


def _timestamp_in_range(
    value: datetime,
    *,
    start_time: datetime | None,
    end_time: datetime | None,
) -> bool:
    value = ensure_timezone_aware_utc(value)
    return not (
        (start_time is not None and value < start_time)
        or (end_time is not None and value > end_time)
    )


def _filter_feed_items(
    values: Iterable[FeedItem],
    *,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[FeedItem, ...]:
    result: list[FeedItem] = []
    for value in values:
        if start_time is None and end_time is None:
            result.append(value)
            continue
        if value.published_at is None:
            raise ExternalSourceError(
                "bounded feed selection requires item publication timestamps"
            )
        if _timestamp_in_range(
            value.published_at,
            start_time=start_time,
            end_time=end_time,
        ):
            result.append(value)
    return tuple(result)


def _record_one_shot_live_coverage(
    archive: ExternalEventArchive,
    *,
    source: str,
    observed_at: datetime,
) -> None:
    """Record a verified point without bridging separate one-shot runs."""
    observed_at = ensure_timezone_aware_utc(observed_at)
    current = archive.load_coverage(source)
    if current is None:
        archive.save_coverage(
            SourceCoverage(
                source=source,
                coverage_start=observed_at,
                coverage_end=observed_at,
                coverage_status=CoverageStatus.LIVE_ONLY,
                bootstrap_time=observed_at,
                live_collection_start=observed_at,
                last_verification_time=observed_at,
                coverage_generation=1,
            )
        )
        return
    archive.save_coverage(
        SourceCoverage(
            source=source,
            coverage_start=current.coverage_start,
            coverage_end=current.coverage_end,
            coverage_status=current.coverage_status,
            known_gaps=current.known_gaps,
            bootstrap_time=current.bootstrap_time,
            completed_backfill_ranges=current.completed_backfill_ranges,
            live_collection_start=current.live_collection_start,
            last_verification_time=observed_at,
            coverage_generation=current.coverage_generation + 1,
            coverage_version=current.coverage_version,
        )
    )


def _record_partial_backfill_coverage(
    archive: ExternalEventArchive,
    *,
    source: str,
    start: datetime,
    end: datetime,
    observed_at: datetime,
) -> None:
    """Record bounded work without claiming a feed/index was historically complete."""
    start = ensure_timezone_aware_utc(start)
    end = ensure_timezone_aware_utc(end)
    observed_at = ensure_timezone_aware_utc(observed_at)
    current = archive.load_coverage(source)
    coverage_start = (
        start
        if current is None or current.coverage_start is None
        else min(start, current.coverage_start)
    )
    coverage_end = (
        end
        if current is None or current.coverage_end is None
        else max(end, current.coverage_end)
    )
    archive.save_coverage(
        SourceCoverage(
            source=source,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            coverage_status=CoverageStatus.PARTIAL,
            known_gaps=() if current is None else current.known_gaps,
            bootstrap_time=(
                observed_at if current is None else current.bootstrap_time
            ),
            completed_backfill_ranges=(
                () if current is None else current.completed_backfill_ranges
            ),
            live_collection_start=(
                None if current is None else current.live_collection_start
            ),
            last_verification_time=observed_at,
            coverage_generation=(
                1 if current is None else current.coverage_generation + 1
            ),
            coverage_version=(
                "external_coverage_v1"
                if current is None
                else current.coverage_version
            ),
        )
    )


def _extend_continuous_poll_coverage(
    archive: ExternalEventArchive,
    *,
    source: str,
    previous_observed_at: datetime | None,
    observed_at: datetime,
    continuity_bound_seconds: float,
) -> None:
    observed_at = ensure_timezone_aware_utc(observed_at)
    current = archive.load_coverage(source)
    if current is None:
        _record_one_shot_live_coverage(
            archive, source=source, observed_at=observed_at
        )
        current = archive.load_coverage(source)
        assert current is not None
    gaps = list(current.known_gaps)
    previous = (
        current.coverage_end
        if previous_observed_at is None
        else ensure_timezone_aware_utc(previous_observed_at)
    )
    if previous is not None and observed_at > previous:
        elapsed = (observed_at - previous).total_seconds()
        if elapsed > continuity_bound_seconds:
            gap_start = previous + timedelta(microseconds=1)
            gap_end = observed_at - timedelta(microseconds=1)
            if gap_start <= gap_end:
                gap = CoverageInterval(start=gap_start, end=gap_end)
                if gap not in gaps:
                    gaps.append(gap)
    archive.save_coverage(
        SourceCoverage(
            source=source,
            coverage_start=(
                observed_at
                if current.coverage_start is None
                else min(current.coverage_start, observed_at)
            ),
            coverage_end=(
                observed_at
                if current.coverage_end is None
                else max(current.coverage_end, observed_at)
            ),
            coverage_status=current.coverage_status,
            known_gaps=tuple(sorted(gaps, key=lambda value: value.start)),
            bootstrap_time=current.bootstrap_time or observed_at,
            completed_backfill_ranges=current.completed_backfill_ranges,
            live_collection_start=current.live_collection_start or observed_at,
            last_verification_time=observed_at,
            coverage_generation=current.coverage_generation + 1,
            coverage_version=current.coverage_version,
        )
    )


__all__ = [
    "BoundedHTTPClient",
    "BoundedSourcePoller",
    "EARNINGS_SOURCE",
    "EarningsDiscoveryAdapter",
    "EarningsPackage",
    "EarningsSettings",
    "ExternalHTTPSettings",
    "ExternalSourceError",
    "FeedItem",
    "HTTPFetchResult",
    "LMT_SOURCE",
    "LockheedMartinRSSAdapter",
    "LockheedMartinRSSSettings",
    "PLTR_SOURCE",
    "PalantirIRAdapter",
    "PalantirIRSettings",
    "PalantirRelease",
    "ParsedFeed",
    "SourceCollectionResult",
    "SourceHealthStatus",
    "discover_earnings_packages",
    "parse_feed",
    "parse_palantir_release_list",
]
