from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json

import pytest
import yaml

from market_relay_engine.context.state_cache import ContextStateCache
from market_relay_engine.context.usaspending_collector import (
    INDICATOR_NAME,
    SOURCE_NAME,
    USAspendingCollectionStatus,
    USAspendingCollector,
    USAspendingCollectorBusyError,
    USAspendingCollectorError,
    USAspendingConfig,
    USAspendingHTTPClient,
    USAspendingRecipientMapping,
    JSONUSAspendingCheckpointStore,
    cache_entry_name,
    discovery_window,
    load_recipient_mappings,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED_AT = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
UEI = "EXACTUEI123"


def _mapping(ticker: str = "TST", uei: str = UEI) -> USAspendingRecipientMapping:
    return USAspendingRecipientMapping(
        recipient_uei=uei,
        recipient_name="EXACT LEGAL NAME",
        ticker=ticker,
        issuer_name="Test Issuer Inc.",
        mapping_confidence="confirmed",
        economic_beneficiary="prime_recipient",
        active=True,
        mapping_version="usaspending_recipient_map_v1",
    )


def _search_row(
    lookup: str,
    *,
    award_id: str | None = None,
    uei: str = UEI,
    last_modified: str = "2026-06-20",
    amount: float = 100.0,
) -> dict[str, object]:
    return {
        "generated_internal_id": lookup,
        "Award ID": award_id or f"AWARD-{lookup}",
        "Recipient UEI": uei,
        "Recipient Name": "EXACT LEGAL NAME",
        "Last Modified Date": last_modified,
        "Base Obligation Date": "2026-06-20",
        "Award Amount": amount,
        "Contract Award Type": "A",
        "NAICS": "541330",
        "PSC": "R425",
    }


def _detail(
    canonical_id: str,
    *,
    uei: str = UEI,
    amount: float = 100.0,
    last_modified: str = "2026-06-20",
    action_date: str = "2026-06-20",
    award_type: str = "A",
) -> dict[str, object]:
    return {
        "generated_unique_award_id": canonical_id,
        "recipient": {"recipient_uei": uei, "recipient_name": "EXACT LEGAL NAME"},
        "type": award_type,
        "type_description": "Definitive Contract",
        "category": "contract",
        "description": "Official factual award description.",
        "date_signed": action_date,
        "action_date": action_date,
        "last_modified_date": last_modified,
        "period_of_performance_start": action_date,
        "period_of_performance_current_end_date": "2026-12-31",
        "total_obligation": amount,
        "base_exercised_options": amount,
        "base_and_all_options": amount + 50.0,
        "awarding_agency": {"name": "Agency A", "code": "AA"},
        "funding_agency": {"name": "Agency F", "code": "FF"},
        "latest_transaction_contract_data": {
            "naics": "541330",
            "naics_description": "Engineering Services",
            "product_or_service_code": "R425",
            "product_or_service_description": "Engineering services",
        },
    }


def _funding(
    *,
    has_next: bool = False,
    amount: float = 100.0,
    object_class: str = "25.2",
) -> dict[str, object]:
    return {
        "results": [
            {
                "transaction_obligated_amount": amount,
                "reporting_fiscal_year": 2026,
                "reporting_fiscal_quarter": 3,
                "reporting_fiscal_month": 6,
                "awarding_agency_name": "Agency A",
                "funding_agency_name": "Agency F",
                "federal_account": "000-0000",
                "account_title": "Operations",
                "program_activity_code": "0001",
                "program_activity_name": "Program",
                "object_class": object_class,
                "object_class_name": "Services",
            }
        ],
        "page_metadata": {"hasNext": has_next},
    }


class FakeClient:
    def __init__(
        self,
        *,
        last_updated: object = "2026-06-20",
        searches: dict[str, dict[str, object]] | None = None,
        details: dict[str, dict[str, object]] | None = None,
        funding: dict[str, dict[str, object]] | None = None,
        fail_last_updated: bool = False,
        fail_search: bool = False,
        fail_funding: set[str] | None = None,
    ) -> None:
        self.last_updated = last_updated
        self.searches = searches or {UEI: {"results": [], "page_metadata": {"hasNext": False}}}
        self.details = details or {}
        self.funding = funding or {}
        self.fail_last_updated = fail_last_updated
        self.fail_search = fail_search
        self.fail_funding = set() if fail_funding is None else set(fail_funding)
        self.calls: list[tuple[str, object]] = []

    def fetch_last_updated(self) -> dict[str, object]:
        self.calls.append(("last_updated", None))
        if self.fail_last_updated:
            raise RuntimeError("last updated unavailable")
        return {"last_updated": self.last_updated}

    def search_spending_by_award(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("search", dict(kwargs)))
        if self.fail_search:
            raise RuntimeError("search unavailable")
        return deepcopy(self.searches[str(kwargs["recipient_uei"])])

    def fetch_award_detail(self, award_id: str) -> dict[str, object]:
        self.calls.append(("detail", award_id))
        return deepcopy(self.details[award_id])

    def fetch_award_funding(self, award_id: str, *, limit: int) -> dict[str, object]:
        self.calls.append(("funding", {"award_id": award_id, "limit": limit}))
        if award_id in self.fail_funding:
            raise RuntimeError("funding unavailable")
        return deepcopy(self.funding[award_id])


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.snapshots: list[object] = []

    def write_context_indicator_snapshot(self, snapshot: object, **kwargs: object) -> str:
        if self.fail:
            raise RuntimeError("writer failed")
        self.snapshots.append(snapshot)
        return "written"


def _collector(
    tmp_path: Path,
    client: FakeClient,
    *,
    cache: ContextStateCache | None = None,
    writer: FakeWriter | None = None,
    mappings: tuple[USAspendingRecipientMapping, ...] | None = None,
    config: USAspendingConfig | None = None,
) -> USAspendingCollector:
    return USAspendingCollector(
        cache=cache or ContextStateCache(),
        config=config or USAspendingConfig(enabled=True),
        client=client,
        ledger_writer=writer,
        checkpoint_store=JSONUSAspendingCheckpointStore(tmp_path / "award_checkpoint.json"),
        recipient_mappings=(_mapping(),) if mappings is None else mappings,
    )


def _single_award_client(
    *,
    lookup: str = "lookup-1",
    canonical_id: str = "CONT_AWD_1",
    detail: dict[str, object] | None = None,
    funding: dict[str, object] | None = None,
) -> FakeClient:
    detail_payload = _detail(canonical_id) if detail is None else detail
    return FakeClient(
        searches={UEI: {"results": [_search_row(lookup)], "page_metadata": {"hasNext": False}}},
        details={lookup: detail_payload, canonical_id: detail_payload},
        funding={canonical_id: _funding() if funding is None else funding},
    )


def test_repository_configuration_is_exact_and_disabled_by_default() -> None:
    loaded = yaml.safe_load((REPO_ROOT / "config" / "context_sources.yaml").read_text(encoding="utf-8"))
    config = USAspendingConfig.from_repository_config(loaded)

    assert config.enabled is False
    assert config.api_key_required is False
    assert config.discovery_last_modified_lookback_calendar_days == 14
    assert config.funding_limit_per_award == 100
    assert config.award_registry_retention_calendar_days == 180
    assert not hasattr(config, "event_cache_ttl_seconds")


def test_mapping_file_starts_empty() -> None:
    loaded = yaml.safe_load(
        (REPO_ROOT / "config" / "usaspending_recipient_ticker_map.yaml").read_text(encoding="utf-8")
    )

    assert loaded == {"mapping_version": "usaspending_recipient_map_v1", "recipients": []}


def test_disabled_collector_does_nothing(tmp_path: Path) -> None:
    client = _single_award_client()
    store = JSONUSAspendingCheckpointStore(tmp_path / "award_checkpoint.json")
    result = USAspendingCollector(
        cache=ContextStateCache(),
        config=USAspendingConfig(),
        client=client,
        checkpoint_store=store,
        recipient_mappings=(_mapping(),),
    ).collect(evaluation_time=CHECKED_AT, write_questdb=True)

    assert result.status is USAspendingCollectionStatus.DISABLED
    assert client.calls == []
    assert not store.path.exists()
    assert not store.lock_path.exists()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"enabled": "false"}, "enabled"),
        ({"api_key_required": True}, "api_key_required"),
        ({"feeds_memory_cache": False}, "feeds_memory_cache"),
        ({"used_in_per_tick_loop": True}, "used_in_per_tick_loop"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"intended_poll_interval_seconds": 59}, "intended_poll_interval_seconds"),
        ({"discovery_last_modified_lookback_calendar_days": 0}, "discovery"),
        ({"source_last_updated_max_age_calendar_days": 15}, "source_last"),
        ({"search_limit_per_recipient": 0}, "search_limit"),
        ({"max_award_details_per_recipient_per_run": 101}, "max_award"),
        ({"funding_limit_per_award": 0}, "funding_limit"),
        ({"revision_recheck_calendar_days": 91}, "revision"),
        ({"max_revision_rechecks_per_run": 0}, "max_revision"),
        ({"late_discovery_calendar_days": 15}, "late_discovery"),
        ({"award_registry_retention_calendar_days": 44}, "award_registry"),
        ({"checkpoint_path": "../bad.json"}, "checkpoint_path"),
        ({"contract_awards_only": False}, "contract_awards_only"),
    ],
)
def test_strict_config_validation(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(USAspendingCollectorError, match=match):
        USAspendingConfig(**kwargs)


def test_enabled_requires_active_mapping(tmp_path: Path) -> None:
    with pytest.raises(USAspendingCollectorError, match="at least one active"):
        _collector(tmp_path, _single_award_client(), mappings=()).collect(
            evaluation_time=CHECKED_AT
        )


def test_duplicate_mapping_uei_rejected(tmp_path: Path) -> None:
    path = tmp_path / "map.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mapping_version": "usaspending_recipient_map_v1",
                "recipients": [
                    _mapping().__dict__,
                    _mapping(ticker="ABC").__dict__,
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(USAspendingCollectorError, match="duplicate recipient_uei"):
        load_recipient_mappings(path)


def test_discovery_window_uses_exact_new_york_inclusive_dates() -> None:
    assert discovery_window(datetime(2026, 6, 20, 1, 30, tzinfo=UTC), 14) == (
        "2026-06-06",
        "2026-06-19",
    )
    assert discovery_window(datetime(2026, 6, 20, 16, 0, tzinfo=UTC), 14) == (
        "2026-06-07",
        "2026-06-20",
    )


def test_http_client_constructs_official_bounded_requests() -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"results": [], "page_metadata": {"hasNext": False}}

    def post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    client = USAspendingHTTPClient(request_post=post, request_get=lambda *args, **kwargs: Response())
    client.search_spending_by_award(
        recipient_uei=UEI,
        start_date="2026-06-07",
        end_date="2026-06-20",
        limit=100,
    )
    body = captured["json"]

    assert captured["url"].endswith("/api/v2/search/spending_by_award/")
    assert body["limit"] == 100
    assert body["page"] == 1
    assert body["filters"]["award_type_codes"] == ["A", "B", "C", "D"]
    assert body["filters"]["recipient_search_text"] == [UEI]
    assert body["filters"]["time_period"] == [
        {
            "start_date": "2026-06-07",
            "end_date": "2026-06-20",
            "date_type": "last_modified_date",
        }
    ]


def test_funding_client_constructs_bounded_request() -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"results": [], "page_metadata": {"hasNext": False}}

    def post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    USAspendingHTTPClient(
        request_post=post,
        request_get=lambda *args, **kwargs: Response(),
    ).fetch_award_funding("CONT_AWD_1", limit=50)
    body = captured["json"]

    assert captured["url"].endswith("/api/v2/awards/funding/")
    assert body == {
        "award_id": "CONT_AWD_1",
        "page": 1,
        "limit": 50,
        "sort": "reporting_fiscal_date",
        "order": "desc",
    }


def test_new_award_emits_snapshot_cache_and_ledger(tmp_path: Path) -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    result = _collector(tmp_path, _single_award_client(), cache=cache, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )

    assert result.status is USAspendingCollectionStatus.SUCCESS
    assert len(result.indicator_snapshots) == len(writer.snapshots) == 1
    snapshot = result.indicator_snapshots[0]
    assert snapshot.source == SOURCE_NAME
    assert snapshot.indicator_name == INDICATOR_NAME
    assert snapshot.ticker_or_sector == "TST"
    assert snapshot.details["canonical_award_id"] == "CONT_AWD_1"
    assert snapshot.details["cache_entry_name"] == cache_entry_name("TST", "CONT_AWD_1")
    entry = cache.get_ticker("TST", cache_entry_name("TST", "CONT_AWD_1"), now=CHECKED_AT)
    assert entry is not None
    assert entry.valid_until is None
    assert entry.severity == "INFO"


def test_multiple_same_ticker_awards_coexist_and_do_not_overwrite(tmp_path: Path) -> None:
    details = {
        "lookup-1": _detail("CONT_AWD_MAJOR", amount=1_000_000.0),
        "lookup-2": _detail("CONT_AWD_MINOR", amount=1.0),
    }
    details["CONT_AWD_MAJOR"] = details["lookup-1"]
    details["CONT_AWD_MINOR"] = details["lookup-2"]
    client = FakeClient(
        searches={
            UEI: {
                "results": [
                    _search_row("lookup-1", award_id="MAJOR", amount=1_000_000.0),
                    _search_row("lookup-2", award_id="MINOR", amount=1.0),
                ],
                "page_metadata": {"hasNext": False},
            }
        },
        details=details,
        funding={"CONT_AWD_MAJOR": _funding(), "CONT_AWD_MINOR": _funding(amount=1.0)},
    )
    cache = ContextStateCache()
    result = _collector(tmp_path, client, cache=cache).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.SUCCESS
    names = {entry.key.name for entry in cache.latest_for_ticker("TST", now=CHECKED_AT)}
    assert names == {
        cache_entry_name("TST", "CONT_AWD_MAJOR"),
        cache_entry_name("TST", "CONT_AWD_MINOR"),
    }
    assert "usaspending:latest_contract_award:TST" not in names


def test_no_singular_latest_award_key_exists_in_source() -> None:
    text = (REPO_ROOT / "src" / "market_relay_engine" / "context" / "usaspending_collector.py").read_text(encoding="utf-8")
    assert "latest_contract_award" not in text


def test_search_uei_false_positive_is_selection_noise(tmp_path: Path) -> None:
    client = FakeClient(
        searches={
            UEI: {
                "results": [_search_row("lookup-wrong", uei="OTHERUEI")],
                "page_metadata": {"hasNext": False},
            }
        },
    )
    result = _collector(tmp_path, client).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.SUCCESS
    assert result.rejected_search_candidate_count == 1
    assert result.indicator_snapshots == ()
    assert not any(issue.issue_type == "RECIPIENT_SEARCH_TEXT_NOT_EXACT" for issue in result.issues)
    assert not any(call[0] == "detail" for call in client.calls)


def test_detail_uei_mismatch_is_partial_and_emits_no_event(tmp_path: Path) -> None:
    client = _single_award_client(detail=_detail("CONT_AWD_1", uei="OTHERUEI"))
    result = _collector(tmp_path, client).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert result.indicator_snapshots == ()
    assert any(issue.issue_type == "DETAIL_RECIPIENT_UEI_MISMATCH" for issue in result.issues)


def test_missing_lookup_id_is_partial_without_detail_request(tmp_path: Path) -> None:
    row = _search_row("lookup-1")
    del row["generated_internal_id"]
    client = FakeClient(searches={UEI: {"results": [row], "page_metadata": {"hasNext": False}}})
    result = _collector(tmp_path, client).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert any(issue.issue_type == "AWARD_DETAIL_LOOKUP_ID_UNAVAILABLE" for issue in result.issues)
    assert not any(call[0] == "detail" for call in client.calls)


def test_funding_truncation_emits_partial_factual_event(tmp_path: Path) -> None:
    result = _collector(
        tmp_path,
        _single_award_client(funding=_funding(has_next=True)),
    ).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert len(result.indicator_snapshots) == 1
    details = result.indicator_snapshots[0].details
    assert details["funding_page_complete"] is False
    assert details["funding_has_next_page"] is True
    assert "current_transaction_obligation" not in json.dumps(details)
    assert any(issue.issue_type == "FUNDING_TRUNCATED" for issue in result.issues)


def test_funding_failure_emits_no_event_and_no_checkpoint(tmp_path: Path) -> None:
    client = _single_award_client()
    client.fail_funding = {"CONT_AWD_1"}
    result = _collector(tmp_path, client).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert result.indicator_snapshots == ()
    checkpoint = json.loads((tmp_path / "award_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["seen_event_fingerprints"] == {}
    assert any(issue.issue_type == "AWARD_ENRICHMENT_FAILED" for issue in result.issues)


def test_same_day_distinct_award_ids_have_distinct_context_ids(tmp_path: Path) -> None:
    details = {
        "lookup-1": _detail("CONT_AWD_1"),
        "lookup-2": _detail("CONT_AWD_2"),
    }
    details["CONT_AWD_1"] = details["lookup-1"]
    details["CONT_AWD_2"] = details["lookup-2"]
    client = FakeClient(
        searches={UEI: {"results": [_search_row("lookup-1"), _search_row("lookup-2")], "page_metadata": {"hasNext": False}}},
        details=details,
        funding={"CONT_AWD_1": _funding(), "CONT_AWD_2": _funding()},
    )
    result = _collector(tmp_path, client).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.SUCCESS
    assert len({snapshot.context_indicator_id for snapshot in result.indicator_snapshots}) == 2


def test_semantic_revision_changes_identity_and_replaces_same_award_cache(tmp_path: Path) -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    first = _collector(tmp_path, _single_award_client(), cache=cache, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    revised_client = _single_award_client(detail=_detail("CONT_AWD_1", amount=200.0))
    second = _collector(tmp_path, revised_client, cache=cache, writer=writer).collect(
        evaluation_time=CHECKED_AT + timedelta(hours=1),
        write_questdb=True,
    )

    assert first.indicator_snapshots[0].context_indicator_id != second.indicator_snapshots[0].context_indicator_id
    assert second.indicator_snapshots[0].value == "AWARD_REVISION_DISCOVERED"
    assert len(writer.snapshots) == 2
    entries = cache.latest_for_ticker("TST", now=CHECKED_AT + timedelta(hours=1))
    assert len([entry for entry in entries if entry.key.name == cache_entry_name("TST", "CONT_AWD_1")]) == 1


def test_candidate_last_modified_fallback_changes_revision_identity(tmp_path: Path) -> None:
    detail = _detail("CONT_AWD_1")
    del detail["last_modified_date"]
    detail.pop("last_modified", None)
    first_client = FakeClient(
        searches={
            UEI: {
                "results": [_search_row("lookup-1", last_modified="2026-06-20")],
                "page_metadata": {"hasNext": False},
            }
        },
        details={"lookup-1": detail, "CONT_AWD_1": detail},
        funding={"CONT_AWD_1": _funding()},
    )
    first = _collector(tmp_path, first_client).collect(evaluation_time=CHECKED_AT)
    changed_client = FakeClient(
        searches={
            UEI: {
                "results": [_search_row("lookup-1", last_modified="2026-06-21")],
                "page_metadata": {"hasNext": False},
            }
        },
        details={"lookup-1": detail, "CONT_AWD_1": detail},
        funding={"CONT_AWD_1": _funding()},
    )
    second = _collector(tmp_path, changed_client).collect(
        evaluation_time=CHECKED_AT + timedelta(hours=1)
    )

    first_details = first.indicator_snapshots[0].details
    second_details = second.indicator_snapshots[0].details
    assert first_details["award_last_modified_date"] == "2026-06-20"
    assert second_details["award_last_modified_date"] == "2026-06-21"
    assert (
        first_details["semantic_event_fingerprint"]
        != second_details["semantic_event_fingerprint"]
    )
    assert second.indicator_snapshots[0].value == "AWARD_REVISION_DISCOVERED"


def test_funding_evidence_revision_changes_identity(tmp_path: Path) -> None:
    first = _collector(tmp_path, _single_award_client()).collect(evaluation_time=CHECKED_AT)
    changed = _single_award_client(funding=_funding(object_class="26.0"))
    second = _collector(tmp_path, changed).collect(evaluation_time=CHECKED_AT + timedelta(hours=1))

    assert first.indicator_snapshots[0].context_indicator_id != second.indicator_snapshots[0].context_indicator_id
    assert second.indicator_snapshots[0].value == "AWARD_REVISION_DISCOVERED"


def test_restart_rehydrates_cache_without_duplicate_ledger_row(tmp_path: Path) -> None:
    writer = FakeWriter()
    first_cache = ContextStateCache()
    first = _collector(tmp_path, _single_award_client(), cache=first_cache, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    second_cache = ContextStateCache()
    second = _collector(tmp_path, _single_award_client(), cache=second_cache, writer=writer).collect(
        evaluation_time=CHECKED_AT + timedelta(minutes=10),
        write_questdb=True,
    )

    assert first.status is USAspendingCollectionStatus.SUCCESS
    assert second.status is USAspendingCollectionStatus.SUCCESS
    assert len(writer.snapshots) == 1
    assert second.indicator_snapshots == ()
    restored = second_cache.get_ticker(
        "TST",
        cache_entry_name("TST", "CONT_AWD_1"),
        now=CHECKED_AT + timedelta(minutes=10),
    )
    assert restored is not None
    assert restored.details["event_first_observed_at"] == first.indicator_snapshots[0].details["event_first_observed_at"]


def test_classification_does_not_drift_from_new_to_late(tmp_path: Path) -> None:
    _collector(tmp_path, _single_award_client()).collect(evaluation_time=CHECKED_AT)
    later = _collector(tmp_path, _single_award_client()).collect(
        evaluation_time=CHECKED_AT + timedelta(days=10)
    )
    checkpoint = json.loads((tmp_path / "award_checkpoint.json").read_text(encoding="utf-8"))
    records = list(checkpoint["seen_event_fingerprints"].values())

    assert later.indicator_snapshots == ()
    assert records[0]["event_classification"] == "NEW_AWARD_DISCOVERED"


def test_revision_recheck_finds_revision_when_search_empty(tmp_path: Path) -> None:
    first_client = _single_award_client()
    first_client.last_updated = "2026-06-01"
    first = _collector(tmp_path, first_client).collect(
        evaluation_time=datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    )
    assert first.status is USAspendingCollectionStatus.SUCCESS
    revised_detail = _detail("CONT_AWD_1", amount=300.0)
    client = FakeClient(
        searches={UEI: {"results": [], "page_metadata": {"hasNext": False}}},
        details={"CONT_AWD_1": revised_detail},
        funding={"CONT_AWD_1": _funding()},
    )
    result = _collector(tmp_path, client).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.SUCCESS
    assert len(result.indicator_snapshots) == 1
    assert result.indicator_snapshots[0].value == "AWARD_REVISION_DISCOVERED"


def test_caps_and_truncation_are_partial(tmp_path: Path) -> None:
    config = USAspendingConfig(enabled=True, max_award_details_per_recipient_per_run=1)
    client = FakeClient(
        searches={
            UEI: {
                "results": [_search_row("lookup-1"), _search_row("lookup-2")],
                "page_metadata": {"hasNext": True},
            }
        },
        details={"lookup-2": _detail("CONT_AWD_2"), "CONT_AWD_2": _detail("CONT_AWD_2")},
        funding={"CONT_AWD_2": _funding()},
    )
    result = _collector(tmp_path, client, config=config).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert result.coverage_complete is False
    assert {issue.issue_type for issue in result.issues} >= {
        "SEARCH_TRUNCATED",
        "CANDIDATE_ENRICHMENT_CAP_REACHED",
    }


def test_source_health_date_arithmetic_current_stale_future_and_failed(tmp_path: Path) -> None:
    assert _collector(tmp_path, FakeClient(last_updated="2026-06-20")).collect(
        evaluation_time=CHECKED_AT
    ).status is USAspendingCollectionStatus.SUCCESS
    assert _collector(tmp_path / "stale", FakeClient(last_updated="2026-06-01")).collect(
        evaluation_time=CHECKED_AT
    ).status is USAspendingCollectionStatus.STALE
    future = _collector(tmp_path / "future", FakeClient(last_updated="2026-06-21")).collect(
        evaluation_time=CHECKED_AT
    )
    failed_health = _collector(
        tmp_path / "health",
        FakeClient(fail_last_updated=True),
    ).collect(evaluation_time=CHECKED_AT)

    assert future.status is USAspendingCollectionStatus.PARTIAL
    assert any(issue.issue_type == "SOURCE_LAST_UPDATED_FUTURE" for issue in future.issues)
    assert failed_health.status is USAspendingCollectionStatus.PARTIAL


def test_empty_successful_search_with_failed_source_health_is_partial_not_failed(tmp_path: Path) -> None:
    result = _collector(
        tmp_path,
        FakeClient(fail_last_updated=True, searches={UEI: {"results": [], "page_metadata": {"hasNext": False}}}),
    ).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert result.indicator_snapshots == ()


def test_all_discovery_paths_failing_is_failed(tmp_path: Path) -> None:
    result = _collector(tmp_path, FakeClient(fail_search=True)).collect(evaluation_time=CHECKED_AT)

    assert result.status is USAspendingCollectionStatus.FAILED


def test_lock_contention_performs_no_work(tmp_path: Path) -> None:
    client = _single_award_client()
    store = JSONUSAspendingCheckpointStore(tmp_path / "award_checkpoint.json")
    store.acquire_lock()
    try:
        collector = USAspendingCollector(
            cache=ContextStateCache(),
            config=USAspendingConfig(enabled=True),
            client=client,
            checkpoint_store=store,
            recipient_mappings=(_mapping(),),
        )
        with pytest.raises(USAspendingCollectorBusyError):
            collector.collect(evaluation_time=CHECKED_AT)
    finally:
        store.release_lock()

    assert client.calls == []


def test_research_horizon_passing_does_not_create_expired_context(tmp_path: Path) -> None:
    cache = ContextStateCache()
    result = _collector(tmp_path, _single_award_client(), cache=cache).collect(
        evaluation_time=CHECKED_AT
    )
    future = CHECKED_AT + timedelta(days=10)
    snapshot = cache.to_context_state_snapshot(ticker="TST", now=future)

    assert result.indicator_snapshots[0].details["research_horizon_ends_at"]
    assert snapshot.risk_level is None
    assert "expired_context_present" not in snapshot.context_summary


def test_optional_writer_failure_is_partial_and_retryable(tmp_path: Path) -> None:
    writer = FakeWriter(fail=True)
    result = _collector(tmp_path, _single_award_client(), writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    checkpoint = json.loads((tmp_path / "award_checkpoint.json").read_text(encoding="utf-8"))

    assert result.status is USAspendingCollectionStatus.PARTIAL
    assert checkpoint["seen_event_fingerprints"] == {}
    assert any(issue.issue_type == "LEDGER_WRITE_FAILED" for issue in result.issues)


def test_no_forbidden_imports_or_trading_authority_terms() -> None:
    text = (REPO_ROOT / "src" / "market_relay_engine" / "context" / "usaspending_collector.py").read_text(encoding="utf-8")
    forbidden = (
        "market_relay_engine.risk",
        "market_relay_engine.model",
        "market_relay_engine.execution",
        "databento",
        "alpaca",
        "ContextFlag",
        "ContextAIEvent",
        "sleep(",
        "Thread(",
        "USASPENDING_API_KEY",
    )
    assert not any(term in text for term in forbidden)
