from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from market_relay_engine.context.external_event_archive import (
    CoverageStatus,
    ExternalEventArchive,
    ExternalEventArchiveError,
)
from market_relay_engine.context.veritawire import (
    VERITAWIRE_SOURCE,
    VeritaWireAuthenticationError,
    VeritaWireConnector,
    VeritaWireError,
    VeritaWireMessageError,
    VeritaWireSettings,
    build_authorization_headers,
    build_replay_url,
    parse_veritawire_message,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _payload(
    *,
    post_id: str = "114900000000000001",
    content: str = "<p>Lockheed Martin &amp; Palantir update.</p>",
    edited_at: str | None = None,
) -> str:
    return json.dumps(
        {
            "id": post_id,
            "created_at": "2026-07-18T11:59:50Z",
            "edited_at": edited_at,
            "content": content,
            "account": {
                "id": "107780257626128497",
                "username": "realDonaldTrump",
                "acct": "realDonaldTrump@truthsocial.com",
            },
            "uri": f"https://truthsocial.com/@realDonaldTrump/{post_id}",
            "url": f"https://truthsocial.com/@realDonaldTrump/{post_id}",
            "in_reply_to_id": None,
            "quote_id": "114800000000000009",
            "quote": {
                "id": "114800000000000009",
                "content": '<p>Quoted source <a href="https://example.test/a?utm_source=x">link</a></p>',
            },
            "media_attachments": [],
        }
    )


class FakeSocket:
    def __init__(self, values: list[str | bytes | Exception]) -> None:
        self.values = list(values)

    async def recv(self) -> str | bytes:
        if not self.values:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeContext:
    def __init__(
        self,
        socket: FakeSocket | None = None,
        *,
        enter_error: Exception | None = None,
    ) -> None:
        self.socket = socket
        self.enter_error = enter_error
        self.exited = False

    async def __aenter__(self) -> FakeSocket:
        if self.enter_error is not None:
            raise self.enter_error
        assert self.socket is not None
        return self.socket

    async def __aexit__(self, *_args: Any) -> None:
        self.exited = True


class FakeConnectFactory:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = list(contexts)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeContext:
        self.calls.append((url, kwargs))
        return self.contexts.pop(0)


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("secret-bearing provider error should not surface")
        self.status_code = status_code


class TrackingArchive(ExternalEventArchive):
    def __init__(self, root: Path, *, fail_object: bool = False) -> None:
        super().__init__(root, now=lambda: NOW)
        self.steps: list[str] = []
        self.fail_object = fail_object

    def archive_object(self, *args: Any, **kwargs: Any) -> str:
        self.steps.append("archive_object")
        if self.fail_object:
            raise ExternalEventArchiveError("safe archive failure")
        return super().archive_object(*args, **kwargs)

    def publish_revision(self, revision: Any) -> Any:
        self.steps.append("publish_revision")
        return super().publish_revision(revision)

    def publish_observation(self, *args: Any, **kwargs: Any) -> str:
        self.steps.append("publish_observation")
        return super().publish_observation(*args, **kwargs)

    def update_checkpoint(self, *args: Any, **kwargs: Any) -> int:
        self.steps.append("update_checkpoint")
        return super().update_checkpoint(*args, **kwargs)


def _connector(
    tmp_path: Path,
    *,
    archive: ExternalEventArchive | None = None,
    factory: FakeConnectFactory | None = None,
    sleeps: list[float] | None = None,
    api_key: str | None = "test-key-never-log",
    settings: VeritaWireSettings | None = None,
) -> VeritaWireConnector:
    async def sleeper(value: float) -> None:
        if sleeps is not None:
            sleeps.append(value)

    return VeritaWireConnector(
        settings=settings or VeritaWireSettings(enabled=True),
        archive=archive or ExternalEventArchive(tmp_path / "archive", now=lambda: NOW),
        connect_factory=factory,
        now=lambda: NOW,
        async_sleeper=sleeper,
        random_value=lambda: 0.5,
        api_key=api_key,
    )


def test_authorization_header_is_header_only_and_missing_key_fails() -> None:
    headers = build_authorization_headers("test-key-never-log")
    assert headers == {"Authorization": "Bearer test-key-never-log"}
    with pytest.raises(VeritaWireAuthenticationError, match="missing"):
        build_authorization_headers("")

    with pytest.raises(VeritaWireMessageError, match="source URI is invalid"):
        unsafe = json.loads(_payload())
        unsafe["uri"] = "https://user:secret@truthsocial.com/post"
        parse_veritawire_message(json.dumps(unsafe))

    with pytest.raises(VeritaWireMessageError, match="source URI is invalid"):
        unsafe = json.loads(_payload())
        unsafe["uri"] = "https://truthsocial.com/post?api_key=never-store-this"
        parse_veritawire_message(json.dumps(unsafe))


def test_websocket_endpoint_requires_header_only_authentication() -> None:
    with pytest.raises(VeritaWireError, match="header-only"):
        VeritaWireSettings(websocket_url="wss://user:secret@veritawire.com/ws")
    with pytest.raises(VeritaWireError, match="header-only"):
        VeritaWireSettings(websocket_url="wss://veritawire.com/ws?token=secret")
    with pytest.raises(VeritaWireError, match="official"):
        VeritaWireSettings(websocket_url="wss://example.test/ws")
    with pytest.raises(VeritaWireError, match="official"):
        VeritaWireSettings(websocket_url="wss://veritawire.com/other")


def test_replay_url_replaces_only_last_seen_id() -> None:
    url = build_replay_url(
        "wss://veritawire.com/ws?last_seen_id=old",
        "114900000000000001",
    )
    query = parse_qs(urlsplit(url).query)
    assert query == {"last_seen_id": ["114900000000000001"]}
    with pytest.raises(VeritaWireError, match="only last_seen_id"):
        build_replay_url("wss://veritawire.com/ws?token=secret", None)


def test_structured_payload_parsing_normalizes_html_and_relationships() -> None:
    post, raw, root = parse_veritawire_message(_payload())
    assert post.source_fact_id == "114900000000000001"
    assert post.created_at == datetime(2026, 7, 18, 11, 59, 50, tzinfo=UTC)
    assert "Lockheed Martin & Palantir update." in post.normalized_text
    assert "[QUOTED_POST 114800000000000009]" in post.normalized_text
    assert "https://example.test/a" in post.normalized_text
    assert b"Lockheed Martin" in raw
    assert root["id"] == post.source_fact_id


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("id"),
        lambda value: value.__setitem__("id", 123),
        lambda value: value.__setitem__("content", {"html": "bad"}),
        lambda value: value.__setitem__("account", "bad"),
        lambda value: value.__setitem__("created_at", "yesterday"),
    ],
)
def test_payload_required_field_drift_fails_closed(mutation: Any) -> None:
    value = json.loads(_payload())
    mutation(value)
    with pytest.raises(VeritaWireMessageError):
        parse_veritawire_message(json.dumps(value))


def test_optional_schema_drift_preserves_minimum_envelope_without_checkpoint(
    tmp_path: Path,
) -> None:
    value = json.loads(_payload())
    value["quote"] = "unexpected-shape"
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)

    with pytest.raises(VeritaWireMessageError, match="related-post field"):
        _connector(tmp_path, archive=archive).archive_message(json.dumps(value))

    assert archive.get_checkpoint(VERITAWIRE_SOURCE) is None
    object_files = tuple(archive.objects.glob("*/original.json"))
    assert len(object_files) == 1
    rejected = tuple(
        (archive.observations / VERITAWIRE_SOURCE).glob("*.json")
    )
    assert len(rejected) == 1
    safe_observation = json.loads(rejected[0].read_text(encoding="utf-8"))
    assert safe_observation["kind"] == "VERITAWIRE_REJECTED_DELIVERY"
    assert safe_observation["failure_category"] == "SCHEMA_DRIFT"
    assert "content" not in safe_observation


def test_archive_publication_precedes_checkpoint(tmp_path: Path) -> None:
    archive = TrackingArchive(tmp_path / "archive")
    result = _connector(tmp_path, archive=archive).archive_message(_payload())

    assert result.duplicate is False
    assert archive.steps.index("archive_object") < archive.steps.index(
        "publish_revision"
    )
    assert archive.steps.index("publish_revision") < archive.steps.index(
        "publish_observation"
    )
    assert archive.steps.index("publish_observation") < archive.steps.index(
        "update_checkpoint"
    )
    coverage = archive.load_coverage(VERITAWIRE_SOURCE)
    assert coverage is not None
    assert coverage.coverage_status is CoverageStatus.LIVE_ONLY
    assert coverage.coverage_start == NOW
    assert coverage.coverage_end == NOW
    revision = next(archive.iter_revisions(sources=(VERITAWIRE_SOURCE,)))
    assert revision.collection_mode == "LIVE_SYSTEM"


def test_archive_failure_does_not_advance_checkpoint(tmp_path: Path) -> None:
    archive = TrackingArchive(tmp_path / "archive", fail_object=True)
    with pytest.raises(ExternalEventArchiveError, match="archive failure"):
        _connector(tmp_path, archive=archive).archive_message(_payload())
    assert archive.get_checkpoint(VERITAWIRE_SOURCE) is None
    assert "update_checkpoint" not in archive.steps


def test_duplicate_replay_and_content_revision_are_idempotent(tmp_path: Path) -> None:
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    connector = _connector(tmp_path, archive=archive)

    first = connector.archive_message(_payload())
    replay = connector.archive_message(_payload())
    edited = connector.archive_message(
        _payload(
            content="<p>Edited official post.</p>",
            edited_at="2026-07-18T12:01:00Z",
        )
    )

    assert first.duplicate is False
    assert replay.duplicate is True
    assert replay.source_revision_id == first.source_revision_id
    assert edited.duplicate is False
    revisions = sorted(
        archive.iter_revisions(sources=(VERITAWIRE_SOURCE,)),
        key=lambda value: value.revision_sequence,
    )
    assert [value.revision_sequence for value in revisions] == [1, 2]
    assert revisions[1].supersedes_revision_id == revisions[0].source_revision_id
    assert archive.get_checkpoint(VERITAWIRE_SOURCE)["last_seen_id"] == (
        "114900000000000001"
    )
    lineage = archive.load_manifest()["observation_lineage"]
    assert len(lineage[first.source_revision_id]) == 2
    assert len(lineage[edited.source_revision_id]) == 1


def test_a_b_a_post_reversion_creates_a_new_current_lifecycle_revision(
    tmp_path: Path,
) -> None:
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    connector = _connector(tmp_path, archive=archive)

    first = connector.archive_message(_payload(content="<p>Version A</p>"))
    second = connector.archive_message(
        _payload(
            content="<p>Version B</p>",
            edited_at="2026-07-18T12:01:00Z",
        )
    )
    reverted = connector.archive_message(_payload(content="<p>Version A</p>"))

    assert first.duplicate is False
    assert second.duplicate is False
    assert reverted.duplicate is False
    revisions = sorted(
        archive.iter_revisions(sources=(VERITAWIRE_SOURCE,)),
        key=lambda value: value.revision_sequence,
    )
    assert [value.revision_sequence for value in revisions] == [1, 2, 3]
    assert revisions[2].canonical_content_hash == revisions[0].canonical_content_hash
    assert reverted.source_revision_id != first.source_revision_id
    assert revisions[2].supersedes_revision_id == revisions[1].source_revision_id


def test_changed_delivery_metadata_is_a_new_observation_not_content_revision(
    tmp_path: Path,
) -> None:
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    connector = _connector(tmp_path, archive=archive)
    original = json.loads(_payload())
    changed_delivery = dict(original)
    changed_delivery["source_delivery_metadata"] = {"revision_marker": "changed"}

    first = connector.archive_message(json.dumps(original, sort_keys=True))
    changed = connector.archive_message(
        json.dumps(changed_delivery, sort_keys=True)
    )

    assert first.duplicate is False
    assert changed.duplicate is True
    assert changed.source_revision_id == first.source_revision_id
    revisions = sorted(
        archive.iter_revisions(sources=(VERITAWIRE_SOURCE,)),
        key=lambda value: value.revision_sequence,
    )
    assert [value.revision_sequence for value in revisions] == [1]
    lineage = archive.load_manifest()["observation_lineage"]
    assert len(lineage[first.source_revision_id]) == 2
    observation_files = tuple(
        (archive.observations / VERITAWIRE_SOURCE).glob("*.json")
    )
    raw_hashes = {
        json.loads(path.read_text(encoding="utf-8"))["raw_object_hash"]
        for path in observation_files
        if json.loads(path.read_text(encoding="utf-8")).get("kind")
        == "VERITAWIRE_DELIVERY"
    }
    assert len(raw_hashes) == 2


def test_media_only_post_is_archived_but_marked_non_text_bearing(
    tmp_path: Path,
) -> None:
    value = json.loads(_payload(content=""))
    value["quote"] = None
    value["quote_id"] = None
    value["media_attachments"] = [{"id": "media-1", "type": "image"}]
    connector = _connector(tmp_path)

    result = connector.archive_message(json.dumps(value))

    assert result.text_bearing is False
    revision = next(connector.archive.iter_revisions())
    assert revision.normalized_text_hash is None


def test_successful_fake_connection_uses_private_disabled_transport_logger(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> tuple[int, FakeContext, FakeConnectFactory, VeritaWireConnector]:
        context = FakeContext(FakeSocket([_payload()]))
        factory = FakeConnectFactory([context])
        connector = _connector(tmp_path, factory=factory)
        received = await connector.run(max_messages=1, timeout_seconds=1)
        return received, context, factory, connector

    received, context, factory, connector = asyncio.run(scenario())

    assert received == 1
    assert context.exited is True
    url, kwargs = factory.calls[0]
    assert url == "wss://veritawire.com/ws"
    assert kwargs["additional_headers"] == {
        "Authorization": "Bearer test-key-never-log"
    }
    assert kwargs["ping_interval"] == 20
    transport_logger = kwargs["logger"]
    assert transport_logger.disabled is True
    assert transport_logger.propagate is False
    with caplog.at_level(logging.DEBUG):
        transport_logger.debug(
            "Authorization: Bearer test-key-never-log %s",
            _payload(),
        )
    assert "test-key-never-log" not in caplog.text
    assert "Lockheed Martin" not in caplog.text
    assert connector.health.new_record_count == 1
    assert connector.health.pending_classification_count == 1
    assert connector.health.completed_classification_count == 0
    assert connector.health.extractor_version == "veritawire_content_html_v1"


def test_reconnect_uses_checkpoint_last_seen_and_bounded_backoff(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, FakeConnectFactory, list[float], VeritaWireConnector]:
        archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
        archive.update_checkpoint(
            VERITAWIRE_SOURCE,
            {"last_seen_id": "114800000000000000"},
        )
        first = FakeContext(enter_error=ConnectionError("private detail"))
        second = FakeContext(FakeSocket([_payload()]))
        factory = FakeConnectFactory([first, second])
        sleeps: list[float] = []
        connector = _connector(
            tmp_path, archive=archive, factory=factory, sleeps=sleeps
        )
        received = await connector.run(max_messages=1, timeout_seconds=1)
        return received, factory, sleeps, connector

    received, factory, sleeps, connector = asyncio.run(scenario())

    assert received == 1
    assert sleeps == [0.5]
    assert connector.health.reconnect_count == 1
    assert parse_qs(urlsplit(factory.calls[0][0]).query)["last_seen_id"] == [
        "114800000000000000"
    ]
    assert parse_qs(urlsplit(factory.calls[1][0]).query)["last_seen_id"] == [
        "114800000000000000"
    ]


def test_authentication_rejection_is_fatal_and_secret_safe(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory = FakeConnectFactory([FakeContext(enter_error=StatusError(401))])
        connector = _connector(tmp_path, factory=factory)
        with pytest.raises(
            VeritaWireAuthenticationError, match="rejected authentication"
        ) as raised:
            await connector.run(max_messages=1, timeout_seconds=1)
        assert "test-key-never-log" not in str(raised.value)
        assert connector.health.failure_category == "AUTHENTICATION"

    asyncio.run(scenario())


def test_malformed_message_does_not_advance_checkpoint_or_stop_receiver(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, VeritaWireConnector]:
        factory = FakeConnectFactory(
            [FakeContext(FakeSocket(["not-json", _payload()]))]
        )
        connector = _connector(tmp_path, factory=factory)
        received = await connector.run(max_messages=1, timeout_seconds=1)
        return received, connector

    received, connector = asyncio.run(scenario())

    assert received == 1
    assert connector.health.malformed_count == 1
    assert connector.archive.get_checkpoint(VERITAWIRE_SOURCE)["last_seen_id"] == (
        "114900000000000001"
    )


def test_clean_task_cancellation_closes_connection_without_checkpoint(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[FakeContext, VeritaWireConnector]:
        context = FakeContext(FakeSocket([]))
        factory = FakeConnectFactory([context])
        connector = _connector(tmp_path, factory=factory)
        task = asyncio.create_task(connector.run(timeout_seconds=30))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return context, connector

    context, connector = asyncio.run(scenario())
    assert context.exited is True
    assert connector.archive.get_checkpoint(VERITAWIRE_SOURCE) is None


def test_missing_key_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VERITAWIRE_API_KEY", raising=False)
    async def scenario() -> FakeConnectFactory:
        factory = FakeConnectFactory([])
        connector = _connector(tmp_path, factory=factory, api_key=None)
        with pytest.raises(VeritaWireAuthenticationError, match="missing"):
            await connector.run(max_messages=1, timeout_seconds=1)
        return factory

    factory = asyncio.run(scenario())
    assert factory.calls == []


def test_safe_health_and_source_files_do_not_contain_secret_or_trading_imports(
    tmp_path: Path,
) -> None:
    connector = _connector(tmp_path)
    connector.archive_message(_payload())
    serialized = json.dumps(connector.health.safe_snapshot(), sort_keys=True)
    assert "test-key-never-log" not in serialized

    text = (
        Path(__file__).parents[2]
        / "src"
        / "market_relay_engine"
        / "context"
        / "veritawire.py"
    ).read_text(encoding="utf-8")
    assert "market_relay_engine.risk" not in text
    assert "market_relay_engine.execution" not in text
    assert "google.genai" not in text
    assert "alpaca" not in text.lower()
    assert "VERITAWARE_API_KEY" not in text


def test_classification_health_reconstructs_completed_state_after_restart(
    tmp_path: Path,
) -> None:
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    connector = _connector(tmp_path, archive=archive)
    archived = connector.archive_message(_payload())
    assert connector.health.pending_classification_count == 1
    archive.publish_readiness(
        source_revision_id=archived.source_revision_id,
        classification_input_fingerprint="a" * 64,
        canonical_classification_attempt_id="offline-attempt",
        complete_output_fingerprint="b" * 64,
        policy_output_fingerprint="c" * 64,
        profile_hash="d" * 64,
        classification_profile={"fixture": "veritawire-health"},
        classification_status="ABSTAINED",
        policy_eligible=False,
        context_event=None,
        evidence_ready_at=NOW,
    )

    restarted = _connector(
        tmp_path,
        archive=ExternalEventArchive(tmp_path / "archive", now=lambda: NOW),
    )

    assert restarted.health.pending_classification_count == 0
    assert restarted.health.completed_classification_count == 1
    assert restarted.health.last_source_record_identity == archived.source_fact_id
    assert restarted.health.last_system_receipt_time == NOW
