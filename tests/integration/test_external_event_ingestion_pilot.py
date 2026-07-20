"""Offline end-to-end acceptance coverage for the external-event pilot."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

import scripts.check_external_event_sources as source_checker
from market_relay_engine.ai_context.classifier import (
    GeminiContextClassifier,
    VALIDATOR_VERSION_V2,
)
from market_relay_engine.ai_context.prompting import CONTEXT_FILTER_PROMPT_VERSION_V2
from market_relay_engine.ai_context.runtime_guards import (
    ClassificationDedupCache,
    ProviderCallBudget,
)
from market_relay_engine.ai_context.schema import (
    CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
)
from market_relay_engine.ai_context.settings import load_ai_context_filter_settings
from market_relay_engine.common.serialization import to_json_string
from market_relay_engine.context.decision_context import DecisionContextAssembler
from market_relay_engine.context.external_classification import (
    ExternalClassificationPipeline,
)
from market_relay_engine.context.external_event_archive import (
    CoverageInterval,
    CoverageStatus,
    ExternalEventArchive,
    SourceCoverage,
)
from market_relay_engine.context.external_normalization import (
    EXCERPT_VERSION,
    SCOPE_RESOLVER_VERSION,
)
from market_relay_engine.context.external_sources import (
    BoundedHTTPClient,
    EarningsDiscoveryAdapter,
    EarningsSettings,
    ExternalHTTPSettings,
    LockheedMartinRSSAdapter,
    PalantirIRAdapter,
    PalantirIRSettings,
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
from market_relay_engine.context.veritawire import (
    VeritaWireConnector,
    VeritaWireSettings,
)
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextClassificationRequest,
    ContextRawInput,
    ContextSourceDocument,
    ShadowContextAction,
)
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.execution.alpaca_paper import AlpacaPaperClient
from market_relay_engine.risk import risk_filter as risk_filter_module


REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVED_AT = datetime(2026, 7, 18, 16, 1, tzinfo=UTC)
READY_AT = OBSERVED_AT + timedelta(seconds=4)
RAW_BODY_SENTINEL = "raw-source-body-must-never-enter-ledger"
API_KEY_SENTINEL = "offline-api-key-must-never-enter-ledger"


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class FakeResponse:
    def __init__(
        self,
        *,
        content: bytes,
        content_type: str,
        url: str,
    ) -> None:
        self.status_code = 200
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.url = url

    def iter_content(self, *, chunk_size: int):
        del chunk_size
        yield self.content

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("offline fixture attempted an unexpected HTTP request")
        return self.responses.popleft()


class FakeGeminiTransport:
    """One deterministic provider response through the existing classifier."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            status="completed",
            model="gemini-test",
            output_text=json.dumps(self.payload, separators=(",", ":")),
            steps=None,
        )


def _forbid_live_request(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("offline external-event acceptance test attempted network I/O")


def _http_client(
    responses: list[FakeResponse],
    *,
    clock: MutableClock,
) -> BoundedHTTPClient:
    return BoundedHTTPClient(
        ExternalHTTPSettings(
            user_agent="Market Relay offline acceptance fixture",
            timeout_seconds=1,
            max_retries=0,
            retry_base_delay_seconds=0.01,
            retry_max_delay_seconds=0.01,
            max_response_bytes=100_000,
        ),
        session=FakeSession(responses),  # type: ignore[arg-type]
        now=clock,
        sleeper=lambda _delay: None,
    )


def _response(content: bytes, *, content_type: str, url: str) -> FakeResponse:
    return FakeResponse(content=content, content_type=content_type, url=url)


def _veritawire_payload() -> str:
    return json.dumps(
        {
            "id": "truth-e2e-1",
            "created_at": "2026-07-18T16:00:50Z",
            "content": (
                "<p>Lockheed Martin and Palantir support the defense industrial base.</p>"
                f"<p>{RAW_BODY_SENTINEL}</p>"
            ),
            "account": {"username": "realDonaldTrump"},
            "url": "https://truthsocial.com/@realDonaldTrump/truth-e2e-1",
            "media_attachments": [],
        }
    )


def _collect_source_revision(
    root: Path,
    *,
    source_case: str,
    clock: MutableClock,
) -> tuple[ExternalEventArchive, object, str, str]:
    archive = ExternalEventArchive(root, now=clock)
    if source_case == "veritawire":
        connector = VeritaWireConnector(
            settings=VeritaWireSettings(enabled=True),
            archive=archive,
            now=clock,
            api_key=API_KEY_SENTINEL,
        )
        connector.archive_message(_veritawire_payload())
        source = "veritawire_truth_social"
        ticker = "LMT"
        coverage_source = source
    elif source_case == "lmt-rss":
        feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
        article_url = "https://news.lockheedmartin.com/e2e-release"
        feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
          <title>Lockheed Martin</title><item><guid>lmt-e2e-1</guid>
          <title>Official program update</title><link>{article_url}</link>
          <pubDate>Sat, 18 Jul 2026 16:00:00 GMT</pubDate></item>
          </channel></rss>""".encode()
        article = (
            '<html><div class="wd_body wd_news_body fr-view">'
            f"<p>Lockheed Martin received an official contract. {RAW_BODY_SENTINEL}</p>"
            "</div></html>"
        ).encode()
        client = _http_client(
            [
                _response(feed, content_type="text/xml", url=feed_url),
                _response(article, content_type="text/html", url=article_url),
            ],
            clock=clock,
        )
        LockheedMartinRSSAdapter(client=client, archive=archive, now=clock).collect_once(
            max_items=1
        )
        source = "lockheed_martin_rss"
        ticker = "LMT"
        coverage_source = source
    elif source_case == "pltr-ir":
        settings = PalantirIRSettings()
        endpoint = settings.endpoint_template.format(year=2026)
        payload = json.dumps(
            {
                "GetPressReleaseListResult": [
                    {
                        "PressReleaseId": 901,
                        "RevisionNumber": 1,
                        "Headline": "Palantir official program update",
                        "Body": (
                            "<p>Palantir announced an official contract. "
                            f"{RAW_BODY_SENTINEL}</p>"
                        ),
                        "LinkToDetailPage": "/news-details/2026/e2e-release",
                        "PressReleaseDate": "07/18/2026 12:00:00",
                    }
                ]
            }
        ).encode()
        client = _http_client(
            [_response(payload, content_type="application/json", url=endpoint)],
            clock=clock,
        )
        PalantirIRAdapter(
            client=client,
            archive=archive,
            settings=settings,
            now=clock,
        ).collect_once(year=2026, max_items=1)
        source = "palantir_ir"
        ticker = "PLTR"
        coverage_source = source
    elif source_case == "earnings":
        page_url = (
            "https://investors.lockheedmartin.com/financial-information/"
            "quarterly-results"
        )
        release_url = "https://investors.lockheedmartin.com/e2e-q1-release"
        page = b"""<html><div><h2>First Quarter 2026</h2>
          <a href="/e2e-q1-release">Press Release</a>
          <a href="/e2e-q1-webcast">Webcast</a></div></html>"""
        release = (
            "<html><main><h1>Quarterly results</h1>"
            f"<p>Lockheed Martin issued earnings guidance. {RAW_BODY_SENTINEL}</p>"
            "</main></html>"
        ).encode()
        client = _http_client(
            [
                _response(page, content_type="text/html", url=page_url),
                _response(release, content_type="text/html", url=release_url),
            ],
            clock=clock,
        )
        palantir = PalantirIRAdapter(client=client, archive=archive, now=clock)
        EarningsDiscoveryAdapter(
            client=client,
            archive=archive,
            palantir_ir=palantir,
            settings=EarningsSettings(lmt_results_url=page_url),
            now=clock,
        ).collect_once(ticker="LMT", max_items=1)
        source = "company_earnings"
        ticker = "LMT"
        coverage_source = "company_earnings:LMT"
    else:  # pragma: no cover - parameter list owns the case universe.
        raise AssertionError(f"unsupported source case: {source_case}")

    revisions = tuple(archive.iter_revisions(sources=(source,)))
    assert len(revisions) == 1
    return archive, revisions[0], ticker, coverage_source


def _profile_for_revision(revision: object, *, model: str) -> ResearchSourceClassificationProfile:
    source = str(getattr(revision, "source"))
    fixed_ticker = (
        str(getattr(revision, "affected_tickers")[0])
        if source in {"lockheed_martin_rss", "palantir_ir", "company_earnings"}
        else None
    )
    return ResearchSourceClassificationProfile(
        source=source,
        source_type=str(getattr(revision, "source_type")),
        ticker=fixed_ticker,
        semantic_adapter_version=str(getattr(revision, "adapter_version")),
        extraction_version=str(getattr(revision, "extractor_version")),
        normalization_version=str(getattr(revision, "normalizer_version")),
        excerpt_version=EXCERPT_VERSION,
        scope_version=SCOPE_RESOLVER_VERSION,
        prompt_version=CONTEXT_FILTER_PROMPT_VERSION_V2,
        model_version=model,
        response_schema_version=CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
        validator_version=VALIDATOR_VERSION_V2,
        classification_config_hash=sha256(b"offline-e2e-classifier").hexdigest(),
    )


def _manifest_hash(payload: object) -> str:
    return sha256(to_json_string(payload).encode("utf-8")).hexdigest()


def _complete_coverage(
    archive: ExternalEventArchive,
    *,
    coverage_source: str,
) -> SourceCoverage:
    start = OBSERVED_AT - timedelta(minutes=1)
    end = READY_AT + timedelta(minutes=1)
    existing = archive.load_coverage(coverage_source)
    coverage = SourceCoverage(
        source=coverage_source,
        coverage_start=start,
        coverage_end=end,
        coverage_status=CoverageStatus.COMPLETE_FOR_RANGE,
        completed_backfill_ranges=(CoverageInterval(start=start, end=end),),
        bootstrap_time=OBSERVED_AT,
        live_collection_start=OBSERVED_AT,
        last_verification_time=READY_AT,
        coverage_generation=(
            1 if existing is None else existing.coverage_generation + 1
        ),
        coverage_version="external_coverage_v1",
    )
    archive.save_coverage(coverage)
    return coverage


def _run_definition(
    archive: ExternalEventArchive,
    *,
    profile: ResearchSourceClassificationProfile,
    coverage: SourceCoverage,
    coverage_source: str,
) -> ResearchRunDefinition:
    manifest = archive.load_manifest()
    resolutions = archive.load_resolution_manifest()
    return ResearchRunDefinition(
        ticker_universe=("LMT", "PLTR"),
        event_sources=(profile.source,),
        evidence_categories=(EvidenceCategory.AI_EVENT,),
        hydration_start_time=OBSERVED_AT - timedelta(minutes=1),
        hydration_end_time=READY_AT + timedelta(minutes=1),
        capacity=20,
        classification_profile=ResearchClassificationProfile(
            extraction_version="sec_8k_items_v1",
            prompt_version="context_filter_v1",
            model_version="gemini-test",
            response_schema_version="context_classification_response_v1",
            classification_config_hash=sha256(b"unused-sec-profile").hexdigest(),
        ),
        # The hydrated start must include the finite lookback.  A short test
        # lookback keeps both T+2 and T+4 inside complete index coverage.
        max_age_without_valid_until=timedelta(seconds=30),
        selection_policy_version="external_e2e_selection_v1",
        availability_mode=ResearchAvailabilityMode.LIVE_SYSTEM_READY,
        external_classification_profiles=(profile,),
        source_coverage_profiles=(
            ResearchSourceCoverageProfile(
                source=profile.source,
                ticker=profile.ticker,
                semantic_adapter_version=profile.semantic_adapter_version,
                coverage_manifest_source=coverage_source,
                coverage_generation=coverage.coverage_generation,
                coverage_version=coverage.coverage_version,
            ),
        ),
        conflict_resolution_generation=int(resolutions["generation"]),
        conflict_resolution_manifest_hash=_manifest_hash(resolutions),
        lifecycle_version="external_lifecycle_v1",
        correlation_version="external_correlation_v1",
        external_archive_generation=int(manifest["generation"]),
        external_archive_manifest_hash=_manifest_hash(manifest),
    )


def _classified_fixture(
    tmp_path: Path,
    *,
    source_case: str,
) -> tuple[
    ExternalEventArchive,
    object,
    ContextAIEvent,
    ResearchSourceClassificationProfile,
    str,
    FakeGeminiTransport,
]:
    clock = MutableClock(OBSERVED_AT)
    archive, revision, ticker, coverage_source = _collect_source_revision(
        tmp_path,
        source_case=source_case,
        clock=clock,
    )
    clock.value = READY_AT
    settings = replace(
        load_ai_context_filter_settings(
            base_dir=REPO_ROOT,
            enabled_override=True,
        ),
        model="gemini-test",
        prompt_version=CONTEXT_FILTER_PROMPT_VERSION_V2,
        response_schema_version=CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
        max_retries=0,
        max_provider_calls_per_minute=10,
        max_provider_calls_per_run=10,
    )
    event_type = (
        "EARNINGS_GUIDANCE"
        if source_case == "earnings"
        else "SOCIAL_POLITICAL_STATEMENT"
        if source_case == "veritawire"
        else "GOVERNMENT_CONTRACT"
    )
    provider_scope = [ticker]
    if source_case == "veritawire":
        provider_scope.append("PLTR")
    transport = FakeGeminiTransport(
        {
            "status": "VALID",
            "event_type": event_type,
            "risk_level": "MEDIUM",
            "urgency": "MEDIUM",
            "confidence": 0.8,
            "summary": "A synthetic research-only external event was classified.",
            "affected_tickers": sorted(set(provider_scope)),
            "affected_sectors": ["DEFENSE"],
            "global_relevance": False,
        }
    )
    classifier = GeminiContextClassifier(
        settings,
        api_key=API_KEY_SENTINEL,
        transport=transport,
        cache=ClassificationDedupCache(settings.dedup_cache_max_entries),
        budget=ProviderCallBudget(
            max_calls_per_minute=settings.max_provider_calls_per_minute,
            max_calls_per_run=settings.max_provider_calls_per_run,
        ),
        ticker_sector_hints={"LMT": "DEFENSE", "PLTR": "DEFENSE"},
        now=clock,
        monotonic_clock=lambda: 1.0,
        sleeper=lambda _delay: None,
        random_value=lambda: 0.0,
    )
    profile = _profile_for_revision(revision, model=settings.model)
    pipeline = ExternalClassificationPipeline(
        archive=archive,
        classifier=classifier,
        profile=profile,
        approved_tickers=("LMT", "PLTR"),
        approved_sectors=("DEFENSE",),
        ticker_sector_hints={"LMT": "DEFENSE", "PLTR": "DEFENSE"},
        max_input_characters=settings.max_input_characters,
        now=clock,
    )

    prepared = pipeline.prepare(
        revision,  # type: ignore[arg-type]
        title=getattr(revision, "source_title"),
        earnings=source_case == "earnings",
    )
    assert prepared is not None
    assert isinstance(prepared.raw_input, ContextRawInput)
    assert isinstance(prepared.source_document, ContextSourceDocument)
    assert isinstance(prepared.request, ContextClassificationRequest)
    assert prepared.request.source_revision_id == getattr(
        revision, "source_revision_id"
    )

    outcome = pipeline.process_revision(
        revision,  # type: ignore[arg-type]
        title=getattr(revision, "source_title"),
        earnings=source_case == "earnings",
    )
    assert outcome.status == "VALID"
    assert outcome.provider_called is True
    assert outcome.evidence_ready_at == READY_AT
    assert isinstance(outcome.context_event, ContextAIEvent)
    assert len(transport.calls) == 1
    assert outcome.classification_input_fingerprint is not None
    stored = archive.read_materialized_event(
        str(getattr(revision, "source_revision_id")),
        classification_input_fingerprint=outcome.classification_input_fingerprint,
    )
    assert stored is not None
    assert stored["context_event_id"] == outcome.context_event.context_event_id
    return (
        archive,
        revision,
        outcome.context_event,
        profile,
        coverage_source,
        transport,
    )


@pytest.mark.parametrize(
    ("source_case", "expected_source", "decision_ticker"),
    [
        ("veritawire", "veritawire_truth_social", "LMT"),
        ("lmt-rss", "lockheed_martin_rss", "LMT"),
        ("pltr-ir", "palantir_ir", "PLTR"),
        ("earnings", "company_earnings", "LMT"),
    ],
)
def test_external_source_fixture_reaches_ready_shadow_evidence_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_case: str,
    expected_source: str,
    decision_ticker: str,
) -> None:
    monkeypatch.setattr(requests.sessions.Session, "request", _forbid_live_request)
    authority_calls: list[str] = []

    def forbidden_authority(*_args: object, **_kwargs: object) -> object:
        authority_calls.append("unexpected")
        raise AssertionError("external research evidence invoked trading authority")

    monkeypatch.setattr(risk_filter_module, "evaluate_risk", forbidden_authority)
    monkeypatch.setattr(AlpacaPaperClient, "submit_order", forbidden_authority)

    archive, revision, event, profile, coverage_source, _transport = _classified_fixture(
        tmp_path / source_case,
        source_case=source_case,
    )
    assert event.source == expected_source
    coverage = _complete_coverage(archive, coverage_source=coverage_source)

    # Reopen from disk to prove the projection uses durable publication rather
    # than objects retained by the collector/classifier process.
    reopened = ExternalEventArchive(archive.root, now=lambda: READY_AT)
    index = hydrate_external_research_evidence(
        archive=reopened,
        run_definition=_run_definition(
            reopened,
            profile=profile,
            coverage=coverage,
            coverage_source=coverage_source,
        ),
    )
    assembler = DecisionContextAssembler(cache=ContextStateCache())
    before = OBSERVED_AT + timedelta(seconds=2)
    before_context = assembler.build_for_decision(
        decision_ticker,
        before,
        f"trace-{source_case}-before",
        None,
        ticker_sector="DEFENSE",
    )
    assert index.select(before_context).selected_evidence == ()

    ready_context = assembler.build_for_decision(
        decision_ticker,
        READY_AT,
        f"trace-{source_case}-ready",
        None,
        ticker_sector="DEFENSE",
    )
    ready_selection = index.select(ready_context)
    assert len(ready_selection.selected_evidence) == 1
    selected = ready_selection.selected_evidence[0]
    assert selected.source == expected_source
    assert selected.source_revision_id == getattr(revision, "source_revision_id")
    assert selected.available_at == READY_AT
    assert selected.source_available_at == getattr(revision, "source_available_at")
    assert selected.evidence_ready_at == READY_AT

    signal = ModelSignal(
        signal_time=READY_AT,
        ticker=decision_ticker,
        signal=SignalSide.BUY,
        confidence=0.7,
        raw_score=0.2,
        model_version="offline-model-v1",
        calibration_version="offline-calibration-v1",
        feature_version="offline-features-v1",
        feature_snapshot_id=f"snapshot-{source_case}",
        signal_id=f"signal-{source_case}",
        trace_id=f"trace-{source_case}-ready",
    )
    evaluation = evaluate_shadow_context(
        model_signal=signal,
        decision_context=ready_context,
        evidence_selection=ready_selection,
        risk_decision=None,
    )
    assert evaluation.hypothetical_action is ShadowContextAction.NO_CHANGE
    assert evaluation.matched_context_event_ids == []
    assert authority_calls == []


def test_external_event_emergency_ledger_excludes_raw_source_and_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests.sessions.Session, "request", _forbid_live_request)
    _archive, _revision, event, _profile, _coverage_source, _transport = (
        _classified_fixture(tmp_path / "fallback", source_case="veritawire")
    )
    exception_secret = "provider-exception-secret-must-not-enter-ledger"

    class FailingWriter:
        def write_context_ai_event(self, _record: object) -> object:
            raise RuntimeError(
                f"{exception_secret}: {API_KEY_SENTINEL}: {RAW_BODY_SENTINEL}"
            )

    class RecordingFallback:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def append_record(self, **kwargs: object) -> object:
            self.records.append(dict(kwargs))
            return {"persisted": True}

    fallback = RecordingFallback()
    result = source_checker._ContextAIEventWriter(
        writer=FailingWriter(),
        fallback=fallback,
    ).write(event)

    assert result == {"persisted": True}
    assert len(fallback.records) == 1
    serialized = json.dumps(fallback.records, default=str, sort_keys=True)
    for forbidden_value in (
        RAW_BODY_SENTINEL,
        API_KEY_SENTINEL,
        exception_secret,
        "Authorization: Bearer",
    ):
        assert forbidden_value not in serialized
    payload = fallback.records[0]["payload"]
    assert isinstance(payload, dict)
    assert {
        "raw_text",
        "raw_payload",
        "source_body",
        "input_text",
        "prompt",
        "provider_body",
        "api_key",
        "authorization",
    }.isdisjoint(payload)
