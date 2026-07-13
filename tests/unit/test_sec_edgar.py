"""Fixture-backed tests for the bounded, research-only SEC EDGAR collector."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

import market_relay_engine.context.sec_edgar as sec_edgar_module
from market_relay_engine.ai_context import ContextClassificationAttemptResult
from market_relay_engine.ai_context.classifier import GeminiContextClassifier
from market_relay_engine.ai_context.settings import AIContextFilterSettings
from market_relay_engine.common.config import ConfigValidationError, load_yaml_config
from market_relay_engine.context.sec_edgar import (
    EIGHT_K_EXTRACTION_VERSION,
    EIGHT_K_TRUNCATION_POLICY,
    SECEDGARCollector,
    SECEDGARConfigurationError,
    SECEDGARFairAccessError,
    SECEDGARHTTPClient,
    SECEDGARHTTPError,
    SECEDGARSettings,
    SECFiling,
    SECMappingDriftError,
    default_aggregate_form4_events,
    discover_filings,
    extract_relevant_8k_sections,
    filing_document_url,
    load_sec_issuers,
    normalize_accession_number,
    parse_form4,
    prepare_8k_section,
    resolve_form4_xml_document,
)
from market_relay_engine.context.sec_edgar_archive import SECArchiveError, SECEDGARArchive
from market_relay_engine.contracts.context import (
    ContextClassificationEventType,
    ContextClassificationResponse,
    ContextClassificationStatus,
    ContextRiskLevel,
    ContextUrgency,
    DeterministicContextEventType,
)
from market_relay_engine.questdb.writer import context_classification_attempt_to_row


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 13, 14, 30, tzinfo=UTC)
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "sec_edgar"
EIGHT_K = (FIXTURE_DIR / "eight_k.html").read_bytes()
FORM4 = (FIXTURE_DIR / "form4.xml").read_bytes()
SUBMISSIONS = json.loads(
    (FIXTURE_DIR / "submissions.json").read_text(encoding="utf-8")
)
FILING_INDEX = json.loads(
    (FIXTURE_DIR / "filing_index.json").read_text(encoding="utf-8")
)


class FakeSECClient:
    def __init__(
        self,
        *,
        submissions: dict[str, object] | None = None,
        eight_k: bytes = EIGHT_K,
        form4: bytes = FORM4,
    ) -> None:
        self.submissions = deepcopy(submissions or SUBMISSIONS)
        self.eight_k = eight_k
        self.form4 = form4
        self.json_calls: list[str] = []
        self.bytes_calls: list[str] = []

    def get_json(self, url: str) -> dict[str, object]:
        self.json_calls.append(url)
        if url.endswith("/index.json"):
            return deepcopy(FILING_INDEX)
        return deepcopy(self.submissions)

    def get_bytes(self, url: str) -> bytes:
        self.bytes_calls.append(url)
        if url.lower().endswith(".xml"):
            return self.form4
        return self.eight_k


class FakeClassifier:
    def __init__(self, statuses: list[ContextClassificationStatus]) -> None:
        self.statuses = list(statuses)
        self.requests: list[object] = []

    def classify(self, request: object) -> ContextClassificationAttemptResult:
        self.requests.append(request)
        status = self.statuses.pop(0)
        common = {
            "classification_request_id": request.classification_request_id,
            "classified_at": NOW,
            "provider": "fake",
            "model_version": "gemini-test",
            "prompt_version": request.prompt_version,
            "status": status,
            "provider_latency_ms": 1,
            "provider_request_count": 1,
            "retry_count": 0,
        }
        if status is ContextClassificationStatus.VALID:
            response = ContextClassificationResponse(
                **common,
                event_type=ContextClassificationEventType.SEC_8K_RESULTS,
                risk_level=ContextRiskLevel.LOW,
                urgency=ContextUrgency.LOW,
                confidence=0.8,
                summary="Results disclosure.",
            )
        elif status is ContextClassificationStatus.ABSTAINED:
            response = ContextClassificationResponse(
                **common,
                summary="No classifiable material event.",
            )
        else:
            response = ContextClassificationResponse(
                **common,
                safe_failure_category="TIMEOUT",
                safe_failure_summary="The Gemini request timed out.",
            )
        return ContextClassificationAttemptResult(response=response)


class FakeGeminiTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        payload = {
            "status": "VALID",
            "event_type": "SEC_8K_RESULTS",
            "risk_level": "LOW",
            "urgency": "LOW",
            "confidence": 0.8,
            "summary": "Results disclosure.",
        }
        return type(
            "Interaction",
            (),
            {
                "status": "completed",
                "model": "gemini-test",
                "output_text": json.dumps(payload),
                "steps": None,
            },
        )()


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.initial_calls: list[tuple[object, object, object]] = []
        self.row_calls: list[tuple[str, dict[str, object]]] = []

    def write_context_classification_attempt(
        self, request: object, response: object, validation_result: object
    ) -> None:
        self.initial_calls.append((request, response, validation_result))
        if self.fail:
            raise RuntimeError("simulated QuestDB outage")

    def write_row(self, table: str, row: dict[str, object]) -> None:
        self.row_calls.append((table, row))
        if self.fail:
            raise RuntimeError("simulated QuestDB outage")


class FakeFallback:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append_record(self, **kwargs: object) -> None:
        self.records.append(dict(kwargs))


class CrashBeforeManifestArchive(SECEDGARArchive):
    def save_manifest(self, manifest: object) -> None:
        del manifest
        raise RuntimeError("simulated crash before manifest save")


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content: bytes = b"ok",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls = 0

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.calls += 1
        return self.responses.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _settings(
    tmp_path: Path, *, request_rate_per_second: float | None = None
) -> SECEDGARSettings:
    config = deepcopy(load_yaml_config("context_sources", base_dir=REPO_ROOT))
    source = config["unstructured_sources"]["sec_edgar"]
    source["archive_path"] = str(tmp_path / "archive")
    if request_rate_per_second is not None:
        source["request_rate_per_second"] = request_rate_per_second
    return SECEDGARSettings.from_repository_config(config, base_dir=tmp_path)


def _ai_settings(
    *, enabled: bool = False, max_input_characters: int = 12_000
) -> AIContextFilterSettings:
    return AIContextFilterSettings(
        enabled=enabled,
        provider="gemini",
        model="gemini-test",
        api_key_env="GEMINI_API_KEY",
        prompt_version="context_filter_v1",
        response_schema_version="context_classification_response_v1",
        timeout_seconds=1,
        max_retries=0,
        retry_base_delay_seconds=0.1,
        retry_max_delay_seconds=0.1,
        max_input_characters=max_input_characters,
        max_prompt_characters=30_000,
        max_summary_characters=500,
        max_output_tokens=256,
        max_provider_calls_per_minute=20,
        max_provider_calls_per_run=20,
        dedup_cache_max_entries=20,
        temperature=0,
        direct_trade_authority=False,
    )


def _http_client(
    session: FakeSession, clock: FakeClock, *, rate: float = 2, max_retries: int = 2
) -> SECEDGARHTTPClient:
    return SECEDGARHTTPClient(
        user_agent="Example Research sec@example.test",
        timeout_seconds=1,
        max_retries=max_retries,
        request_rate_per_second=rate,
        retry_base_delay_seconds=0.5,
        retry_max_delay_seconds=4,
        session=session,
        monotonic_clock=clock,
        sleeper=clock.sleep,
    )


def _issuer():
    return load_sec_issuers(base_dir=REPO_ROOT)[0]


def _archive_first_eight_k(tmp_path: Path):
    settings = _settings(tmp_path)
    archive = SECEDGARArchive(settings.archive_path)
    client = FakeSECClient()
    filing = discover_filings(
        client, _issuer(), forms=("8-K",), collected_at=NOW
    )[0]
    result = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=client,
        archive=archive,
    )._load_or_archive(filing, archive.load_manifest())
    return archive, filing, client, result


def _crash_after_filing_metadata(tmp_path: Path):
    settings = _settings(tmp_path)
    crash_archive = CrashBeforeManifestArchive(settings.archive_path)
    client = FakeSECClient()
    filing = discover_filings(
        client, _issuer(), forms=("8-K",), collected_at=NOW
    )[0]
    with pytest.raises(RuntimeError, match="simulated crash before manifest save"):
        SECEDGARCollector(
            settings=settings,
            issuers=(_issuer(),),
            client=client,
            archive=crash_archive,
        )._load_or_archive(filing, crash_archive.load_manifest())
    archive = SECEDGARArchive(settings.archive_path)
    assert not archive.manifest_path.exists()
    assert archive.read_filing_metadata(filing.accession_number) is not None
    return archive, filing


def test_sec_configuration_user_agent_mapping_and_rate_limit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.request_rate_per_second == 2
    assert (
        settings.user_agent(
            {
                "SEC_ORGANIZATION": "Example Research",
                "SEC_CONTACT_EMAIL": "sec@example.test",
            }
        )
        == "Example Research sec@example.test"
    )
    with pytest.raises(SECEDGARConfigurationError):
        settings.user_agent({})
    with pytest.raises(ConfigValidationError, match="must not exceed 8"):
        _settings(tmp_path, request_rate_per_second=8.01)
    assert [(value.ticker, value.cik) for value in load_sec_issuers(base_dir=REPO_ROOT)] == [
        ("PLTR", "0001321655"),
        ("LMT", "0000936468"),
        ("RTX", "0000101829"),
        ("GD", "0000040533"),
        ("AVAV", "0001368622"),
        ("XOM", "0000034088"),
        ("OXY", "0000797468"),
        ("SLB", "0000087347"),
        ("COP", "0001163165"),
        ("VLO", "0001035002"),
    ]


def test_sec_env_example_uses_separate_blank_contact_fields() -> None:
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    }
    assert "SEC_CONTACT_EMAIL=" in lines
    assert "SEC_ORGANIZATION=" in lines
    assert not any(line.startswith("SEC_USER_AGENT=") for line in lines)
    assert not any(line.startswith("SEC_API_KEY=") for line in lines)


def test_sec_http_client_uses_monotonic_sequential_pacing() -> None:
    clock = FakeClock()
    session = FakeSession([FakeResponse(200), FakeResponse(200)])
    client = _http_client(session, clock)
    client.get_bytes("https://www.sec.gov/one")
    client.get_bytes("https://www.sec.gov/two")
    assert clock.sleeps == [0.5]
    assert session.calls == 2


def test_sec_http_client_honors_retry_after() -> None:
    clock = FakeClock()
    session = FakeSession(
        [FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(200)]
    )
    assert _http_client(session, clock, rate=8).get_bytes("https://www.sec.gov/x") == b"ok"
    assert clock.sleeps == [2.0]


def test_sec_http_client_bounds_5xx_retries() -> None:
    clock = FakeClock()
    session = FakeSession(
        [FakeResponse(503), FakeResponse(502), FakeResponse(200)]
    )
    assert _http_client(session, clock, rate=8).get_bytes("https://www.sec.gov/x") == b"ok"
    assert clock.sleeps == [0.5, 1.0]
    exhausted = FakeSession([FakeResponse(503), FakeResponse(503)])
    with pytest.raises(SECEDGARHTTPError):
        _http_client(exhausted, FakeClock(), rate=8, max_retries=1).get_bytes(
            "https://www.sec.gov/x"
        )
    assert exhausted.calls == 2


def test_sec_http_client_stops_immediately_on_fair_access_403() -> None:
    clock = FakeClock()
    session = FakeSession([FakeResponse(403), FakeResponse(200)])
    with pytest.raises(SECEDGARFairAccessError):
        _http_client(session, clock).get_bytes("https://www.sec.gov/x")
    assert session.calls == 1
    assert clock.sleeps == []


def test_discovery_validates_mapping_and_parses_accessions() -> None:
    client = FakeSECClient()
    filings = discover_filings(
        client, _issuer(), forms=("8-K", "4", "4/A"), collected_at=NOW
    )
    assert [filing.form_type for filing in filings] == ["8-K", "4", "4/A"]
    assert filings[0].acceptance_at == datetime(2026, 7, 12, 10, 15, 30, tzinfo=UTC)
    assert filings[2].amendment_of is None
    assert normalize_accession_number("000132165526000123") == "0001321655-26-000123"
    assert filing_document_url(
        "0001321655", "0001321655-26-000123", "form8k.htm"
    ).endswith("/1321655/000132165526000123/form8k.htm")


@pytest.mark.parametrize(
    ("field", "value"),
    [("cik", "0000000001"), ("tickers", ["WRONG"])],
)
def test_discovery_detects_static_mapping_drift(field: str, value: object) -> None:
    submissions = deepcopy(SUBMISSIONS)
    submissions[field] = value
    with pytest.raises(SECMappingDriftError, match="manual review|required"):
        discover_filings(FakeSECClient(submissions=submissions), _issuer())


def test_extract_relevant_8k_sections_uses_item_901_as_boundary() -> None:
    sections = extract_relevant_8k_sections(EIGHT_K)

    assert [section.item_number for section in sections] == ["2.02", "8.01"]
    assert sections[1].text == "Item 8.01 Other Events Other material event."
    assert "Item 9.01" not in sections[1].text
    assert "Exhibit 99.1" not in sections[1].text


def test_complete_section_and_deterministic_excerpt_metadata() -> None:
    long_document = (
        "<html><body><h2>Item 2.02</h2><p>" + ("material results " * 100) + "</p></body></html>"
    ).encode()
    section = extract_relevant_8k_sections(long_document)[0]
    prepared = prepare_8k_section(section, max_input_characters=120)
    assert len(section.text) > 120
    assert prepared.full_character_count == len(section.text)
    assert prepared.excerpt_character_count == 120
    assert prepared.input_truncated is True
    assert prepared.full_section_hash != prepared.excerpt_hash
    assert prepared.truncation_policy == EIGHT_K_TRUNCATION_POLICY == "HEAD_V1"
    assert prepared.extraction_version == EIGHT_K_EXTRACTION_VERSION


def test_sec_raw_input_id_is_deterministic_and_filing_item_scoped() -> None:
    filing = discover_filings(
        FakeSECClient(), _issuer(), forms=("8-K",), collected_at=NOW
    )[0]
    sections = tuple(
        prepare_8k_section(section, max_input_characters=12_000)
        for section in extract_relevant_8k_sections(EIGHT_K)
    )
    document_hash = sha256(EIGHT_K).hexdigest()
    first = sec_edgar_module._classification_request(
        filing, document_hash, sections[0], "context_filter_v1"
    )
    rebuilt = sec_edgar_module._classification_request(
        filing, document_hash, sections[0], "context_filter_v1"
    )
    other_accession = "0001321655-26-999998"
    other_filing = replace(
        filing,
        accession_number=other_accession,
        filing_url=filing_document_url(
            filing.issuer_cik, other_accession, filing.primary_document
        ),
    )
    other_filing_request = sec_edgar_module._classification_request(
        other_filing, document_hash, sections[0], "context_filter_v1"
    )
    other_item_request = sec_edgar_module._classification_request(
        filing, document_hash, sections[1], "context_filter_v1"
    )

    assert first.raw_input_hash == other_filing_request.raw_input_hash
    assert first.raw_input_id != other_filing_request.raw_input_id
    assert first.raw_input_id != other_item_request.raw_input_id
    assert first.raw_input_id == rebuilt.raw_input_id
    assert first.source_document_id == rebuilt.source_document_id


def test_sec_raw_input_id_is_shared_by_document_request_and_questdb_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_documents = []
    source_document_type = sec_edgar_module.ContextSourceDocument

    def capture_source_document(**kwargs: object):
        document = source_document_type(**kwargs)
        captured_documents.append(document)
        return document

    monkeypatch.setattr(
        sec_edgar_module, "ContextSourceDocument", capture_source_document
    )
    filing = discover_filings(
        FakeSECClient(), _issuer(), forms=("8-K",), collected_at=NOW
    )[0]
    section = prepare_8k_section(
        extract_relevant_8k_sections(EIGHT_K)[0], max_input_characters=12_000
    )
    request = sec_edgar_module._classification_request(
        filing, sha256(EIGHT_K).hexdigest(), section, "context_filter_v1"
    )
    response = FakeClassifier([ContextClassificationStatus.VALID]).classify(
        request
    ).response
    row = context_classification_attempt_to_row(request, response)

    assert len(captured_documents) == 1
    assert captured_documents[0].raw_input_id == request.raw_input_id
    assert row["raw_input_id"] == request.raw_input_id
    assert captured_documents[0].source_document_id == request.source_document_id
    assert row["source_document_id"] == request.source_document_id
    assert row["raw_input_hash"] == request.raw_input_hash == section.excerpt_hash


def test_archive_immutable_objects_sections_and_atomic_manifest(tmp_path: Path) -> None:
    archive = SECEDGARArchive(tmp_path)
    digest = archive.archive_document(b"original", extension="html")
    assert archive.archive_document(b"original", extension="html") == digest
    archive.archive_normalized_text(digest, "normalized")
    section = archive.archive_normalized_section(
        digest,
        item_number="2.02",
        section_hash=sha256(b"complete section").hexdigest(),
        text="complete section",
    )
    assert section.read_text(encoding="utf-8") == "complete section"
    manifest = archive.load_manifest()
    manifest["filings"]["0001321655-26-000123"] = {"document_hash": digest}
    archive.save_manifest(manifest)
    assert archive.load_manifest()["filings"]["0001321655-26-000123"]["document_hash"] == digest
    assert not list(tmp_path.rglob("*.tmp"))


def test_first_time_filing_archival_still_writes_durable_state(tmp_path: Path) -> None:
    archive, filing, client, result = _archive_first_eight_k(tmp_path)
    content, document_hash, state, was_archived = result

    assert was_archived is True
    assert content == EIGHT_K
    assert client.bytes_calls == [filing.filing_url]
    assert archive.read_filing_metadata(filing.accession_number) is not None
    assert archive.load_manifest()["filings"][filing.accession_number] == state
    assert state["document_hash"] == document_hash
    assert state["collected_at"] == NOW.isoformat()


def test_filing_archive_recovers_missing_manifest_without_redownload(
    tmp_path: Path,
) -> None:
    archive, filing = _crash_after_filing_metadata(tmp_path)
    metadata_path = archive.filings / f"{filing.accession_number}.json"
    original_metadata_bytes = metadata_path.read_bytes()
    original_metadata = archive.read_filing_metadata(filing.accession_number)
    assert original_metadata is not None
    original_hash = original_metadata["document_hash"]

    recovery_client = FakeSECClient()
    rediscovered = discover_filings(
        recovery_client,
        _issuer(),
        forms=("8-K",),
        collected_at=NOW + timedelta(minutes=5),
    )[0]
    settings = _settings(tmp_path)
    recovered = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=recovery_client,
        archive=archive,
    )._load_or_archive(rediscovered, archive.load_manifest())
    content, document_hash, state, was_archived = recovered

    assert was_archived is False
    assert content == EIGHT_K
    assert document_hash == original_hash
    assert recovery_client.bytes_calls == []
    assert metadata_path.read_bytes() == original_metadata_bytes
    assert state["document_hash"] == original_metadata["document_hash"]
    assert state["official_document_identity"] == original_metadata[
        "official_document_identity"
    ]
    assert state["official_document_url"] == original_metadata[
        "official_document_url"
    ]
    assert state["collected_at"] == original_metadata["collected_at"]
    assert state["collected_at"] == NOW.isoformat()
    assert archive.load_manifest()["filings"][filing.accession_number] == state


@pytest.mark.parametrize(
    ("field", "conflicting_value"),
    [
        ("accession_number", "0001321655-26-999999"),
        ("ticker", "WRONG"),
        ("issuer_cik", "0000000001"),
        ("form_type", "8-K/A"),
        ("filing_date", "2026-01-01"),
        ("primary_document", "other.htm"),
        ("filing_url", "https://www.sec.gov/other"),
    ],
)
def test_filing_archive_recovery_rejects_conflicting_source_identity(
    tmp_path: Path, field: str, conflicting_value: str
) -> None:
    archive, filing = _crash_after_filing_metadata(tmp_path)
    metadata = archive.read_filing_metadata(filing.accession_number)
    assert metadata is not None
    metadata[field] = conflicting_value
    metadata_path = archive.filings / f"{filing.accession_number}.json"
    metadata_path.write_text(
        json.dumps(metadata, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    recovery_client = FakeSECClient()
    rediscovered = discover_filings(
        recovery_client,
        _issuer(),
        forms=("8-K",),
        collected_at=NOW + timedelta(minutes=5),
    )[0]

    with pytest.raises(SECArchiveError, match="identity conflicts"):
        SECEDGARCollector(
            settings=_settings(tmp_path),
            issuers=(_issuer(),),
            client=recovery_client,
            archive=archive,
        )._load_or_archive(rediscovered, archive.load_manifest())
    assert recovery_client.bytes_calls == []


@pytest.mark.parametrize("damage", ["missing", "corrupted"])
def test_filing_archive_recovery_rejects_missing_or_corrupted_document(
    tmp_path: Path, damage: str
) -> None:
    archive, filing = _crash_after_filing_metadata(tmp_path)
    metadata = archive.read_filing_metadata(filing.accession_number)
    assert metadata is not None
    document_path = next(
        (archive.objects / metadata["document_hash"]).glob("original.*")
    )
    if damage == "missing":
        document_path.unlink()
    else:
        document_path.write_bytes(b"corrupted")
    recovery_client = FakeSECClient()
    rediscovered = discover_filings(
        recovery_client,
        _issuer(),
        forms=("8-K",),
        collected_at=NOW + timedelta(minutes=5),
    )[0]

    with pytest.raises(SECArchiveError, match="missing|hash does not match"):
        SECEDGARCollector(
            settings=_settings(tmp_path),
            issuers=(_issuer(),),
            client=recovery_client,
            archive=archive,
        )._load_or_archive(rediscovered, archive.load_manifest())
    assert recovery_client.bytes_calls == []


def test_successful_classification_is_durable_across_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeSECClient()
    classifier = FakeClassifier(
        [ContextClassificationStatus.VALID, ContextClassificationStatus.VALID]
    )
    first = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=client,
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=_ai_settings(),
    ).collect(forms=("8-K",), max_filings=1)
    second = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=client,
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=_ai_settings(),
    ).collect(forms=("8-K",), max_filings=1)
    assert first["classifications"] == 2
    assert second["classifications"] == 0
    assert second["persistent_suppressions"] == 2
    assert len(client.bytes_calls) == 1
    assert len(classifier.requests) == 2
    manifest = SECEDGARArchive(settings.archive_path).load_manifest()
    records = next(iter(manifest["filings"].values()))["classifications"]
    record = next(iter(records.values()))
    assert record["classification_complete"] is True
    assert record["status"] == "VALID"
    assert record["full_section_hash"]
    assert record["excerpt_hash"]
    assert record["classification_config_hash"]
    assert "input_text" not in json.dumps(record)


def test_abstained_classification_is_durably_reusable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    classifier = FakeClassifier(
        [ContextClassificationStatus.ABSTAINED, ContextClassificationStatus.ABSTAINED]
    )
    collector = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=FakeSECClient(),
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=_ai_settings(),
    )
    collector.collect(forms=("8-K",), max_filings=1)
    result = collector.collect(forms=("8-K",), max_filings=1)
    assert result["persistent_suppressions"] == 2
    assert len(classifier.requests) == 2


def test_provider_failure_remains_retryable_on_later_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    classifier = FakeClassifier(
        [
            ContextClassificationStatus.PROVIDER_FAILED,
            ContextClassificationStatus.VALID,
            ContextClassificationStatus.VALID,
        ]
    )
    collector = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=FakeSECClient(),
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=_ai_settings(),
    )
    collector.collect(forms=("8-K",), max_filings=1)
    collector.collect(forms=("8-K",), max_filings=1)
    assert len(classifier.requests) == 3


def test_collector_reuses_pr35_classifier_and_never_sends_oversized_input(
    tmp_path: Path,
) -> None:
    long_document = (
        "<html><body><h2>Item 2.02</h2><p>" + ("results " * 500) + "</p></body></html>"
    ).encode()
    settings = _settings(tmp_path)
    transport = FakeGeminiTransport()
    ai_settings = _ai_settings(enabled=True, max_input_characters=200)
    classifier = GeminiContextClassifier(ai_settings, transport=transport)
    collector = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=FakeSECClient(eight_k=long_document),
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=ai_settings,
    )
    first = collector.collect(forms=("8-K",), max_filings=1)
    second = collector.collect(forms=("8-K",), max_filings=1)
    assert first["classifications"] == 1
    assert second["persistent_suppressions"] == 1
    assert len(transport.calls) == 1
    assert len(transport.calls[0]["prompt"]) <= ai_settings.max_prompt_characters
    manifest = SECEDGARArchive(settings.archive_path).load_manifest()
    saved = next(iter(next(iter(manifest["filings"].values()))["classifications"].values()))
    assert saved["excerpt_character_count"] == 200
    assert saved["full_section_character_count"] > 200


def test_questdb_failure_preserves_paid_result_and_retries_only_ledger(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    classifier = FakeClassifier(
        [ContextClassificationStatus.VALID, ContextClassificationStatus.VALID]
    )
    fallback = FakeFallback()
    failed_writer = FakeWriter(fail=True)
    collector = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=FakeSECClient(),
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=_ai_settings(),
        ledger_writer=failed_writer,
        fallback=fallback,
    )
    collector.collect(forms=("8-K",), max_filings=1, write_questdb=True)
    manifest = SECEDGARArchive(settings.archive_path).load_manifest()
    saved_records = next(iter(manifest["filings"].values()))["classifications"].values()
    assert {value["ledger_write_status"] for value in saved_records} == {
        "FALLBACK_WRITTEN_QUESTDB_PENDING"
    }
    assert len(fallback.records) == 2

    successful_writer = FakeWriter()
    retry = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=FakeSECClient(),
        archive=SECEDGARArchive(settings.archive_path),
        ledger_writer=successful_writer,
        fallback=fallback,
    ).collect(forms=("8-K",), max_filings=1, write_questdb=True)
    assert retry["ledger_retries"] == 2
    assert len(successful_writer.row_calls) == 2
    assert all(call[0] == "context_classification_attempts" for call in successful_writer.row_calls)
    assert all("input_text" not in str(call[1]) for call in successful_writer.row_calls)


def test_official_form4_xml_selection_avoids_renderer_document() -> None:
    client = FakeSECClient()
    filing = discover_filings(client, _issuer(), forms=("4",), collected_at=NOW)[0]
    identity, url, content = resolve_form4_xml_document(client, filing)
    assert identity == "ownership.xml"
    assert url.endswith("/ownership.xml")
    assert content == FORM4
    assert any(value.endswith("/index.json") for value in client.json_calls)
    assert not any("xslF345X02" in value for value in client.bytes_calls)


def test_form4_preserves_derivatives_and_promotes_only_nonderivative_p_s() -> None:
    filing = SECFiling(
        ticker="PLTR",
        issuer_cik="0001321655",
        accession_number="0001321655-26-000124",
        form_type="4",
        filing_date=date(2026, 7, 11),
        primary_document="ownership.xml",
        filing_url="https://www.sec.gov/example",
        collected_at=NOW,
        acceptance_at=NOW,
    )
    parsed = parse_form4(FORM4, filing)
    assert [(value.security_kind, value.transaction_code) for value in parsed.transactions] == [
        ("NON_DERIVATIVE", "P"),
        ("NON_DERIVATIVE", "S"),
        ("NON_DERIVATIVE", "M"),
        ("DERIVATIVE", "P"),
        ("DERIVATIVE", "M"),
    ]
    assert [value.event_type for value in parsed.promoted_events] == [
        DeterministicContextEventType.SEC_FORM4_OPEN_MARKET_PURCHASE,
        DeterministicContextEventType.SEC_FORM4_OPEN_MARKET_SALE,
    ]
    assert parsed.transactions[3].underlying_shares == 10
    assert parsed.transactions[3].promoted_event_type is None
    assert parsed.promoted_events[0].approximate_value == 2050
    assert parsed.promoted_events[0].aggregate_eligibility == "ELIGIBLE"


def test_form4_amendment_is_preserved_but_excluded_from_default_aggregates() -> None:
    filing = SECFiling(
        ticker="PLTR",
        issuer_cik="0001321655",
        accession_number="0001321655-26-000125",
        form_type="4/A",
        filing_date=date(2026, 7, 10),
        primary_document="ownershipa.xml",
        filing_url="https://www.sec.gov/example",
        collected_at=NOW,
        acceptance_at=NOW,
        amendment_of=None,
    )
    parsed = parse_form4(FORM4, filing)
    assert parsed.is_amendment is True
    assert parsed.amends_accession is None
    assert {value.aggregate_eligibility for value in parsed.promoted_events} == {
        "AMENDMENT_UNRESOLVED"
    }
    assert default_aggregate_form4_events(parsed.promoted_events) == ()


def test_collector_archives_all_form4_transactions_without_gemini(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = FakeSECClient()
    result = SECEDGARCollector(
        settings=settings,
        issuers=(_issuer(),),
        client=client,
        archive=SECEDGARArchive(settings.archive_path),
    ).collect(forms=("4",), max_filings=1)
    assert result["form4_events"] == 2
    payload = json.loads(
        next((settings.archive_path / "form4").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert len(payload["normalized_transactions"]) == 5
    assert len(payload["research_events"]) == 2
    assert any(
        value["security_kind"] == "DERIVATIVE"
        for value in payload["normalized_transactions"]
    )


def test_no_risk_model_execution_or_alpaca_imports() -> None:
    text = (
        REPO_ROOT / "src" / "market_relay_engine" / "context" / "sec_edgar.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "market_relay_engine.risk",
        "market_relay_engine.execution",
        "market_relay_engine.model",
        "alpaca",
        "approved_risk_context",
    )
    assert not any(value in text for value in forbidden)
