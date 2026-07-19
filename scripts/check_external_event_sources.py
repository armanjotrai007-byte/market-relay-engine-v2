"""Bounded checker for the research-only external news/social source pilot.

The default mode uses synthetic local inputs and a temporary archive.  It makes
no network, Gemini, QuestDB, Alpaca, risk, or trading calls.  ``--live`` is an
explicit bounded source-connectivity gate; classification and QuestDB writes
have independent opt-in flags.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
for path in (REPO_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_relay_engine.context.external_event_archive import (  # noqa: E402
    ExternalEventArchive,
    ExternalSourceRevision,
    LifecycleState,
    classification_input_fingerprint,
    output_fingerprints,
    source_revision_id,
)
from market_relay_engine.context.external_normalization import (  # noqa: E402
    build_scope_aware_excerpt,
    normalize_html_fragment,
    resolve_explicit_scope,
)
from market_relay_engine.context.external_source_config import (  # noqa: E402
    ExternalEventSourcesSettings,
    load_external_event_source_settings,
)


SOURCE_CHOICES = ("veritawire", "lmt-rss", "pltr-ir", "earnings")
MAX_CHECK_TIMEOUT_SECONDS = 120.0
MAX_CHECK_ITEMS = 100
MAX_CHECK_POLLS = 20


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    label: str
    detail: str


@dataclass(frozen=True)
class _PendingClassificationCandidate:
    revision: ExternalSourceRevision
    profile: Any
    classification_input_fingerprint: str


class _PreparationOnlyClassifier:
    """Fail loudly if candidate inspection ever crosses the provider boundary."""

    def classify(self, _request: object) -> object:
        raise AssertionError("candidate inspection must not call Gemini")


class _OfflineHTTPResponse:
    def __init__(
        self,
        *,
        content: bytes,
        content_type: str,
        url: str,
        status_code: int = 200,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.url = url

    def iter_content(self, *, chunk_size: int):
        del chunk_size
        yield self.content

    def close(self) -> None:
        return None


class _OfflineHTTPSession:
    def __init__(self, responses: list[_OfflineHTTPResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _OfflineHTTPResponse:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("offline checker attempted an unexpected HTTP request")
        return self.responses.popleft()


class _OfflineWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = deque(messages)

    async def recv(self) -> str:
        if not self.messages:
            raise AssertionError("offline checker attempted an unexpected socket read")
        return self.messages.popleft()


class _OfflineWebSocketContext:
    def __init__(self, messages: list[str]) -> None:
        self.socket = _OfflineWebSocket(messages)

    async def __aenter__(self) -> _OfflineWebSocket:
        return self.socket

    async def __aexit__(self, *_args: object) -> None:
        return None


class _OfflineConnectFactory:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **kwargs: object) -> _OfflineWebSocketContext:
        self.calls.append((url, dict(kwargs)))
        return _OfflineWebSocketContext(self.messages)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Contact exactly one selected official source; omitted is offline only.",
    )
    parser.add_argument(
        "--source",
        choices=SOURCE_CHOICES,
        help="Required with --live; ignored source selection is never inferred.",
    )
    parser.add_argument(
        "--ticker",
        choices=("PLTR", "LMT"),
        help="Required only for the earnings source.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=1,
        help="Strict discovery/message bound for this check (default: 1).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Finite live-check deadline, at most 120 seconds (default: 20).",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Use the explicit polling loop instead of one-shot HTTP collection.",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=1,
        help="Strict polling-loop bound, at most 20 (default: 1).",
    )
    parser.add_argument(
        "--establish-checkpoint",
        action="store_true",
        help="Establish a forward-only checkpoint instead of processing backlog items.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Explicit bounded HTTP-source historical backfill mode.",
    )
    parser.add_argument(
        "--start-time",
        type=_timestamp_argument,
        help="Inclusive UTC-aware ISO timestamp; required for --backfill.",
    )
    parser.add_argument(
        "--end-time",
        type=_timestamp_argument,
        help="Inclusive UTC-aware ISO timestamp; required for --backfill.",
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Explicitly opt in to the existing Gemini classifier after archive publication.",
    )
    parser.add_argument(
        "--questdb",
        action="store_true",
        help="Explicitly opt in to metadata-only QuestDB publication.",
    )
    return parser


def validate_arguments(args: argparse.Namespace) -> str | None:
    if args.max_items <= 0 or args.max_items > MAX_CHECK_ITEMS:
        return f"--max-items must be from 1 through {MAX_CHECK_ITEMS}"
    if (
        args.timeout_seconds <= 0
        or args.timeout_seconds > MAX_CHECK_TIMEOUT_SECONDS
    ):
        return (
            "--timeout-seconds must be greater than zero and at most "
            f"{MAX_CHECK_TIMEOUT_SECONDS:g}"
        )
    if args.max_polls <= 0 or args.max_polls > MAX_CHECK_POLLS:
        return f"--max-polls must be from 1 through {MAX_CHECK_POLLS}"
    if args.live and args.source is None:
        return "--live requires --source"
    if not args.live and (
        args.source is not None
        or args.classify
        or args.questdb
        or args.poll
        or args.establish_checkpoint
        or args.backfill
    ):
        return "live source/action flags require --live"
    if args.source == "earnings" and args.ticker is None:
        return "--source earnings requires --ticker PLTR or LMT"
    if args.source != "earnings" and args.ticker is not None:
        return "--ticker is valid only with --source earnings"
    if args.source == "veritawire" and args.poll:
        return "VeritaWire is persistent WebSocket mode; --poll is HTTP-only"
    if args.establish_checkpoint and args.backfill:
        return "--establish-checkpoint cannot be combined with --backfill"
    if args.backfill:
        if args.source == "veritawire":
            return "bounded timestamp backfill is not supported by the VeritaWire feed"
        if args.start_time is None or args.end_time is None:
            return "--backfill requires --start-time and --end-time"
        if args.end_time < args.start_time:
            return "--end-time precedes --start-time"
        if args.poll:
            return "--backfill cannot be combined with --poll"
    elif args.start_time is not None or args.end_time is not None:
        return "--start-time and --end-time require --backfill"
    return None


def run_offline_checks(
    *,
    base_dir: Path = REPO_ROOT,
) -> tuple[CheckResult, ...]:
    """Exercise deterministic source boundaries without external side effects."""

    results: list[CheckResult] = []
    try:
        settings = load_external_event_source_settings(base_dir=base_dir)
        _record(
            results,
            all(
                not value.common.enabled
                for value in (
                    settings.veritawire,
                    settings.lmt_rss,
                    settings.pltr_ir,
                    settings.earnings,
                )
            ),
            "configuration",
            "four source profiles loaded; all external access defaults disabled",
        )
    except Exception as exc:  # noqa: BLE001 - safe checker boundary.
        return (
            CheckResult(False, "configuration", type(exc).__name__),
        )

    try:
        text = normalize_html_fragment(
            "<p>Lockheed Martin and Palantir support the defense industrial base.</p>"
            "<p>Worldwide tariffs remain under review &amp; no links are fetched.</p>"
        )
        scope = resolve_explicit_scope(
            text,
            approved_tickers=("LMT", "PLTR"),
        )
        _record(
            results,
            scope.tickers == ("LMT", "PLTR")
            and scope.sectors == ("DEFENSE",)
            and scope.global_relevance,
            "normalization-and-scope",
            "HTML normalization and union scope passed with no link/media fetch",
        )
        long_text = "Opening context. " + ("filler " * 1800) + text
        long_scope = resolve_explicit_scope(
            long_text,
            approved_tickers=("LMT", "PLTR"),
        )
        excerpt = build_scope_aware_excerpt(
            long_text,
            title="Synthetic source fixture",
            scope=long_scope,
            max_characters=3000,
        )
        _record(
            results,
            excerpt.truncated
            and "Lockheed Martin" in excerpt.text
            and "Palantir" in excerpt.text
            and not excerpt.omitted_scope_values,
            "scope-aware-excerpt",
            "middle-document supporting scope remains inside bounded input",
        )
    except Exception as exc:  # noqa: BLE001 - report type, never fixture body.
        _record(results, False, "normalization", type(exc).__name__)

    try:
        with TemporaryDirectory(prefix="external-event-check-") as directory:
            _check_temporary_archive(Path(directory))
        _record(
            results,
            True,
            "archive-and-suppression",
            "immutable revision, idempotent replay, and canonical input claim passed",
        )
    except Exception as exc:  # noqa: BLE001 - report type, never archived bytes.
        _record(results, False, "archive-and-suppression", type(exc).__name__)

    _run_optional_offline_connector_checks(results)
    with TemporaryDirectory(prefix="external-pilot-check-") as directory:
        archive: ExternalEventArchive | None = None
        revision: ExternalSourceRevision | None = None
        clock: dict[str, datetime] | None = None
        try:
            archive, revision, clock = _check_offline_source_collectors(
                Path(directory)
            )
            _record(
                results,
                True,
                "source-collector-fixtures",
                "fake WebSocket plus LMT, PLTR, and earnings HTTP acquisition passed",
            )
        except Exception as exc:  # noqa: BLE001 - fixture bodies remain private.
            _record(
                results,
                False,
                "source-collector-fixtures",
                type(exc).__name__,
            )
        if archive is not None and revision is not None and clock is not None:
            try:
                _check_offline_classification_projection(
                    archive=archive,
                    revision=revision,
                    clock=clock,
                )
                _record(
                    results,
                    True,
                    "classification-projection-shadow",
                    "durable classification reuse, PR37 as-of hydration, and NO_CHANGE passed",
                )
            except Exception as exc:  # noqa: BLE001 - never include source/provider bodies.
                _record(
                    results,
                    False,
                    "classification-projection-shadow",
                    type(exc).__name__,
                )
        else:
            _record(
                results,
                False,
                "classification-projection-shadow",
                "source collector fixture prerequisite failed",
            )
    return tuple(results)


def _check_temporary_archive(root: Path) -> None:
    now = datetime(2026, 7, 18, 16, 1, tzinfo=UTC)
    archive = ExternalEventArchive(root, now=lambda: now)
    raw = b'{"synthetic":true,"id":"fixture-post-1"}'
    raw_hash = archive.archive_object(
        raw,
        extension="json",
        content_type="application/json",
    )
    normalized = "Lockheed Martin and Palantir support the defense industrial base."
    normalized_hash = archive.archive_normalized_text(normalized)
    revision_id = source_revision_id(
        source="veritawire_truth_social",
        source_fact_id="fixture-post-1",
        canonical_content_hash=normalized_hash,
        lifecycle_state=LifecycleState.ACTIVE,
        adapter_version="veritawire_truth_social_v1",
    )
    revision = ExternalSourceRevision(
        source="veritawire_truth_social",
        source_fact_id="fixture-post-1",
        source_revision_id=revision_id,
        revision_sequence=1,
        supersedes_revision_id=None,
        lifecycle_state=LifecycleState.ACTIVE,
        lifecycle_effective_at=now,
        system_observed_at=now,
        source_available_at=now,
        archived_at=now,
        raw_object_hash=raw_hash,
        document_hash=raw_hash,
        normalized_text_hash=normalized_hash,
        canonical_content_hash=normalized_hash,
        source_type="SOCIAL_POST",
        source_platform="truth_social_via_veritawire",
        affected_tickers=("PLTR", "LMT"),
        affected_sectors=("DEFENSE",),
        global_relevance=False,
        adapter_version="veritawire_truth_social_v1",
        extractor_version="veritawire_content_v1",
        normalizer_version="external_html_text_v1",
    )
    archive.publish_observation(
        source=revision.source,
        payload={
            "source_fact_id": revision.source_fact_id,
            "source_revision_id": revision.source_revision_id,
            "system_observed_at": now.isoformat(),
        },
    )
    archive.publish_revision(revision)
    archive.publish_revision(revision)
    loaded = tuple(archive.iter_revisions(sources=(revision.source,)))
    if loaded != (revision,):
        raise AssertionError("idempotent revision replay changed archive state")

    semantic_request = {
        "source": revision.source,
        "source_type": revision.source_type,
        "document_hash": revision.document_hash,
        "normalized_text_hash": normalized_hash,
        "excerpt_hash": normalized_hash,
        "trusted_input_scope": {
            "affected_tickers": ["LMT", "PLTR"],
            "affected_sectors": ["DEFENSE"],
            "global_relevance": False,
        },
    }
    profile = {
        "adapter_version": revision.adapter_version,
        "extractor_version": revision.extractor_version,
        "normalizer_version": revision.normalizer_version,
        "excerpt_version": "scope_aware_excerpt_v1",
        "scope_resolver_version": "external_scope_v2",
        "prompt_version": "context_filter_v2_scope",
        "model_version": "synthetic-model",
        "response_schema_version": "context_classification_response_v2",
        "validator_version": "context_filter_validator_v2_scope",
        "classifier_configuration_hash": sha256(b"synthetic-config").hexdigest(),
    }
    input_hash = classification_input_fingerprint(semantic_request, profile)
    profile_hash = sha256(
        repr(sorted(profile.items())).encode("utf-8")
    ).hexdigest()
    output = {
        "status": "ABSTAINED",
        "event_type": None,
        "risk_level": None,
        "urgency": None,
        "confidence": None,
        "affected_tickers": ["LMT", "PLTR"],
        "affected_sectors": ["DEFENSE"],
        "global_relevance": False,
        "valid_from": None,
        "valid_until": None,
    }
    complete_output_hash, policy_output_hash = output_fingerprints(output)
    attempt = {
        "classification_attempt_id": "synthetic-attempt-1",
        "classification_input_fingerprint": input_hash,
        "complete_output_fingerprint": complete_output_hash,
        "policy_output_fingerprint": policy_output_hash,
        "profile_hash": profile_hash,
        "validation_outcome": True,
        "durably_published": True,
        "normalized_output": output,
        "first_archived_at": now.isoformat(),
    }
    with archive.classification_lease(input_hash, owner_id="offline-check") as acquired:
        if not acquired:
            raise AssertionError("synthetic canonical input lease was not acquired")
        archive.publish_classification_attempt(
            classification_input_fingerprint=input_hash,
            attempt_id="synthetic-attempt-1",
            payload=attempt,
        )
        archive.claim_canonical_result(
            classification_input_fingerprint=input_hash,
            attempt_id="synthetic-attempt-1",
            complete_output_fingerprint=complete_output_hash,
            policy_output_fingerprint=policy_output_hash,
            profile_hash=profile_hash,
            evidence_ready_at=now,
        )
    if archive.read_canonical_claim(input_hash) is None:
        raise AssertionError("canonical classification input was not durably claimed")


def _run_optional_offline_connector_checks(results: list[CheckResult]) -> None:
    """Exercise parser fixtures when the connector modules are present."""

    try:
        from market_relay_engine.context.external_sources import parse_feed
        from market_relay_engine.context.veritawire import parse_veritawire_message
    except ImportError:
        _record(
            results,
            False,
            "connector-fixtures",
            "connector modules are not available",
        )
        return
    try:
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Synthetic official feed</title><item><guid>fixture-lmt-1</guid>
        <title>Fixture release</title><link>https://news.lockheedmartin.com/fixture-release</link>
        <pubDate>Sat, 18 Jul 2026 16:00:00 GMT</pubDate></item></channel></rss>"""
        parsed = parse_feed(
            feed,
            max_items=1,
            base_url="https://news.lockheedmartin.com/news-releases?pagetemplate=rss",
        )
        message = parse_veritawire_message(
            '{"id":"truth-fixture-1","created_at":"2026-07-18T16:00:00Z",'
            '"account":{"username":"realDonaldTrump"},"content":"<p>Fixture post</p>"}'
        )
        _record(
            results,
            len(parsed.items) == 1
            and message[0].source_fact_id == "truth-fixture-1",
            "connector-fixtures",
            "synthetic RSS and VeritaWire envelope parsing passed",
        )
    except Exception as exc:  # noqa: BLE001 - never print fixture/provider bodies.
        _record(results, False, "connector-fixtures", type(exc).__name__)


def _check_offline_source_collectors(
    root: Path,
) -> tuple[ExternalEventArchive, ExternalSourceRevision, dict[str, datetime]]:
    from market_relay_engine.context.external_sources import (
        BoundedHTTPClient,
        EarningsDiscoveryAdapter,
        EarningsSettings,
        ExternalHTTPSettings,
        LockheedMartinRSSAdapter,
        PalantirIRAdapter,
        PalantirIRSettings,
    )
    from market_relay_engine.context.veritawire import (
        VeritaWireConnector,
        VeritaWireSettings,
    )

    clock = {"value": datetime(2026, 7, 18, 16, 1, tzinfo=UTC)}
    now = lambda: clock["value"]
    archive = ExternalEventArchive(root / "archive", now=now)

    socket_message = json.dumps(
        {
            "id": "truth-offline-check-1",
            "created_at": "2026-07-18T16:00:50Z",
            "account": {"username": "realDonaldTrump"},
            "content": "<p>Lockheed Martin and Palantir defense update.</p>",
            "url": (
                "https://truthsocial.com/@realDonaldTrump/"
                "truth-offline-check-1"
            ),
        }
    )
    connect_factory = _OfflineConnectFactory([socket_message])
    connector = VeritaWireConnector(
        settings=VeritaWireSettings(enabled=True, max_reconnect_attempts=0),
        archive=archive,
        connect_factory=connect_factory,
        now=now,
        api_key="synthetic-offline-key",
    )
    received = asyncio.run(connector.run(max_messages=1, timeout_seconds=1.0))
    if received != 1 or len(connect_factory.calls) != 1:
        raise AssertionError("fake VeritaWire lifecycle did not receive one message")

    def client(responses: list[_OfflineHTTPResponse]) -> BoundedHTTPClient:
        return BoundedHTTPClient(
            ExternalHTTPSettings(
                user_agent="Market Relay offline source checker",
                timeout_seconds=1.0,
                max_retries=0,
                retry_base_delay_seconds=0.01,
                retry_max_delay_seconds=0.01,
                max_response_bytes=100_000,
            ),
            session=_OfflineHTTPSession(responses),  # type: ignore[arg-type]
            now=now,
            sleeper=lambda _delay: None,
        )

    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    lmt_url = "https://news.lockheedmartin.com/offline-check-release"
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <title>Lockheed Martin</title><item><guid>lmt-offline-check-1</guid>
      <title>Official program update</title><link>{lmt_url}</link>
      <pubDate>Sat, 18 Jul 2026 16:00:00 GMT</pubDate></item>
      </channel></rss>""".encode()
    article = (
        '<html><div class="wd_body wd_news_body fr-view">'
        "<p>Lockheed Martin received an official contract.</p></div></html>"
    ).encode()
    lmt_result = LockheedMartinRSSAdapter(
        client=client(
            [
                _OfflineHTTPResponse(
                    content=feed, content_type="text/xml", url=feed_url
                ),
                _OfflineHTTPResponse(
                    content=article, content_type="text/html", url=lmt_url
                ),
            ]
        ),
        archive=archive,
        now=now,
    ).collect_once(max_items=1)
    if lmt_result.new_count != 1:
        raise AssertionError("LMT RSS fixture did not archive one release")

    pltr_settings = PalantirIRSettings()
    pltr_endpoint = pltr_settings.endpoint_template.format(year=2026)
    pltr_payload = json.dumps(
        {
            "GetPressReleaseListResult": [
                {
                    "PressReleaseId": 901,
                    "RevisionNumber": 1,
                    "Headline": "Palantir official update",
                    "Body": "<p>Palantir announced an official contract.</p>",
                    "LinkToDetailPage": (
                        "/news-details/2026/offline-check-release"
                    ),
                    "PressReleaseDate": "07/18/2026 12:00:00",
                }
            ]
        }
    ).encode()
    pltr_result = PalantirIRAdapter(
        client=client(
            [
                _OfflineHTTPResponse(
                    content=pltr_payload,
                    content_type="application/json",
                    url=pltr_endpoint,
                )
            ]
        ),
        archive=archive,
        settings=pltr_settings,
        now=now,
    ).collect_once(year=2026, max_items=1)
    if pltr_result.new_count != 1:
        raise AssertionError("PLTR IR fixture did not archive one release")

    earnings_page_url = (
        "https://investors.lockheedmartin.com/financial-information/"
        "quarterly-results"
    )
    earnings_release_url = (
        "https://investors.lockheedmartin.com/offline-check-q1-release"
    )
    earnings_page = b"""<html><section><h2>First Quarter 2026</h2>
      <a href="/offline-check-q1-release">Press Release</a>
      <a href="/offline-check-q1-webcast">Webcast</a></section></html>"""
    earnings_release = (
        "<html><main><h1>Quarterly results</h1>"
        "<p>Lockheed Martin issued earnings guidance.</p></main></html>"
    ).encode()
    earnings_client = client(
        [
            _OfflineHTTPResponse(
                content=earnings_page,
                content_type="text/html",
                url=earnings_page_url,
            ),
            _OfflineHTTPResponse(
                content=earnings_release,
                content_type="text/html",
                url=earnings_release_url,
            ),
        ]
    )
    earnings_result = EarningsDiscoveryAdapter(
        client=earnings_client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(
            client=earnings_client,
            archive=archive,
            now=now,
        ),
        settings=EarningsSettings(lmt_results_url=earnings_page_url),
        now=now,
    ).collect_once(ticker="LMT", max_items=1)
    if earnings_result.new_count != 1:
        raise AssertionError("earnings fixture did not archive one release")

    counts = {
        source: len(tuple(archive.iter_revisions(sources=(source,))))
        for source in (
            "veritawire_truth_social",
            "lockheed_martin_rss",
            "palantir_ir",
            "company_earnings",
        )
    }
    if counts != {
        "veritawire_truth_social": 1,
        "lockheed_martin_rss": 1,
        "palantir_ir": 1,
        "company_earnings": 1,
    }:
        raise AssertionError("offline source revision counts changed")
    lmt_revision = next(
        archive.iter_revisions(sources=("lockheed_martin_rss",))
    )
    return archive, lmt_revision, clock


def _check_offline_classification_projection(
    *,
    archive: ExternalEventArchive,
    revision: ExternalSourceRevision,
    clock: dict[str, datetime],
) -> None:
    from market_relay_engine.ai_context.classifier import (
        ContextClassificationAttemptResult,
    )
    from market_relay_engine.context.decision_context import DecisionContextAssembler
    from market_relay_engine.context.external_classification import (
        ExternalClassificationPipeline,
    )
    from market_relay_engine.context.external_normalization import (
        EXCERPT_VERSION,
        SCOPE_RESOLVER_VERSION,
    )
    from market_relay_engine.context.research_projection import (
        EvidenceCategory,
        ResearchAvailabilityMode,
        ResearchClassificationProfile,
        ResearchRunDefinition,
        ResearchSourceClassificationProfile,
        ResearchSourceCoverageProfile,
        hydrate_external_research_evidence,
    )
    from market_relay_engine.context.shadow_evaluation import evaluate_shadow_context
    from market_relay_engine.context.state_cache import ContextStateCache
    from market_relay_engine.contracts.context import (
        ContextClassificationEventType,
        ContextClassificationResponse,
        ContextClassificationStatus,
        ContextRiskLevel,
        ContextUrgency,
        ContextValidationResult,
        ShadowContextAction,
    )
    from market_relay_engine.contracts.model import ModelSignal, SignalSide
    from market_relay_engine.common.serialization import to_json_string
    from market_relay_engine.context.external_event_archive import (
        CoverageInterval,
        CoverageStatus,
        SourceCoverage,
    )

    observed_at = revision.system_observed_at
    ready_at = observed_at + timedelta(seconds=4)
    clock["value"] = ready_at

    profile = ResearchSourceClassificationProfile(
        source=revision.source,
        source_type=revision.source_type,
        ticker="LMT",
        semantic_adapter_version=revision.adapter_version,
        extraction_version=revision.extractor_version,
        normalization_version=revision.normalizer_version,
        excerpt_version=EXCERPT_VERSION,
        scope_version=SCOPE_RESOLVER_VERSION,
        prompt_version="context_filter_v2_scope",
        model_version="offline-fixture-model",
        response_schema_version="context_classification_response_v2",
        validator_version="context_filter_validator_v2_scope",
        classification_config_hash=sha256(b"offline-check-profile").hexdigest(),
    )

    class Classifier:
        def __init__(self, *, forbid: bool = False) -> None:
            self.calls = 0
            self.forbid = forbid

        def classify(self, request: object) -> ContextClassificationAttemptResult:
            if self.forbid:
                raise AssertionError("durable classification was not reused")
            self.calls += 1
            request_id = str(getattr(request, "classification_request_id"))
            attempt_id = "offline-check-attempt"
            return ContextClassificationAttemptResult(
                response=ContextClassificationResponse(
                    classification_request_id=request_id,
                    classification_attempt_id=attempt_id,
                    classified_at=ready_at,
                    provider="offline-fixture",
                    model_version=profile.model_version,
                    prompt_version=profile.prompt_version,
                    response_schema_version=profile.response_schema_version,
                    status=ContextClassificationStatus.VALID,
                    event_type=ContextClassificationEventType.GOVERNMENT_CONTRACT,
                    risk_level=ContextRiskLevel.LOW,
                    urgency=ContextUrgency.LOW,
                    confidence=0.8,
                    summary="A bounded offline official-source fixture.",
                    affected_tickers=["LMT"],
                    affected_sectors=["DEFENSE"],
                    global_relevance=False,
                    provider_latency_ms=1.0,
                    provider_request_count=1,
                    retry_count=0,
                ),
                validation_result=ContextValidationResult(
                    classification_request_id=request_id,
                    classification_attempt_id=attempt_id,
                    validation_outcome=True,
                    reason_codes=[],
                    validator_version=profile.validator_version,
                    validated_at=ready_at,
                ),
            )

    first_classifier = Classifier()
    pipeline = ExternalClassificationPipeline(
        archive=archive,
        classifier=first_classifier,
        profile=profile,
        approved_tickers=("LMT", "PLTR"),
        approved_sectors=("DEFENSE", "ENERGY"),
        ticker_sector_hints={"LMT": "DEFENSE", "PLTR": "DEFENSE"},
        max_input_characters=12_000,
        now=lambda: clock["value"],
    )
    outcome = pipeline.process_revision(revision, title=revision.source_title)
    if outcome.status != "VALID" or first_classifier.calls != 1:
        raise AssertionError("offline classification did not publish one valid result")

    reuse_classifier = Classifier(forbid=True)
    reused = ExternalClassificationPipeline(
        archive=archive,
        classifier=reuse_classifier,
        profile=profile,
        approved_tickers=("LMT", "PLTR"),
        approved_sectors=("DEFENSE", "ENERGY"),
        ticker_sector_hints={"LMT": "DEFENSE", "PLTR": "DEFENSE"},
        max_input_characters=12_000,
        now=lambda: clock["value"],
    ).process_revision(revision, title=revision.source_title)
    if reused.status != "VALID" or reused.provider_called:
        raise AssertionError("restart-safe classification suppression failed")

    coverage_start = observed_at - timedelta(minutes=1)
    coverage_end = ready_at + timedelta(minutes=1)
    coverage = SourceCoverage(
        source=revision.source,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        coverage_status=CoverageStatus.COMPLETE_FOR_RANGE,
        completed_backfill_ranges=(
            CoverageInterval(start=coverage_start, end=coverage_end),
        ),
        bootstrap_time=observed_at,
        live_collection_start=observed_at,
        last_verification_time=ready_at,
        coverage_generation=1,
        coverage_version="external_coverage_v1",
    )
    archive.save_coverage(coverage)
    manifest = archive.load_manifest()
    resolutions = archive.load_resolution_manifest()
    payload_hash = lambda value: sha256(
        to_json_string(value).encode("utf-8")
    ).hexdigest()
    run_definition = ResearchRunDefinition(
        ticker_universe=("LMT", "PLTR"),
        event_sources=(revision.source,),
        evidence_categories=(EvidenceCategory.AI_EVENT,),
        hydration_start_time=coverage_start,
        hydration_end_time=coverage_end,
        capacity=20,
        classification_profile=ResearchClassificationProfile(
            extraction_version="sec_8k_items_v1",
            prompt_version="context_filter_v1",
            model_version="unused-offline-sec-model",
            response_schema_version="context_classification_response_v1",
            classification_config_hash=sha256(b"unused-sec-profile").hexdigest(),
        ),
        max_age_without_valid_until=timedelta(seconds=30),
        selection_policy_version="external_offline_check_v1",
        availability_mode=ResearchAvailabilityMode.LIVE_SYSTEM_READY,
        external_classification_profiles=(profile,),
        source_coverage_profiles=(
            ResearchSourceCoverageProfile(
                source=revision.source,
                ticker="LMT",
                semantic_adapter_version=revision.adapter_version,
                coverage_manifest_source=revision.source,
                coverage_generation=coverage.coverage_generation,
                coverage_version=coverage.coverage_version,
            ),
        ),
        conflict_resolution_generation=int(resolutions["generation"]),
        conflict_resolution_manifest_hash=payload_hash(resolutions),
        lifecycle_version="external_lifecycle_v1",
        correlation_version="external_correlation_v1",
        external_archive_generation=int(manifest["generation"]),
        external_archive_manifest_hash=payload_hash(manifest),
    )
    index = hydrate_external_research_evidence(
        archive=ExternalEventArchive(archive.root, now=lambda: clock["value"]),
        run_definition=run_definition,
    )
    assembler = DecisionContextAssembler(cache=ContextStateCache())
    before_context = assembler.build_for_decision(
        "LMT",
        observed_at + timedelta(seconds=2),
        "offline-check-before",
        None,
        ticker_sector="DEFENSE",
    )
    if index.select(before_context).selected_evidence:
        raise AssertionError("evidence selected before durable readiness")
    ready_context = assembler.build_for_decision(
        "LMT",
        ready_at,
        "offline-check-ready",
        None,
        ticker_sector="DEFENSE",
    )
    ready_selection = index.select(ready_context)
    if len(ready_selection.selected_evidence) != 1:
        raise AssertionError("ready evidence was not selected exactly once")
    signal = ModelSignal(
        signal_time=ready_at,
        ticker="LMT",
        signal=SignalSide.BUY,
        confidence=0.7,
        raw_score=0.2,
        model_version="offline-model-v1",
        calibration_version="offline-calibration-v1",
        feature_version="offline-features-v1",
        feature_snapshot_id="offline-feature-snapshot",
        signal_id="offline-signal",
        trace_id="offline-check-ready",
    )
    evaluation = evaluate_shadow_context(
        model_signal=signal,
        decision_context=ready_context,
        evidence_selection=ready_selection,
        risk_decision=None,
    )
    if evaluation.hypothetical_action is not ShadowContextAction.NO_CHANGE:
        raise AssertionError("default shadow action changed from NO_CHANGE")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    issue = validate_arguments(args)
    if issue is not None:
        print(f"External event source check FAIL: {issue}")
        return 2

    if not args.live:
        results = run_offline_checks()
        for result in results:
            print(f"[{'PASS' if result.ok else 'FAIL'}] {result.label}: {result.detail}")
        failures = [result for result in results if not result.ok]
        if failures:
            print(f"External event source offline check FAILED ({len(failures)} failure(s)).")
            return 1
        print("External event source offline check PASSED.")
        print("network=false gemini=false questdb=false alpaca=false risk_changes=false")
        return 0

    try:
        settings = load_external_event_source_settings(base_dir=REPO_ROOT)
        result = _run_live(args, settings)
    except Exception as exc:  # noqa: BLE001 - redacted live boundary.
        print(f"External event source live check FAIL: {type(exc).__name__}")
        return 1
    for key in sorted(result):
        value = result[key]
        if _safe_live_result_value(key, value):
            print(f"{key}={value}")
    print("External event source live check PASS")
    return 0


def _run_live(
    args: argparse.Namespace,
    settings: ExternalEventSourcesSettings,
) -> dict[str, object]:
    if args.source == "veritawire":
        return _run_live_veritawire(args, settings)
    return _run_live_http(args, settings)


def _run_live_veritawire(
    args: argparse.Namespace,
    settings: ExternalEventSourcesSettings,
) -> dict[str, object]:
    from market_relay_engine.context.veritawire import (
        VeritaWireConnector,
        VeritaWireSettings,
    )

    load_dotenv(REPO_ROOT / ".env", override=False)
    api_key = os.getenv(settings.veritawire.api_key_env)
    if not api_key:
        raise RuntimeError("VeritaWire API key is unavailable")
    connector_settings = VeritaWireSettings(
        websocket_url=settings.veritawire.websocket_url,
        api_key_env=settings.veritawire.api_key_env,
        enabled=True,
        connect_timeout_seconds=min(
            args.timeout_seconds, settings.veritawire.connect_timeout_seconds
        ),
        close_timeout_seconds=settings.veritawire.close_timeout_seconds,
        ping_interval_seconds=settings.veritawire.ping_interval_seconds,
        ping_timeout_seconds=settings.veritawire.ping_timeout_seconds,
        reconnect_base_delay_seconds=settings.veritawire.reconnect_base_delay_seconds,
        reconnect_max_delay_seconds=settings.veritawire.reconnect_max_delay_seconds,
        reconnect_jitter_fraction=settings.veritawire.reconnect_jitter_fraction,
        max_reconnect_attempts=settings.veritawire.max_reconnect_attempts,
        max_message_bytes=settings.veritawire.max_message_bytes,
        adapter_version=settings.veritawire.common.adapter_version,
        extractor_version=settings.veritawire.common.extraction_version,
    )
    connector = VeritaWireConnector(
        settings=connector_settings,
        archive=ExternalEventArchive(settings.archive_path),
        api_key=api_key,
    )
    raw_result = asyncio.run(
        connector.run(
            max_messages=min(args.max_items, settings.veritawire.max_records_per_run),
            timeout_seconds=min(args.timeout_seconds, settings.veritawire.smoke_timeout_seconds),
        )
    )
    result = _safe_result_mapping(
        raw_result,
        source="veritawire",
        classify=args.classify,
        questdb=args.questdb,
    )
    if args.classify:
        result.update(
            _classify_pending_revisions(
                archive=connector.archive,
                source="veritawire_truth_social",
                max_items=args.max_items,
                write_questdb=args.questdb,
            )
        )
    elif args.questdb:
        result["questdb_records"] = 0
    return result


def _run_live_http(
    args: argparse.Namespace,
    settings: ExternalEventSourcesSettings,
) -> dict[str, object]:
    from market_relay_engine.context.external_sources import (
        BoundedHTTPClient,
        EarningsDiscoveryAdapter,
        EarningsSettings,
        ExternalHTTPSettings,
        ExternalSourceError,
        LockheedMartinRSSAdapter,
        LockheedMartinRSSSettings,
        PalantirIRAdapter,
        PalantirIRSettings,
    )

    selected = {
        "lmt-rss": settings.lmt_rss,
        "pltr-ir": settings.pltr_ir,
        "earnings": settings.earnings,
    }[args.source]
    deadline = time.monotonic() + args.timeout_seconds

    def bounded_sleep(delay: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or delay >= remaining:
            raise ExternalSourceError("live source check deadline expired")
        time.sleep(delay)

    client = BoundedHTTPClient(
        ExternalHTTPSettings(
            user_agent=selected.http.user_agent,
            timeout_seconds=min(args.timeout_seconds, selected.http.timeout_seconds),
            max_retries=selected.http.max_retries,
            retry_base_delay_seconds=selected.http.retry_base_delay_seconds,
            retry_max_delay_seconds=selected.http.retry_max_delay_seconds,
            max_response_bytes=selected.http.max_response_bytes,
        ),
        sleeper=bounded_sleep,
        deadline_monotonic=deadline,
    )
    archive = ExternalEventArchive(settings.archive_path)
    if args.source == "lmt-rss":
        adapter: Any = LockheedMartinRSSAdapter(
            client=client,
            archive=archive,
            settings=LockheedMartinRSSSettings(
                feed_url=settings.lmt_rss.feed_url,
                fixed_ticker=settings.lmt_rss.ticker,
                max_feed_items=settings.lmt_rss.http.max_items_per_poll,
                official_domains=settings.lmt_rss.allowed_domains,
                adapter_version=settings.lmt_rss.common.adapter_version,
                extractor_version=settings.lmt_rss.common.extraction_version,
            ),
        )
    elif args.source == "pltr-ir":
        adapter = PalantirIRAdapter(
            client=client,
            archive=archive,
            settings=PalantirIRSettings(
                endpoint_template=settings.pltr_ir.release_list_url_template,
                fixed_ticker=settings.pltr_ir.ticker,
                max_items=settings.pltr_ir.http.max_items_per_poll,
                official_domains=settings.pltr_ir.allowed_domains,
                adapter_version=settings.pltr_ir.common.adapter_version,
                extractor_version=settings.pltr_ir.common.extraction_version,
            ),
        )
    else:
        palantir_adapter = PalantirIRAdapter(
            client=client,
            archive=archive,
            settings=PalantirIRSettings(
                endpoint_template=settings.pltr_ir.release_list_url_template,
                fixed_ticker=settings.pltr_ir.ticker,
                max_items=settings.pltr_ir.http.max_items_per_poll,
                official_domains=settings.pltr_ir.allowed_domains,
                adapter_version=settings.pltr_ir.common.adapter_version,
                extractor_version=settings.pltr_ir.common.extraction_version,
            ),
        )
        adapter = EarningsDiscoveryAdapter(
            client=client,
            archive=archive,
            palantir_ir=palantir_adapter,
            settings=EarningsSettings(
                pltr_events_url=settings.earnings.pltr_events_url,
                lmt_results_url=settings.earnings.lmt_results_url,
                max_items=settings.earnings.http.max_items_per_poll,
                pltr_domains=settings.earnings.pltr_allowed_domains,
                lmt_domains=settings.earnings.lmt_allowed_domains,
                adapter_version=settings.earnings.common.adapter_version,
                pdf_extraction_version=settings.earnings.pdf_extraction_version,
                max_pdf_pages=settings.earnings.max_pdf_pages,
                max_pdf_text_characters=(
                    settings.earnings.max_pdf_text_characters
                ),
            ),
        )

    max_items = min(args.max_items, selected.http.max_items_per_poll)
    kwargs: dict[str, object] = {
        "max_items": max_items,
        "establish_checkpoint": args.establish_checkpoint,
    }
    if args.source == "pltr-ir":
        kwargs["year"] = datetime.now(UTC).year
    if args.source == "earnings":
        kwargs["ticker"] = args.ticker
    if args.backfill:
        assert args.start_time is not None and args.end_time is not None
        backfill_kwargs: dict[str, object] = {
            "start_time": args.start_time,
            "end_time": args.end_time,
            "max_items": max_items,
        }
        if args.source == "earnings":
            backfill_kwargs["ticker"] = args.ticker
        raw_result = adapter.collect_backfill(**backfill_kwargs)
    elif args.poll:
        from market_relay_engine.context.external_sources import BoundedSourcePoller

        if args.source == "lmt-rss":
            interval = settings.lmt_rss.poll_interval_seconds
        elif args.source == "pltr-ir":
            interval = settings.pltr_ir.poll_interval_seconds
        else:
            interval = settings.earnings.fast_poll_interval_seconds
        binding = _CollectOnceBinding(
            collect=lambda *, max_items: adapter.collect_once(
                **{**kwargs, "max_items": max_items}
            )
        )
        raw_result = BoundedSourcePoller(
            binding,
            poll_interval_seconds=interval,
            sleeper=bounded_sleep,
            coverage_archive=archive,
            coverage_source=_poll_coverage_source(args.source, args.ticker),
        ).run(
            max_polls=args.max_polls,
            max_items=max_items,
        )
    else:
        raw_result = adapter.collect_once(**kwargs)
    result = _safe_result_mapping(
        raw_result,
        source=args.source,
        classify=args.classify,
        questdb=args.questdb,
    )
    if args.classify:
        source_name = {
            "lmt-rss": "lockheed_martin_rss",
            "pltr-ir": "palantir_ir",
            "earnings": "company_earnings",
        }[args.source]
        result.update(
            _classify_pending_revisions(
                archive=archive,
                source=source_name,
                max_items=max_items,
                write_questdb=args.questdb,
            )
        )
    elif args.questdb:
        result["questdb_records"] = 0
    return result


def _classify_pending_revisions(
    *,
    archive: ExternalEventArchive,
    source: str,
    max_items: int,
    write_questdb: bool,
) -> dict[str, object]:
    """Run the existing classifier only for bounded current pending revisions."""

    revisions = _current_classifiable_revisions(archive=archive, source=source)
    if not revisions:
        return {
            "classification_candidates": 0,
            "classification_completed": 0,
            "classification_pending": 0,
            "provider_calls": 0,
            "canonical_reuses": 0,
            "questdb_records": 0,
        }

    from market_relay_engine.ai_context import (
        GeminiContextClassifier,
        load_ai_context_filter_settings,
    )
    from market_relay_engine.common.config import load_yaml_config
    from market_relay_engine.context.external_classification import (
        ExternalClassificationPipeline,
    )
    from market_relay_engine.context.research_projection import (
        ResearchSourceClassificationProfile,
    )

    ai_settings = load_ai_context_filter_settings(
        base_dir=REPO_ROOT,
        enabled_override=True,
    )
    ai_settings = replace(
        ai_settings,
        prompt_version="context_filter_v2_scope",
        response_schema_version="context_classification_response_v2",
    )
    ticker_sector_hints = _ticker_sector_hints(
        load_yaml_config("symbols", base_dir=REPO_ROOT)
    )
    candidates = _pending_classification_candidates(
        archive=archive,
        revisions=revisions,
        max_items=max_items,
        ai_settings=ai_settings,
        ticker_sector_hints=ticker_sector_hints,
    )
    if not candidates:
        return {
            "classification_candidates": 0,
            "classification_completed": 0,
            "classification_pending": 0,
            "provider_calls": 0,
            "canonical_reuses": 0,
            "questdb_records": 0,
        }

    load_dotenv(REPO_ROOT / ".env", override=False)
    api_key = os.getenv(ai_settings.api_key_env)
    if not api_key:
        raise RuntimeError("Gemini API key is unavailable")
    classifier = GeminiContextClassifier(
        ai_settings,
        api_key=api_key,
        ticker_sector_hints=ticker_sector_hints,
    )
    writer = _build_metadata_writer() if write_questdb else None
    outcomes = []
    try:
        for candidate in candidates:
            revision = candidate.revision
            profile = candidate.profile
            if not isinstance(profile, ResearchSourceClassificationProfile):
                raise RuntimeError("pending classification profile is invalid")
            pipeline = ExternalClassificationPipeline(
                archive=archive,
                classifier=classifier,
                profile=profile,
                approved_tickers=tuple(sorted(ticker_sector_hints)),
                approved_sectors=tuple(sorted(set(ticker_sector_hints.values()))),
                ticker_sector_hints=ticker_sector_hints,
                max_input_characters=ai_settings.max_input_characters,
                questdb_writer=writer,
            )
            outcomes.append(
                pipeline.process_revision(
                    revision,
                    title=revision.source_title,
                    earnings=revision.source == "company_earnings",
                )
            )
    finally:
        classifier.close()
    return {
        "classification_candidates": len(candidates),
        "classification_completed": sum(
            value.status in {"VALID", "ABSTAINED"} for value in outcomes
        ),
        "classification_pending": sum(value.evidence_ready_at is None for value in outcomes),
        "provider_calls": sum(value.provider_called for value in outcomes),
        "canonical_reuses": sum(value.reused_canonical_result for value in outcomes),
        "questdb_records": sum(
            value.context_event is not None for value in outcomes
        )
        if write_questdb
        else 0,
    }


def _current_classifiable_revisions(
    *,
    archive: ExternalEventArchive,
    source: str,
) -> tuple[ExternalSourceRevision, ...]:
    current: dict[str, ExternalSourceRevision] = {}
    for revision in archive.iter_revisions(sources=(source,)):
        existing = current.get(revision.source_fact_id)
        if existing is None or (
            revision.system_observed_at,
            revision.revision_sequence,
            revision.source_revision_id,
        ) > (
            existing.system_observed_at,
            existing.revision_sequence,
            existing.source_revision_id,
        ):
            current[revision.source_fact_id] = revision
    candidates = [
        value
        for value in current.values()
        if value.normalized_text_hash is not None
        and value.lifecycle_state in {LifecycleState.ACTIVE, LifecycleState.UPDATED}
    ]
    candidates.sort(
        key=lambda value: (
            value.system_observed_at,
            value.source_fact_id,
            value.source_revision_id,
        ),
        reverse=True,
    )
    return tuple(candidates)


def _pending_classification_candidates(
    *,
    archive: ExternalEventArchive,
    revisions: tuple[ExternalSourceRevision, ...],
    max_items: int,
    ai_settings: Any,
    ticker_sector_hints: dict[str, str],
) -> tuple[_PendingClassificationCandidate, ...]:
    """Filter exact-profile durable completions before applying the work bound."""

    from market_relay_engine.ai_context.classifier import VALIDATOR_VERSION_V2
    from market_relay_engine.context.external_classification import (
        ExternalClassificationPipeline,
    )

    actionable: list[_PendingClassificationCandidate] = []
    config_hash = _classification_config_hash(ai_settings, ticker_sector_hints)
    approved_tickers = tuple(sorted(ticker_sector_hints))
    approved_sectors = tuple(sorted(set(ticker_sector_hints.values())))
    for revision in revisions:
        profile = _classification_profile_for_revision(
            revision,
            ai_settings=ai_settings,
            validator_version=VALIDATOR_VERSION_V2,
            classification_config_hash=config_hash,
        )
        pipeline = ExternalClassificationPipeline(
            archive=archive,
            classifier=_PreparationOnlyClassifier(),
            profile=profile,
            approved_tickers=approved_tickers,
            approved_sectors=approved_sectors,
            ticker_sector_hints=ticker_sector_hints,
            max_input_characters=ai_settings.max_input_characters,
        )
        prepared = pipeline.prepare(
            revision,
            title=revision.source_title,
            earnings=revision.source == "company_earnings",
        )
        if prepared is None:
            continue
        fingerprint = prepared.request.classification_input_fingerprint
        if fingerprint is None:
            raise RuntimeError("prepared classification fingerprint is unavailable")
        readiness = archive.read_readiness(
            revision.source_revision_id,
            classification_input_fingerprint=fingerprint,
        )
        if readiness is not None and readiness.get("classification_status") in {
            "VALID",
            "ABSTAINED",
        }:
            continue
        actionable.append(
            _PendingClassificationCandidate(
                revision=revision,
                profile=profile,
                classification_input_fingerprint=fingerprint,
            )
        )
        if len(actionable) >= max_items:
            break
    return tuple(actionable)


def _classification_profile_for_revision(
    revision: ExternalSourceRevision,
    *,
    ai_settings: Any,
    validator_version: str,
    classification_config_hash: str,
) -> Any:
    from market_relay_engine.context.research_projection import (
        ResearchSourceClassificationProfile,
    )

    return ResearchSourceClassificationProfile(
        source=revision.source,
        source_type=revision.source_type,
        ticker=(
            revision.affected_tickers[0]
            if revision.source
            in {
                "lockheed_martin_rss",
                "palantir_ir",
                "company_earnings",
            }
            and len(revision.affected_tickers) == 1
            else None
        ),
        semantic_adapter_version=revision.adapter_version,
        extraction_version=revision.extractor_version,
        normalization_version=revision.normalizer_version,
        excerpt_version="scope_aware_excerpt_v1",
        scope_version="external_scope_v2",
        prompt_version=ai_settings.prompt_version,
        model_version=ai_settings.model,
        response_schema_version=ai_settings.response_schema_version,
        validator_version=validator_version,
        classification_config_hash=classification_config_hash,
    )


def _ticker_sector_hints(symbol_config: dict[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    tradable = symbol_config.get("tradable_universe")
    if not isinstance(tradable, dict):
        raise RuntimeError("tradable universe is unavailable")
    for sector_name, sector in tradable.items():
        if not isinstance(sector, dict) or not isinstance(sector.get("symbols"), list):
            raise RuntimeError("tradable universe has invalid shape")
        canonical_sector = "ENERGY" if str(sector_name).lower() == "oil" else str(sector_name).upper()
        for entry in sector["symbols"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
                raise RuntimeError("tradable universe ticker is invalid")
            hints[entry["ticker"].upper()] = canonical_sector
    return dict(sorted(hints.items()))


def _classification_config_hash(
    settings: object,
    ticker_sector_hints: dict[str, str],
) -> str:
    canonical_hints = {
        str(ticker).upper(): str(sector).upper()
        for ticker, sector in sorted(ticker_sector_hints.items())
    }
    payload = {
        "settings": asdict(settings),  # type: ignore[arg-type]
        "approved_tickers": sorted(canonical_hints),
        "approved_sectors": sorted(set(canonical_hints.values())),
        "ticker_sector_hints": canonical_hints,
    }
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _build_metadata_writer() -> object:
    from market_relay_engine.questdb.jsonl_fallback import (
        EmergencyJSONLLedgerFallback,
    )
    from market_relay_engine.questdb.writer import QuestDBLedgerWriter

    return _ContextAIEventWriter(
        writer=QuestDBLedgerWriter(),
        fallback=EmergencyJSONLLedgerFallback(),
    )


@dataclass(frozen=True)
class _ContextAIEventWriter:
    writer: object
    fallback: object

    def write(self, record: object) -> object:
        method = getattr(self.writer, "write_context_ai_event", None)
        if not callable(method):
            raise RuntimeError("QuestDB context event writer is unavailable")
        try:
            return method(record)
        except Exception as exc:  # noqa: BLE001 - durable metadata fallback boundary.
            from market_relay_engine.questdb.writer import context_ai_event_to_row

            row = context_ai_event_to_row(record)
            forbidden = {
                "raw_text",
                "raw_payload",
                "source_body",
                "prompt",
                "provider_body",
                "api_key",
                "authorization",
            }
            if forbidden.intersection(row):
                raise RuntimeError("QuestDB row contains forbidden raw fields") from exc
            append = getattr(self.fallback, "append_record", None)
            if not callable(append):
                raise RuntimeError("emergency metadata fallback is unavailable") from exc
            tickers = tuple(getattr(record, "affected_tickers", ()))
            sectors = tuple(getattr(record, "affected_sectors", ()))
            scope = ",".join((*tickers, *sectors)) or (
                "GLOBAL" if getattr(record, "global_relevance", False) else "UNSCOPED"
            )
            return append(
                record_type="ContextAIEvent",
                target_table="context_ai_events",
                record_id=str(getattr(record, "context_event_id")),
                event_time=getattr(record, "event_time"),
                source=str(getattr(record, "source")),
                ticker_or_sector=scope,
                primary_write_failure={
                    "category": "QUESTDB_WRITE_FAILED",
                    "exception_type": type(exc).__name__,
                },
                payload=row,
            )


def _safe_result_mapping(
    value: object,
    *,
    source: str,
    classify: bool,
    questdb: bool,
) -> dict[str, object]:
    result: dict[str, object] = {
        "source": source,
        "mode": "research_only",
        "classification_requested": classify,
        "questdb_requested": questdb,
        "alpaca": False,
        "risk_changes": False,
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if _safe_live_result_value(str(key), child):
                result[str(key)] = child
    elif isinstance(value, tuple):
        result["polls"] = len(value)
        if value:
            result.update(_source_result_fields(value[-1]))
    elif value is not None:
        result.update(_source_result_fields(value))
        if isinstance(value, int):
            result["messages_received"] = value
    return result


def _source_result_fields(value: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in (
        "not_modified",
        "discovered_count",
        "new_count",
        "duplicate_count",
        "revision_count",
        "skipped_count",
        "pending_count",
        "checkpoint_generation",
    ):
        child = getattr(value, key, None)
        if _safe_live_result_value(key, child):
            result[key] = child
    return result


@dataclass(frozen=True)
class _CollectOnceBinding:
    collect: Callable[..., object]

    def collect_once(self, *, max_items: int) -> object:
        return self.collect(max_items=max_items)


def _poll_coverage_source(source: str, ticker: str | None) -> str:
    if source == "earnings":
        if ticker not in {"PLTR", "LMT"}:
            raise RuntimeError("earnings polling coverage requires a reviewed ticker")
        return f"company_earnings:{ticker}"
    try:
        return {
            "lmt-rss": "lockheed_martin_rss",
            "pltr-ir": "palantir_ir",
        }[source]
    except KeyError as exc:
        raise RuntimeError("HTTP polling coverage source is invalid") from exc


def _safe_live_result_value(key: str, value: object) -> bool:
    lowered = key.lower()
    forbidden = (
        "key",
        "authorization",
        "credential",
        "secret",
        "token",
        "body",
        "content",
        "payload",
        "prompt",
        "exception",
        "traceback",
        "url",
    )
    if any(marker in lowered for marker in forbidden):
        return False
    return value is None or isinstance(value, (str, int, float, bool))


def _record(
    results: list[CheckResult],
    ok: bool,
    label: str,
    detail: str,
) -> None:
    results.append(CheckResult(ok=ok, label=label, detail=detail))


def _timestamp_argument(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
