"""Authenticated VeritaWire receiver for Truth Social source observations.

The receiver's hot path ends after immutable archival and checkpoint
publication.  Classification is intentionally a separate archive consumer so
provider latency cannot prevent receipt of later WebSocket messages.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import logging
import os
import random
from typing import Any, AsyncContextManager, Awaitable, Callable, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    ExternalNormalizationError,
    HTML_NORMALIZER_VERSION,
    canonicalize_url,
    normalize_html_fragment,
)


VERITAWIRE_SOURCE = "veritawire_truth_social"

# websockets logs complete handshake headers and frame bodies at DEBUG.  This
# dedicated disabled logger prevents a process-wide DEBUG setting from exposing
# the bearer token or archived social content through the transport library.
_WEBSOCKET_TRANSPORT_LOGGER = logging.getLogger(
    "market_relay_engine.transport.veritawire.private"
)
_WEBSOCKET_TRANSPORT_LOGGER.disabled = True
_WEBSOCKET_TRANSPORT_LOGGER.propagate = False


class VeritaWireError(RuntimeError):
    """Base safe connector error; messages never include response bodies/keys."""


class VeritaWireAuthenticationError(VeritaWireError):
    """Raised when the API key is absent or authentication is rejected."""


class VeritaWireMessageError(VeritaWireError):
    """Raised when the observed payload does not match the relied-upon schema."""


@dataclass(frozen=True, kw_only=True)
class VeritaWireSettings:
    websocket_url: str = "wss://veritawire.com/ws"
    api_key_env: str = "VERITAWIRE_API_KEY"
    enabled: bool = False
    connect_timeout_seconds: float = 10.0
    close_timeout_seconds: float = 5.0
    ping_interval_seconds: float = 20.0
    ping_timeout_seconds: float = 20.0
    max_message_bytes: int = 1_000_000
    reconnect_base_delay_seconds: float = 0.5
    reconnect_max_delay_seconds: float = 30.0
    reconnect_jitter_fraction: float = 0.2
    max_reconnect_attempts: int | None = None
    expected_authors: tuple[str, ...] = ("realdonaldtrump",)
    adapter_version: str = "veritawire_truth_social_v1"
    extractor_version: str = "veritawire_content_html_v1"

    def __post_init__(self) -> None:
        parts = urlsplit(self.websocket_url)
        if (
            parts.scheme.lower() != "wss"
            or parts.hostname is None
            or parts.hostname.lower() != "veritawire.com"
            or parts.path != "/ws"
            or parts.fragment
        ):
            raise VeritaWireError(
                "VeritaWire endpoint must be the official wss://veritawire.com/ws endpoint"
            )
        if parts.username is not None or parts.password is not None or parts.query:
            raise VeritaWireError(
                "VeritaWire endpoint must use header-only authentication"
            )
        if parts.netloc.lower() != "veritawire.com":
            raise VeritaWireError(
                "VeritaWire endpoint must be the official wss://veritawire.com/ws endpoint"
            )
        if self.api_key_env != "VERITAWIRE_API_KEY":
            raise VeritaWireError(
                "VeritaWire API key environment name must be VERITAWIRE_API_KEY"
            )
        for name in (
            "connect_timeout_seconds",
            "close_timeout_seconds",
            "ping_interval_seconds",
            "ping_timeout_seconds",
            "reconnect_base_delay_seconds",
            "reconnect_max_delay_seconds",
        ):
            if getattr(self, name) <= 0:
                raise VeritaWireError(f"{name} must be positive")
        if self.max_message_bytes < 1:
            raise VeritaWireError("max_message_bytes must be positive")
        if self.reconnect_base_delay_seconds > self.reconnect_max_delay_seconds:
            raise VeritaWireError("reconnect base delay exceeds maximum")
        if not 0 <= self.reconnect_jitter_fraction <= 1:
            raise VeritaWireError("reconnect jitter fraction must be between 0 and 1")
        if self.max_reconnect_attempts is not None and self.max_reconnect_attempts < 0:
            raise VeritaWireError("max reconnect attempts must be non-negative")
        if not self.expected_authors:
            raise VeritaWireError("at least one expected Truth Social author is required")


@dataclass(frozen=True, kw_only=True)
class ParsedVeritaWirePost:
    source_fact_id: str
    created_at: datetime
    updated_at: datetime | None
    author: str
    content_html: str
    normalized_text: str
    source_uri: str | None
    in_reply_to_id: str | None
    quote_id: str | None

    @property
    def text_bearing(self) -> bool:
        return bool(self.normalized_text.strip())


@dataclass(frozen=True, kw_only=True)
class VeritaWireArchiveResult:
    source_fact_id: str
    source_revision_id: str
    duplicate: bool
    text_bearing: bool
    checkpoint_generation: int


@dataclass
class VeritaWireHealth:
    source: str = VERITAWIRE_SOURCE
    enabled: bool = False
    connected: bool = False
    last_successful_connection: datetime | None = None
    last_source_record_identity: str | None = None
    last_system_receipt_time: datetime | None = None
    last_source_published_time: datetime | None = None
    failure_category: str | None = None
    consecutive_failure_count: int = 0
    reconnect_count: int = 0
    duplicate_count: int = 0
    new_record_count: int = 0
    pending_classification_count: int = 0
    completed_classification_count: int = 0
    malformed_count: int = 0
    checkpoint_generation: int = 0
    parser_version: str = "veritawire_payload_v1"
    extractor_version: str = "veritawire_content_html_v1"

    def safe_snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "enabled": self.enabled,
            "connected": self.connected,
            "last_successful_connection": _optional_iso(
                self.last_successful_connection
            ),
            "last_source_record_identity": self.last_source_record_identity,
            "last_system_receipt_time": _optional_iso(
                self.last_system_receipt_time
            ),
            "last_source_published_time": _optional_iso(
                self.last_source_published_time
            ),
            "failure_category": self.failure_category,
            "consecutive_failure_count": self.consecutive_failure_count,
            "reconnect_count": self.reconnect_count,
            "duplicate_count": self.duplicate_count,
            "new_record_count": self.new_record_count,
            "pending_classification_count": self.pending_classification_count,
            "completed_classification_count": self.completed_classification_count,
            "malformed_count": self.malformed_count,
            "checkpoint_generation": self.checkpoint_generation,
            "parser_version": self.parser_version,
            "extractor_version": self.extractor_version,
        }


class WebSocketLike(Protocol):
    async def recv(self) -> str | bytes: ...


class ConnectFactory(Protocol):
    def __call__(self, url: str, **kwargs: Any) -> AsyncContextManager[WebSocketLike]: ...


def build_authorization_headers(api_key: str) -> dict[str, str]:
    if not isinstance(api_key, str) or not api_key.strip():
        raise VeritaWireAuthenticationError("VeritaWire API key is missing")
    if "\r" in api_key or "\n" in api_key:
        raise VeritaWireAuthenticationError("VeritaWire API key is invalid")
    return {"Authorization": f"Bearer {api_key.strip()}"}


def build_replay_url(websocket_url: str, last_seen_id: str | None) -> str:
    parts = urlsplit(websocket_url)
    if (
        parts.scheme.lower() != "wss"
        or parts.hostname is None
        or parts.hostname.lower() != "veritawire.com"
        or parts.netloc.lower() != "veritawire.com"
        or parts.path != "/ws"
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise VeritaWireError(
            "VeritaWire endpoint must be the official wss://veritawire.com/ws endpoint"
        )
    existing_query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key != "last_seen_id" for key, _value in existing_query):
        raise VeritaWireError(
            "VeritaWire replay URL accepts only last_seen_id query state"
        )
    query: list[tuple[str, str]] = []
    if last_seen_id is not None:
        if not isinstance(last_seen_id, str) or not last_seen_id.strip():
            raise VeritaWireError("last_seen_id must be a non-empty string")
        query.append(("last_seen_id", last_seen_id.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def parse_veritawire_message(
    message: str | bytes,
    *,
    max_message_bytes: int = 1_000_000,
) -> tuple[ParsedVeritaWirePost, bytes, Mapping[str, Any]]:
    if isinstance(message, str):
        raw = message.encode("utf-8")
    elif isinstance(message, bytes):
        raw = message
    else:
        raise VeritaWireMessageError("VeritaWire message must be text or bytes")
    if len(raw) > max_message_bytes:
        raise VeritaWireMessageError("VeritaWire message exceeds configured size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VeritaWireMessageError("VeritaWire message is malformed JSON") from exc
    if not isinstance(value, Mapping):
        raise VeritaWireMessageError("VeritaWire message root must be an object")
    source_id = _required_string_field(value, "id")
    created_at = _required_timestamp(value, "created_at")
    content = value.get("content")
    if not isinstance(content, str):
        raise VeritaWireMessageError("VeritaWire content field must be a string")
    account = value.get("account")
    if not isinstance(account, Mapping):
        raise VeritaWireMessageError("VeritaWire account field must be an object")
    username = account.get("username")
    acct = account.get("acct")
    author = username if isinstance(username, str) and username.strip() else acct
    if not isinstance(author, str) or not author.strip():
        raise VeritaWireMessageError("VeritaWire account identity is missing")
    edited_at = _optional_timestamp(value.get("edited_at"), "edited_at")
    source_uri = _optional_http_uri(value.get("uri") or value.get("url"))
    in_reply_to_id = _optional_id(value.get("in_reply_to_id"), "in_reply_to_id")
    quote_id = _optional_id(value.get("quote_id"), "quote_id")

    pieces: list[str] = []
    normalized = normalize_html_fragment(content)
    if normalized:
        pieces.append(normalized)
    for label, relation in (
        ("QUOTED_POST", value.get("quote")),
        ("PARENT_POST", value.get("in_reply_to")),
    ):
        if relation is None:
            continue
        if not isinstance(relation, Mapping):
            raise VeritaWireMessageError(
                "VeritaWire related-post field changed type"
            )
        related_content = relation.get("content")
        if not isinstance(related_content, str):
            raise VeritaWireMessageError(
                "VeritaWire related post is missing content"
            )
        related_text = normalize_html_fragment(related_content)
        if related_text:
            related_id = relation.get("id")
            suffix = (
                ""
                if not isinstance(related_id, str) or not related_id.strip()
                else f" {related_id.strip()}"
            )
            pieces.append(f"[{label}{suffix}]\n{related_text}")
    return (
        ParsedVeritaWirePost(
            source_fact_id=source_id,
            created_at=created_at,
            updated_at=edited_at,
            author=author.strip(),
            content_html=content,
            normalized_text="\n\n".join(pieces).strip(),
            source_uri=source_uri,
            in_reply_to_id=in_reply_to_id,
            quote_id=quote_id,
        ),
        raw,
        value,
    )


class VeritaWireConnector:
    """One persistent authenticated receiver with restart-safe replay."""

    def __init__(
        self,
        *,
        settings: VeritaWireSettings,
        archive: ExternalEventArchive,
        connect_factory: ConnectFactory | None = None,
        now: Callable[[], datetime] = utc_now,
        async_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        api_key: str | None = None,
    ) -> None:
        self.settings = settings
        self.archive = archive
        self._connect = connect_factory or _default_connect
        self._now = now
        self._sleep = async_sleeper
        self._random = random_value
        self._api_key = api_key
        self._classification_states = _veritawire_classification_states(archive)
        checkpoint = archive.get_checkpoint(VERITAWIRE_SOURCE) or {}
        self.health = VeritaWireHealth(
            enabled=settings.enabled,
            last_source_record_identity=(
                None
                if checkpoint.get("last_seen_id") is None
                else str(checkpoint["last_seen_id"])
            ),
            last_system_receipt_time=_checkpoint_timestamp(
                checkpoint.get("last_received_at")
            ),
            checkpoint_generation=int(checkpoint.get("generation", 0)),
            extractor_version=settings.extractor_version,
        )
        self._update_classification_health_counts()

    def archive_message(
        self, message: str | bytes, *, received_at: datetime | None = None
    ) -> VeritaWireArchiveResult:
        receipt = ensure_timezone_aware_utc(received_at or self._now())
        source_id, author, raw = _minimum_veritawire_envelope(
            message,
            max_message_bytes=self.settings.max_message_bytes,
        )
        expected = {value.casefold() for value in self.settings.expected_authors}
        author_parts = {part.casefold() for part in author.split("@") if part}
        if not expected.intersection(author_parts):
            raise VeritaWireMessageError(
                "VeritaWire message author is outside the reviewed source"
            )
        raw_hash = self.archive.archive_object(
            raw, extension="json", content_type="application/json"
        )
        try:
            post, parsed_raw, payload = parse_veritawire_message(
                message, max_message_bytes=self.settings.max_message_bytes
            )
        except VeritaWireMessageError:
            # The minimum source envelope was valid, so retain a safe rejected
            # observation for schema-drift diagnosis.  The raw object remains
            # local and the replay checkpoint deliberately does not advance.
            self.archive.publish_observation(
                source=VERITAWIRE_SOURCE,
                payload={
                    "kind": "VERITAWIRE_REJECTED_DELIVERY",
                    "source_fact_id": source_id,
                    "raw_object_hash": raw_hash,
                    "system_observed_at": to_utc_iso(receipt),
                    "failure_category": "SCHEMA_DRIFT",
                },
                observed_at=receipt,
            )
            raise
        if parsed_raw != raw or post.source_fact_id != source_id:
            raise VeritaWireMessageError(
                "VeritaWire minimum and complete envelope identities differ"
            )
        normalized_hash = (
            self.archive.archive_normalized_text(post.normalized_text)
            if post.text_bearing
            else None
        )
        canonical_hash = _canonical_post_hash(post)
        existing = sorted(
            (
                value
                for value in self.archive.iter_revisions(
                    sources=(VERITAWIRE_SOURCE,)
                )
                if value.source_fact_id == post.source_fact_id
            ),
            key=lambda value: (value.revision_sequence, value.system_observed_at),
        )
        current = _current_post_revision(existing)
        exact = (
            current
            if current is not None
            and current.canonical_content_hash == canonical_hash
            and current.lifecycle_state
            in {LifecycleState.ACTIVE, LifecycleState.UPDATED}
            else None
        )
        duplicate = exact is not None
        if exact is None:
            lifecycle = (
                LifecycleState.ACTIVE
                if current is None
                else LifecycleState.UPDATED
            )
            sequence = 1 if current is None else current.revision_sequence + 1
            supersedes_revision_id = (
                None if current is None else current.source_revision_id
            )
            revision_id = _ordered_post_revision_id(
                source=VERITAWIRE_SOURCE,
                source_fact_id=post.source_fact_id,
                canonical_content_hash=canonical_hash,
                lifecycle_state=lifecycle,
                adapter_version=self.settings.adapter_version,
                revision_sequence=sequence,
                supersedes_revision_id=supersedes_revision_id,
            )
            archived_at = ensure_timezone_aware_utc(self._now())
            declared_available_at = (
                post.created_at if current is None else post.updated_at
            )
            revision_available_at = (
                receipt
                if declared_available_at is None
                else min(declared_available_at, receipt)
            )
            if current is not None:
                revision_available_at = max(
                    revision_available_at,
                    current.lifecycle_effective_at,
                )
            revision = ExternalSourceRevision(
                source=VERITAWIRE_SOURCE,
                source_fact_id=post.source_fact_id,
                source_revision_id=revision_id,
                revision_sequence=sequence,
                supersedes_revision_id=supersedes_revision_id,
                lifecycle_state=lifecycle,
                lifecycle_effective_at=revision_available_at,
                system_observed_at=receipt,
                source_available_at=revision_available_at,
                archived_at=archived_at,
                raw_object_hash=raw_hash,
                document_hash=raw_hash,
                normalized_text_hash=normalized_hash,
                canonical_content_hash=canonical_hash,
                source_type="SOCIAL_POST",
                source_platform="VERITAWIRE_TRUTH_SOCIAL",
                source_uri=post.source_uri,
                source_published_at=post.created_at,
                source_updated_at=post.updated_at,
                collection_mode="LIVE_SYSTEM",
                adapter_version=self.settings.adapter_version,
                extractor_version=self.settings.extractor_version,
                normalizer_version=HTML_NORMALIZER_VERSION,
            )
            revision = self.archive.publish_revision(revision)
            self._classification_states[post.source_fact_id] = (
                "PENDING" if post.text_bearing else "INELIGIBLE"
            )
            self._update_classification_health_counts()
        else:
            revision = exact

        self.archive.publish_observation(
            source=VERITAWIRE_SOURCE,
            payload={
                "kind": "VERITAWIRE_DELIVERY",
                "source_fact_id": post.source_fact_id,
                "source_revision_id": revision.source_revision_id,
                "raw_object_hash": raw_hash,
                "system_observed_at": to_utc_iso(receipt),
                "source_published_at": to_utc_iso(post.created_at),
                "source_updated_at": _optional_iso(post.updated_at),
                "author": post.author,
                "duplicate": duplicate,
                "text_bearing": post.text_bearing,
                "in_reply_to_id": post.in_reply_to_id,
                "quote_id": post.quote_id,
                "payload_field_names": sorted(str(key) for key in payload),
            },
            source_revision_id=revision.source_revision_id,
            observed_at=receipt,
        )
        generation = self.archive.update_checkpoint(
            VERITAWIRE_SOURCE,
            {
                "last_seen_id": post.source_fact_id,
                "last_received_at": to_utc_iso(receipt),
                "last_revision_id": revision.source_revision_id,
            },
        )
        _record_live_coverage_point(
            self.archive,
            observed_at=receipt,
        )
        return VeritaWireArchiveResult(
            source_fact_id=post.source_fact_id,
            source_revision_id=revision.source_revision_id,
            duplicate=duplicate,
            text_bearing=post.text_bearing,
            checkpoint_generation=generation,
        )

    def refresh_classification_health(self) -> VeritaWireHealth:
        """Refresh archive-derived counts outside the socket receive hot path."""

        self._classification_states = _veritawire_classification_states(
            self.archive
        )
        self._update_classification_health_counts()
        return self.health

    def _update_classification_health_counts(self) -> None:
        self.health.pending_classification_count = sum(
            value == "PENDING" for value in self._classification_states.values()
        )
        self.health.completed_classification_count = sum(
            value == "COMPLETED" for value in self._classification_states.values()
        )

    async def run(
        self,
        *,
        max_messages: int | None = None,
        timeout_seconds: float | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> int:
        if max_messages is not None and max_messages < 1:
            raise VeritaWireError("max_messages must be positive")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise VeritaWireError("timeout_seconds must be positive")
        key = self._api_key
        if key is None:
            key = os.environ.get(self.settings.api_key_env)
        headers = build_authorization_headers(key or "")
        loop = asyncio.get_running_loop()
        deadline = (
            None if timeout_seconds is None else loop.time() + timeout_seconds
        )
        received = 0
        reconnect_attempt = 0
        while not _should_stop(stop_event, deadline, loop.time()):
            checkpoint = self.archive.get_checkpoint(VERITAWIRE_SOURCE) or {}
            replay_url = build_replay_url(
                self.settings.websocket_url,
                (
                    None
                    if checkpoint.get("last_seen_id") is None
                    else str(checkpoint["last_seen_id"])
                ),
            )
            connection_started_at: datetime | None = None
            try:
                async with self._connect(
                    replay_url,
                    additional_headers=headers,
                    open_timeout=self.settings.connect_timeout_seconds,
                    close_timeout=self.settings.close_timeout_seconds,
                    ping_interval=self.settings.ping_interval_seconds,
                    ping_timeout=self.settings.ping_timeout_seconds,
                    max_size=self.settings.max_message_bytes,
                    logger=_WEBSOCKET_TRANSPORT_LOGGER,
                ) as socket:
                    self.health.connected = True
                    connection_started_at = ensure_timezone_aware_utc(self._now())
                    self.health.last_successful_connection = connection_started_at
                    _extend_continuous_live_coverage(
                        self.archive,
                        session_start=connection_started_at,
                        verified_through=connection_started_at,
                    )
                    self.health.failure_category = None
                    while not _should_stop(stop_event, deadline, loop.time()):
                        message = await _receive_bounded(
                            socket,
                            stop_event=stop_event,
                            deadline=deadline,
                        )
                        if message is None:
                            return received
                        receipt = ensure_timezone_aware_utc(self._now())
                        try:
                            result = self.archive_message(
                                message, received_at=receipt
                            )
                        except VeritaWireMessageError:
                            self.health.malformed_count += 1
                            self.health.failure_category = "MALFORMED_MESSAGE"
                            self.health.consecutive_failure_count += 1
                            continue
                        received += 1
                        reconnect_attempt = 0
                        self.health.consecutive_failure_count = 0
                        self.health.last_source_record_identity = result.source_fact_id
                        self.health.last_system_receipt_time = receipt
                        revision = self.archive.read_revision(
                            VERITAWIRE_SOURCE,
                            result.source_fact_id,
                            result.source_revision_id,
                        )
                        _extend_continuous_live_coverage(
                            self.archive,
                            session_start=connection_started_at,
                            verified_through=receipt,
                        )
                        self.health.last_source_published_time = (
                            revision.source_published_at
                        )
                        self.health.checkpoint_generation = (
                            result.checkpoint_generation
                        )
                        if result.duplicate:
                            self.health.duplicate_count += 1
                        else:
                            self.health.new_record_count += 1
                        if max_messages is not None and received >= max_messages:
                            return received
            except asyncio.CancelledError:
                raise
            except VeritaWireAuthenticationError:
                raise
            except Exception as exc:
                status = _exception_status(exc)
                self.health.connected = False
                self.health.consecutive_failure_count += 1
                if status in {401, 403}:
                    self.health.failure_category = "AUTHENTICATION"
                    raise VeritaWireAuthenticationError(
                        "VeritaWire rejected authentication"
                    ) from None
                if status == 429:
                    self.health.failure_category = "RATE_LIMIT"
                else:
                    self.health.failure_category = "CONNECTION"
                if _should_stop(stop_event, deadline, loop.time()):
                    return received
                if (
                    self.settings.max_reconnect_attempts is not None
                    and reconnect_attempt >= self.settings.max_reconnect_attempts
                ):
                    raise VeritaWireError(
                        "VeritaWire reconnect budget exhausted"
                    ) from None
                delay = self._reconnect_delay(reconnect_attempt)
                reconnect_attempt += 1
                self.health.reconnect_count += 1
                await _sleep_with_deadline(
                    delay,
                    sleeper=self._sleep,
                    deadline=deadline,
                    now=loop.time,
                )
            finally:
                if connection_started_at is not None:
                    _extend_continuous_live_coverage(
                        self.archive,
                        session_start=connection_started_at,
                        verified_through=ensure_timezone_aware_utc(self._now()),
                    )
                self.health.connected = False
        return received

    def _reconnect_delay(self, attempt: int) -> float:
        base = min(
            self.settings.reconnect_base_delay_seconds * (2**attempt),
            self.settings.reconnect_max_delay_seconds,
        )
        jitter = (
            (self._random() * 2 - 1)
            * self.settings.reconnect_jitter_fraction
            * base
        )
        return max(0.0, min(self.settings.reconnect_max_delay_seconds, base + jitter))


def _default_connect(url: str, **kwargs: Any) -> AsyncContextManager[WebSocketLike]:
    from websockets.asyncio.client import connect

    return connect(url, **kwargs)


async def _receive_bounded(
    socket: WebSocketLike,
    *,
    stop_event: asyncio.Event | None,
    deadline: float | None,
) -> str | bytes | None:
    loop = asyncio.get_running_loop()
    while True:
        if _should_stop(stop_event, deadline, loop.time()):
            return None
        timeout = 0.25
        if deadline is not None:
            timeout = min(timeout, max(0.001, deadline - loop.time()))
        try:
            return await asyncio.wait_for(socket.recv(), timeout=timeout)
        except TimeoutError:
            continue


async def _sleep_with_deadline(
    delay: float,
    *,
    sleeper: Callable[[float], Awaitable[None]],
    deadline: float | None,
    now: Callable[[], float],
) -> None:
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - now()))
    if delay > 0:
        await sleeper(delay)


def _canonical_post_hash(post: ParsedVeritaWirePost) -> str:
    """Identify the source fact content, not one transport observation.

    VeritaWire may add delivery-only envelope metadata during replay.  Those
    bytes are preserved as a new source observation, but they must not turn an
    unchanged Truth Social post into a content revision.  The exact original
    HTML and relationship-derived normalized text remain revision-significant.
    """

    payload = {
        "content_html": post.content_html,
        "normalized_text": post.normalized_text,
        "edited_at": _optional_iso(post.updated_at),
        "in_reply_to_id": post.in_reply_to_id,
        "quote_id": post.quote_id,
    }
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _current_post_revision(
    revisions: tuple[ExternalSourceRevision, ...] | list[ExternalSourceRevision],
) -> ExternalSourceRevision | None:
    if not revisions:
        return None
    sequences: dict[int, str] = {}
    for revision in revisions:
        prior = sequences.get(revision.revision_sequence)
        if prior is not None and prior != revision.source_revision_id:
            raise VeritaWireMessageError(
                "VeritaWire lifecycle revision order is ambiguous"
            )
        sequences[revision.revision_sequence] = revision.source_revision_id
    return max(
        revisions,
        key=lambda value: (
            value.revision_sequence,
            value.system_observed_at,
            value.source_revision_id,
        ),
    )


def _veritawire_classification_states(
    archive: ExternalEventArchive,
) -> dict[str, str]:
    grouped: dict[str, list[ExternalSourceRevision]] = {}
    for revision in archive.iter_revisions(sources=(VERITAWIRE_SOURCE,)):
        grouped.setdefault(revision.source_fact_id, []).append(revision)
    states: dict[str, str] = {}
    for source_fact_id, revisions in grouped.items():
        current = _current_post_revision(revisions)
        if (
            current is None
            or current.normalized_text_hash is None
            or current.lifecycle_state
            not in {LifecycleState.ACTIVE, LifecycleState.UPDATED}
        ):
            states[source_fact_id] = "INELIGIBLE"
            continue
        readiness = archive.iter_readiness(current.source_revision_id)
        states[source_fact_id] = (
            "COMPLETED"
            if any(
                value.get("classification_status") in {"VALID", "ABSTAINED"}
                for value in readiness
            )
            else "PENDING"
        )
    return states


def _checkpoint_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VeritaWireError("VeritaWire checkpoint timestamp is invalid")
    try:
        return parse_utc_iso(value)
    except ValueError as exc:
        raise VeritaWireError("VeritaWire checkpoint timestamp is invalid") from exc


def _ordered_post_revision_id(
    *,
    source: str,
    source_fact_id: str,
    canonical_content_hash: str,
    lifecycle_state: LifecycleState,
    adapter_version: str,
    revision_sequence: int,
    supersedes_revision_id: str | None,
) -> str:
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


def _required_string_field(value: Mapping[str, Any], name: str) -> str:
    field = value.get(name)
    if not isinstance(field, str) or not field.strip():
        raise VeritaWireMessageError(f"VeritaWire {name} is missing or invalid")
    return field.strip()


def _minimum_veritawire_envelope(
    message: str | bytes,
    *,
    max_message_bytes: int,
) -> tuple[str, str, bytes]:
    """Validate only the fields needed to prove source identity before archive."""

    if isinstance(message, str):
        raw = message.encode("utf-8")
    elif isinstance(message, bytes):
        raw = message
    else:
        raise VeritaWireMessageError("VeritaWire message must be text or bytes")
    if len(raw) > max_message_bytes:
        raise VeritaWireMessageError("VeritaWire message exceeds configured size")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VeritaWireMessageError("VeritaWire message is malformed JSON") from exc
    if not isinstance(value, Mapping):
        raise VeritaWireMessageError("VeritaWire message root must be an object")
    source_id = _required_string_field(value, "id")
    _required_timestamp(value, "created_at")
    if not isinstance(value.get("content"), str):
        raise VeritaWireMessageError("VeritaWire content field must be a string")
    account = value.get("account")
    if not isinstance(account, Mapping):
        raise VeritaWireMessageError("VeritaWire account field must be an object")
    username = account.get("username")
    acct = account.get("acct")
    author = username if isinstance(username, str) and username.strip() else acct
    if not isinstance(author, str) or not author.strip():
        raise VeritaWireMessageError("VeritaWire account identity is missing")
    return source_id, author.strip(), raw


def _required_timestamp(value: Mapping[str, Any], name: str) -> datetime:
    field = value.get(name)
    if not isinstance(field, str):
        raise VeritaWireMessageError(f"VeritaWire {name} is missing or invalid")
    try:
        return parse_utc_iso(field)
    except ValueError as exc:
        raise VeritaWireMessageError(
            f"VeritaWire {name} is not an ISO timestamp"
        ) from exc


def _optional_timestamp(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VeritaWireMessageError(f"VeritaWire {name} changed type")
    try:
        return parse_utc_iso(value)
    except ValueError as exc:
        raise VeritaWireMessageError(
            f"VeritaWire {name} is not an ISO timestamp"
        ) from exc


def _optional_id(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise VeritaWireMessageError(f"VeritaWire {name} changed type")
    return value.strip()


def _optional_http_uri(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise VeritaWireMessageError("VeritaWire source URI changed type")
    try:
        return canonicalize_url(value.strip())
    except ExternalNormalizationError as exc:
        raise VeritaWireMessageError("VeritaWire source URI is invalid") from exc


def _exception_status(exc: Exception) -> int | None:
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct
    response = getattr(exc, "response", None)
    for name in ("status_code", "status"):
        value = getattr(response, name, None)
        if isinstance(value, int):
            return value
    return None


def _should_stop(
    stop_event: asyncio.Event | None, deadline: float | None, now: float
) -> bool:
    return bool(
        (stop_event is not None and stop_event.is_set())
        or (deadline is not None and now >= deadline)
    )


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _record_live_coverage_point(
    archive: ExternalEventArchive, *, observed_at: datetime
) -> None:
    observed_at = ensure_timezone_aware_utc(observed_at)
    current = archive.load_coverage(VERITAWIRE_SOURCE)
    if current is None:
        archive.save_coverage(
            SourceCoverage(
                source=VERITAWIRE_SOURCE,
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
            source=VERITAWIRE_SOURCE,
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


def _extend_continuous_live_coverage(
    archive: ExternalEventArchive,
    *,
    session_start: datetime,
    verified_through: datetime,
) -> None:
    session_start = ensure_timezone_aware_utc(session_start)
    verified_through = ensure_timezone_aware_utc(verified_through)
    if verified_through < session_start:
        raise VeritaWireError("coverage verification precedes connection start")
    current = archive.load_coverage(VERITAWIRE_SOURCE)
    if current is None:
        _record_live_coverage_point(archive, observed_at=session_start)
        current = archive.load_coverage(VERITAWIRE_SOURCE)
        assert current is not None
    gaps = list(current.known_gaps)
    if current.coverage_end is not None and session_start > current.coverage_end:
        gap_start = current.coverage_end + timedelta(microseconds=1)
        gap_end = session_start - timedelta(microseconds=1)
        if gap_start <= gap_end:
            gap = CoverageInterval(start=gap_start, end=gap_end)
            if gap not in gaps:
                gaps.append(gap)
    archive.save_coverage(
        SourceCoverage(
            source=VERITAWIRE_SOURCE,
            coverage_start=(
                session_start
                if current.coverage_start is None
                else min(current.coverage_start, session_start)
            ),
            coverage_end=(
                verified_through
                if current.coverage_end is None
                else max(current.coverage_end, verified_through)
            ),
            coverage_status=current.coverage_status,
            known_gaps=tuple(sorted(gaps, key=lambda value: value.start)),
            bootstrap_time=current.bootstrap_time or session_start,
            completed_backfill_ranges=current.completed_backfill_ranges,
            live_collection_start=current.live_collection_start or session_start,
            last_verification_time=verified_through,
            coverage_generation=current.coverage_generation + 1,
            coverage_version=current.coverage_version,
        )
    )


__all__ = [
    "ParsedVeritaWirePost",
    "VERITAWIRE_SOURCE",
    "VeritaWireArchiveResult",
    "VeritaWireAuthenticationError",
    "VeritaWireConnector",
    "VeritaWireError",
    "VeritaWireHealth",
    "VeritaWireMessageError",
    "VeritaWireSettings",
    "build_authorization_headers",
    "build_replay_url",
    "parse_veritawire_message",
]
