from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from market_relay_engine.context.external_event_archive import ExternalEventArchive
from market_relay_engine.context.external_event_archive import CoverageStatus
from market_relay_engine.context.external_sources import (
    BoundedHTTPClient,
    BoundedSourcePoller,
    EarningsDiscoveryAdapter,
    EarningsSettings,
    ExternalHTTPSettings,
    ExternalSourceError,
    LockheedMartinRSSAdapter,
    LockheedMartinRSSSettings,
    PalantirIRAdapter,
    PalantirIRSettings,
    SourceCollectionResult,
    SourceHealthStatus,
    discover_earnings_packages,
    parse_feed,
    parse_palantir_release_list,
)
from market_relay_engine.context.external_normalization import canonicalize_url


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        url: str = "https://example.test/resource",
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.url = url
        self.closed = False

    def iter_content(self, *, chunk_size: int):
        del chunk_size
        midpoint = len(self.content) // 2
        yield self.content[:midpoint]
        yield self.content[midpoint:]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FailingCheckpointArchive(ExternalEventArchive):
    def __init__(self, *args: Any, fail_on_call: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._checkpoint_calls = 0
        self._fail_on_call = fail_on_call

    def update_checkpoint(self, source: str, payload: Any) -> int:
        self._checkpoint_calls += 1
        if self._checkpoint_calls == self._fail_on_call:
            raise RuntimeError("simulated checkpoint failure")
        return super().update_checkpoint(source, payload)


def _client(
    responses: list[FakeResponse | Exception],
    *,
    max_retries: int = 2,
    max_response_bytes: int = 100_000,
    sleeps: list[float] | None = None,
) -> tuple[BoundedHTTPClient, FakeSession]:
    session = FakeSession(responses)
    client = BoundedHTTPClient(
        ExternalHTTPSettings(
            user_agent="Market Relay research contact@example.test",
            timeout_seconds=1,
            max_retries=max_retries,
            retry_base_delay_seconds=0.5,
            retry_max_delay_seconds=4,
            max_response_bytes=max_response_bytes,
        ),
        session=session,  # type: ignore[arg-type]
        now=lambda: NOW,
        sleeper=(sleeps if sleeps is not None else []).append,
    )
    return client, session


def _rss(*, link: str = "https://news.lockheedmartin.com/release-one") -> bytes:
    return f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>LMT</title>
      <item><title>Program update</title><link>{link}</link>
        <pubDate>Sat, 18 Jul 2026 11:00:00 GMT</pubDate></item>
    </channel></rss>""".encode()


def _article(text: str) -> bytes:
    return (
        '<html><nav>Ignore</nav><div class="wd_body wd_news_body fr-view">'
        f"<p>{text}</p></div><footer>Ignore</footer></html>"
    ).encode()


def _text_pdf(text_value: str) -> bytes:
    escaped = text_value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{object_id} 0 obj\n".encode("ascii"))
        payload.extend(value)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _pltr_payload(
    *,
    body: str = "<p>Official release body</p>",
    revision: int = 1,
    url: str = "/news-details/2026/example-release",
) -> bytes:
    return json.dumps(
        {
            "GetPressReleaseListResult": [
                {
                    "PressReleaseId": 901,
                    "RevisionNumber": revision,
                    "Headline": "Palantir Announces Contract",
                    "Body": body,
                    "LinkToDetailPage": url,
                    "PressReleaseDate": "07/18/2026 08:05:00",
                }
            ]
        }
    ).encode()


def _http_result(
    payload: bytes, *, url: str, content_type: str
) -> FakeResponse:
    return FakeResponse(
        content=payload,
        headers={"Content-Type": content_type},
        url=url,
    )


def _observation_payloads(archive: ExternalEventArchive, source: str) -> list[dict[str, Any]]:
    directory = archive.observations / source
    if not directory.exists():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]


def test_http_client_sends_conditional_headers_and_handles_304() -> None:
    response = FakeResponse(
        304,
        headers={"ETag": '"same"'},
        url="https://news.lockheedmartin.com/feed",
    )
    client, session = _client([response])

    result = client.get(
        "https://news.lockheedmartin.com/feed",
        conditional_headers={
            "If-None-Match": '"old"',
            "If-Modified-Since": "Sat, 18 Jul 2026 10:00:00 GMT",
        },
        allowed_domains=("news.lockheedmartin.com",),
    )

    assert result.not_modified is True
    assert result.content == b""
    assert session.calls[0][1]["headers"]["If-None-Match"] == '"old"'
    assert session.calls[0][1]["headers"]["If-Modified-Since"].startswith("Sat")
    assert response.closed is True


def test_http_client_fails_before_request_when_overall_deadline_expired() -> None:
    session = FakeSession([])
    client = BoundedHTTPClient(
        ExternalHTTPSettings(user_agent="Market Relay research contact@example.test"),
        session=session,  # type: ignore[arg-type]
        now=lambda: NOW,
        deadline_monotonic=10.0,
        monotonic=lambda: 10.0,
    )

    with pytest.raises(ExternalSourceError, match="deadline expired"):
        client.get(
            "https://news.lockheedmartin.com/feed",
            allowed_domains=("news.lockheedmartin.com",),
        )

    assert session.calls == []


def test_http_client_enforces_deadline_while_streaming_response_chunks() -> None:
    response = FakeResponse(
        content=b"bounded-response",
        headers={"Content-Type": "text/plain"},
        url="https://example.test/resource",
    )
    session = FakeSession([response])
    times = iter((0.0, 0.0, 0.0, 2.0))
    client = BoundedHTTPClient(
        ExternalHTTPSettings(
            user_agent="Market Relay research contact@example.test",
            timeout_seconds=5,
            max_retries=0,
            max_response_bytes=100,
        ),
        session=session,  # type: ignore[arg-type]
        deadline_monotonic=1.0,
        monotonic=lambda: next(times),
        now=lambda: NOW,
    )

    with pytest.raises(ExternalSourceError, match="deadline expired"):
        client.get(
            "https://example.test/resource",
            allowed_domains=("example.test",),
            accepted_content_types=("text/plain",),
        )

    assert response.closed is True


def test_http_client_retries_429_and_transport_with_bounded_sleep() -> None:
    sleeps: list[float] = []
    client, _session = _client(
        [
            requests.Timeout("private provider detail"),
            FakeResponse(
                429,
                headers={"Retry-After": "2"},
                url="https://investors.palantir.com/feed",
            ),
            FakeResponse(
                content=b"{}",
                headers={"Content-Type": "application/json"},
                url="https://investors.palantir.com/feed",
            ),
        ],
        sleeps=sleeps,
    )

    result = client.get(
        "https://investors.palantir.com/feed",
        allowed_domains=("investors.palantir.com",),
        accepted_content_types=("application/json",),
    )

    assert result.content == b"{}"
    assert sleeps == [0.5, 2.0]


@pytest.mark.parametrize("status_code", (403, 404))
def test_http_client_fails_closed_for_nonretryable_source_status(
    status_code: int,
) -> None:
    client, _session = _client(
        [
            FakeResponse(
                status_code=status_code,
                url="https://news.lockheedmartin.com/feed",
            )
        ],
        max_retries=2,
    )

    with pytest.raises(ExternalSourceError, match=f"HTTP {status_code}"):
        client.get(
            "https://news.lockheedmartin.com/feed",
            allowed_domains=("news.lockheedmartin.com",),
        )


def test_http_client_retries_5xx_then_fails_at_the_bound() -> None:
    sleeps: list[float] = []
    client, session = _client(
        [
            FakeResponse(503, url="https://investors.palantir.com/feed"),
            FakeResponse(503, url="https://investors.palantir.com/feed"),
        ],
        max_retries=1,
        sleeps=sleeps,
    )

    with pytest.raises(ExternalSourceError, match="HTTP 503"):
        client.get(
            "https://investors.palantir.com/feed",
            allowed_domains=("investors.palantir.com",),
        )

    assert len(session.calls) == 2
    assert sleeps == [0.5]


def test_http_client_rejects_content_type_mismatch_without_parsing_body() -> None:
    client, _session = _client(
        [
            FakeResponse(
                content=b"<html>generic error shell</html>",
                headers={"Content-Type": "text/html"},
                url="https://investors.palantir.com/feed",
            )
        ]
    )

    with pytest.raises(ExternalSourceError, match="content type"):
        client.get(
            "https://investors.palantir.com/feed",
            allowed_domains=("investors.palantir.com",),
            accepted_content_types=("application/json",),
        )


def test_http_client_rejects_oversize_and_off_domain_redirect() -> None:
    client, session = _client(
        [
            FakeResponse(
                content=b"12345",
                headers={"Content-Length": "5"},
                url="https://news.lockheedmartin.com/feed",
            )
        ],
        max_response_bytes=4,
    )
    with pytest.raises(ExternalSourceError, match="size limit"):
        client.get(
            "https://news.lockheedmartin.com/feed",
            allowed_domains=("news.lockheedmartin.com",),
        )

    client, _ = _client(
        [FakeResponse(content=b"x", url="https://attacker.example/challenge")]
    )
    with pytest.raises(ExternalSourceError, match="official domain"):
        client.get(
            "https://news.lockheedmartin.com/feed",
            allowed_domains=("news.lockheedmartin.com",),
        )


def test_http_client_rejects_off_domain_redirect_before_second_request() -> None:
    first = FakeResponse(
        status_code=302,
        headers={"Location": "https://attacker.example/source"},
        url="https://example.test/resource",
    )
    client, session = _client(
        [
            first,
            FakeResponse(
                content=b"must not be requested",
                url="https://attacker.example/source",
            ),
        ],
        max_retries=0,
    )

    with pytest.raises(ExternalSourceError, match="official domain"):
        client.get(
            "https://example.test/resource",
            allowed_domains=("example.test",),
        )

    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert first.closed is True


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test/source",
        "https://example.test/source?api_key=must-not-enter-a-url",
    ],
)
def test_http_client_rejects_credential_bearing_urls_before_request(
    url: str,
) -> None:
    client, session = _client([], max_retries=0)

    with pytest.raises(ExternalSourceError, match="credential"):
        client.get(url, allowed_domains=("example.test",))

    assert session.calls == []


def test_feed_parser_supports_rss_url_fallback_and_atom() -> None:
    rss = parse_feed(
        _rss(), max_items=1, base_url="https://news.lockheedmartin.com/feed"
    )
    assert rss.format == "RSS"
    assert rss.items[0].guid is None
    assert rss.items[0].identity == "https://news.lockheedmartin.com/release-one"
    assert rss.items[0].published_at == datetime(2026, 7, 18, 11, tzinfo=UTC)

    atom = parse_feed(
        b"""<feed xmlns="http://www.w3.org/2005/Atom">
        <entry><id>release-1</id><title>Release</title>
        <link rel="alternate" href="/release"/>
        <published>2026-07-18T11:00:00Z</published></entry></feed>""",
        max_items=1,
        base_url="https://example.test/feed",
    )
    assert atom.format == "ATOM"
    assert atom.items[0].identity == "release-1"
    assert atom.items[0].url == "https://example.test/release"


@pytest.mark.parametrize(
    "payload",
    [b"<rss><broken>", b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>'],
)
def test_feed_parser_fails_closed_on_malformed_or_unsafe_xml(payload: bytes) -> None:
    with pytest.raises(ExternalSourceError):
        parse_feed(payload, max_items=1, base_url="https://example.test/feed")


def test_lmt_adapter_archives_linked_article_and_suppresses_duplicate(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    responses = [
        _http_result(_rss(), url=feed_url, content_type="text/xml"),
        _http_result(
            _article("Lockheed Martin official body"),
            url="https://news.lockheedmartin.com/release-one",
            content_type="text/html",
        ),
        _http_result(_rss(), url=feed_url, content_type="text/xml"),
        _http_result(
            _article("Lockheed Martin official body"),
            url="https://news.lockheedmartin.com/release-one",
            content_type="text/html",
        ),
    ]
    client, _ = _client(responses)
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(client=client, archive=archive)

    first = adapter.collect_once(max_items=1)
    second = adapter.collect_once(max_items=1)

    assert first.new_count == 1
    assert second.duplicate_count == 1
    revisions = tuple(archive.iter_revisions(sources=("lockheed_martin_rss",)))
    assert len(revisions) == 1
    assert revisions[0].affected_tickers == ("LMT",)
    assert revisions[0].source_title == "Program update"
    assert archive.read_object(
        revisions[0].normalized_text_hash, filename="normalized.txt"  # type: ignore[arg-type]
    ) == b"Lockheed Martin official body"


def test_lmt_updated_body_publishes_immutable_revision(tmp_path: Path) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    client, _ = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Original body"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Edited body"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(client=client, archive=archive)
    adapter.collect_once(max_items=1)
    adapter.collect_once(max_items=1)

    revisions = sorted(
        archive.iter_revisions(sources=("lockheed_martin_rss",)),
        key=lambda value: value.revision_sequence,
    )
    assert [value.revision_sequence for value in revisions] == [1, 2]
    assert revisions[1].supersedes_revision_id == revisions[0].source_revision_id


def test_lmt_archives_official_article_before_extraction_failure(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    article_url = "https://news.lockheedmartin.com/release-one"
    unexpected_article = b"<html><main>source layout drift</main></html>"
    client, _ = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                unexpected_article,
                url=article_url,
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)

    with pytest.raises(ExternalSourceError, match="extraction failed"):
        LockheedMartinRSSAdapter(
            client=client,
            archive=archive,
        ).collect_once(max_items=1)

    article_hash = sha256(unexpected_article).hexdigest()
    assert (
        archive.objects / article_hash / "original.html"
    ).read_bytes() == unexpected_article
    checkpoint = archive.get_checkpoint("lockheed_martin_rss")
    assert checkpoint is not None
    assert checkpoint["pending_batches"][0]["item_ids"] == [article_url]
    assert tuple(archive.iter_revisions()) == ()


def test_lmt_duplicate_does_not_starve_later_new_item_within_scan_bound(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    first_url = "https://news.lockheedmartin.com/release-one"
    second_url = "https://news.lockheedmartin.com/release-two"
    two_item_feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>LMT</title>
      <item><title>Program update</title><link>{first_url}</link>
        <pubDate>Sat, 18 Jul 2026 11:00:00 GMT</pubDate></item>
      <item><title>New release</title><link>{second_url}</link>
        <pubDate>Sat, 18 Jul 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>""".encode()
    client, session = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Existing body"),
                url=first_url,
                content_type="text/html",
            ),
            FakeResponse(
                content=two_item_feed,
                url=feed_url,
                headers={"Content-Type": "text/xml", "ETag": '"feed-v2"'},
            ),
            _http_result(
                _article("Existing body"),
                url=first_url,
                content_type="text/html",
            ),
            FakeResponse(status_code=304, url=feed_url),
            _http_result(
                _article("New body"),
                url=second_url,
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(max_feed_items=2),
        now=lambda: NOW,
    )

    adapter.collect_once(max_items=1)
    second = adapter.collect_once(max_items=1)
    third = adapter.collect_once(max_items=1)

    assert second.discovered_count == 1
    assert second.duplicate_count == 1
    assert second.new_count == 0
    assert second.pending_count == 1
    assert third.not_modified is True
    assert third.discovered_count == 1
    assert third.new_count == 1
    assert third.pending_count == 0
    assert len(tuple(archive.iter_revisions(sources=("lockheed_martin_rss",)))) == 2
    assert [call[0] for call in session.calls[-4:]] == [
        feed_url,
        first_url,
        feed_url,
        second_url,
    ]


def test_lmt_two_new_items_max_one_then_304_drains_pending(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/feed"
    first_url = "https://news.lockheedmartin.com/release-one"
    second_url = "https://news.lockheedmartin.com/release-two"
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>LMT</title>
      <item><title>First</title><link>{first_url}</link>
        <pubDate>Sat, 18 Jul 2026 11:00:00 GMT</pubDate></item>
      <item><title>Second</title><link>{second_url}</link>
        <pubDate>Sat, 18 Jul 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>""".encode()
    client, session = _client(
        [
            FakeResponse(
                content=feed,
                url=feed_url,
                headers={"Content-Type": "text/xml", "ETag": '"feed-v1"'},
            ),
            _http_result(_article("First body"), url=first_url, content_type="text/html"),
            FakeResponse(status_code=304, url=feed_url),
            _http_result(_article("Second body"), url=second_url, content_type="text/html"),
        ]
    )
    archive_path = tmp_path / "archive"
    archive = ExternalEventArchive(archive_path, now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(
            feed_url=feed_url, max_feed_items=2
        ),
        now=lambda: NOW,
    )

    first = adapter.collect_once(max_items=1)
    archive = ExternalEventArchive(archive_path, now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(
            feed_url=feed_url, max_feed_items=2
        ),
        now=lambda: NOW,
    )
    second = adapter.collect_once(max_items=1)

    assert (first.new_count, first.pending_count) == (1, 1)
    assert (second.new_count, second.pending_count) == (1, 0)
    assert second.not_modified is True
    article_calls = [
        url for url, _kwargs in session.calls if url in {first_url, second_url}
    ]
    assert article_calls == [first_url, second_url]
    assert len(tuple(archive.iter_revisions(sources=("lockheed_martin_rss",)))) == 2


def test_lmt_a_b_a_reversion_creates_new_current_lifecycle_revision(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    client, _ = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Version A"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Version B"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Version A"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(client=client, archive=archive)

    results = [adapter.collect_once(max_items=1) for _ in range(3)]

    assert [value.new_count for value in results] == [1, 1, 1]
    assert [value.duplicate_count for value in results] == [0, 0, 0]
    revisions = sorted(
        archive.iter_revisions(sources=("lockheed_martin_rss",)),
        key=lambda value: value.revision_sequence,
    )
    assert [value.revision_sequence for value in revisions] == [1, 2, 3]
    assert revisions[2].supersedes_revision_id == revisions[1].source_revision_id
    assert revisions[2].source_revision_id != revisions[0].source_revision_id
    assert revisions[2].canonical_content_hash == revisions[0].canonical_content_hash


def test_lmt_changed_raw_html_with_same_normalized_text_is_a_revision(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    first_html = (
        b'<html><div class="wd_body wd_news_body fr-view">'
        b"<p>Stable official text</p></div></html>"
    )
    second_html = (
        b'<html><div class="wd_body wd_news_body fr-view">'
        b'<p class="source-revision">Stable official text</p></div></html>'
    )
    client, _ = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                first_html,
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                second_html,
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(client=client, archive=archive)

    adapter.collect_once(max_items=1)
    result = adapter.collect_once(max_items=1)

    revisions = sorted(
        archive.iter_revisions(sources=("lockheed_martin_rss",)),
        key=lambda value: value.revision_sequence,
    )
    assert result.new_count == 1
    assert [value.revision_sequence for value in revisions] == [1, 2]
    assert revisions[0].raw_object_hash != revisions[1].raw_object_hash
    assert revisions[0].normalized_text_hash == revisions[1].normalized_text_hash
    assert revisions[0].canonical_content_hash != revisions[1].canonical_content_hash
    assert revisions[1].supersedes_revision_id == revisions[0].source_revision_id


def test_lmt_checkpoint_failure_leaves_archived_revision_replay_safe(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/feed"
    client, _ = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Archived before checkpoint"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
        ]
    )
    archive = FailingCheckpointArchive(
        tmp_path / "archive", now=lambda: NOW, fail_on_call=2
    )
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="checkpoint failure"):
        adapter.collect_once(max_items=1)

    assert len(tuple(archive.iter_revisions())) == 1
    checkpoint = archive.get_checkpoint("lockheed_martin_rss")
    assert checkpoint is not None
    assert checkpoint["pending_batches"][0]["item_ids"] == [
        "https://news.lockheedmartin.com/release-one"
    ]


def test_lmt_conditional_second_poll_preserves_checkpoint_state(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/feed"
    client, session = _client(
        [
            FakeResponse(
                content=_rss(),
                headers={
                    "Content-Type": "text/xml",
                    "ETag": '"feed-v1"',
                    "Last-Modified": "Sat, 18 Jul 2026 11:00:00 GMT",
                },
                url=feed_url,
            ),
            _http_result(
                _article("Conditional article"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
            FakeResponse(304, headers={"ETag": '"feed-v1"'}, url=feed_url),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
        now=lambda: NOW,
    )
    adapter.collect_once(max_items=1)
    result = adapter.collect_once(max_items=1)

    assert result.not_modified is True
    headers = session.calls[2][1]["headers"]
    assert headers["If-None-Match"] == '"feed-v1"'
    assert headers["If-Modified-Since"].startswith("Sat")
    checkpoint = archive.get_checkpoint("lockheed_martin_rss")
    assert checkpoint["item_ids"] == [
        "https://news.lockheedmartin.com/release-one"
    ]


def test_lmt_bootstrap_archives_discovery_without_fetching_article(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    client, session = _client(
        [_http_result(_rss(), url=feed_url, content_type="text/xml")]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)

    result = LockheedMartinRSSAdapter(
        client=client, archive=archive
    ).collect_once(max_items=1, establish_checkpoint=True)

    assert result.skipped_count == 1
    assert tuple(archive.iter_revisions()) == ()
    assert len(session.calls) == 1
    assert archive.get_checkpoint("lockheed_martin_rss")["bootstrap_cutoff"]
    coverage = archive.load_coverage("lockheed_martin_rss")
    assert coverage is not None
    assert coverage.coverage_status is CoverageStatus.LIVE_ONLY
    assert coverage.coverage_start == NOW
    assert coverage.coverage_end == NOW


def test_lmt_bounded_backfill_filters_source_time_and_remains_partial(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/feed"
    client, session = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Bounded historical item"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
        now=lambda: NOW,
    )

    result = adapter.collect_backfill(
        start_time=datetime(2026, 7, 18, 10, tzinfo=UTC),
        end_time=datetime(2026, 7, 18, 12, tzinfo=UTC),
        max_items=1,
    )

    assert result.new_count == 1
    assert len(session.calls) == 2
    coverage = archive.load_coverage("lockheed_martin_rss")
    assert coverage is not None
    assert coverage.coverage_status is CoverageStatus.PARTIAL
    assert coverage.completed_backfill_ranges == ()
    revision = next(archive.iter_revisions(sources=("lockheed_martin_rss",)))
    assert revision.collection_mode == "BACKFILL"


def test_palantir_bounded_backfill_requires_both_dates(tmp_path: Path) -> None:
    client, _ = _client([])
    adapter = PalantirIRAdapter(
        client=client,
        archive=ExternalEventArchive(tmp_path / "archive"),
    )
    with pytest.raises(ExternalSourceError, match="requires explicit"):
        adapter.collect_once(year=2026, max_items=1, backfill=True)


def test_palantir_multiyear_backfill_max_one_bounds_total_requests(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    empty = json.dumps({"GetPressReleaseListResult": []}).encode()
    client, session = _client(
        [_http_result(empty, url=endpoint, content_type="application/json")]
    )
    adapter = PalantirIRAdapter(
        client=client,
        archive=ExternalEventArchive(tmp_path / "archive", now=lambda: NOW),
    )

    result = adapter.collect_backfill(
        start_time=datetime(2024, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 12, 31, tzinfo=UTC),
        max_items=1,
    )

    assert result.discovered_count == 0
    assert len(session.calls) == 1


def test_palantir_backfill_marks_archived_revision_as_backfill(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    client, _ = _client(
        [
            _http_result(
                _pltr_payload(),
                url=endpoint,
                content_type="application/json",
            )
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive)

    result = adapter.collect_backfill(
        start_time=datetime(2026, 7, 18, 12, tzinfo=UTC),
        end_time=datetime(2026, 7, 18, 13, tzinfo=UTC),
        max_items=1,
    )

    assert result.new_count == 1
    revision = next(archive.iter_revisions(sources=("palantir_ir",)))
    assert revision.collection_mode == "BACKFILL"


def test_lmt_rejects_off_domain_item_before_article_fetch(tmp_path: Path) -> None:
    feed_url = "https://news.lockheedmartin.com/feed"
    client, session = _client(
        [
            _http_result(
                _rss(link="https://attacker.example/article"),
                url=feed_url,
                content_type="text/xml",
            )
        ]
    )
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=ExternalEventArchive(tmp_path / "archive"),
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
    )

    with pytest.raises(ExternalSourceError, match="official domain"):
        adapter.collect_once(max_items=1)
    assert len(session.calls) == 1


def test_palantir_parser_uses_stable_id_and_eastern_publication_time() -> None:
    releases = parse_palantir_release_list(
        _pltr_payload(), official_domains=("investors.palantir.com",)
    )
    assert releases[0].press_release_id == "901"
    assert releases[0].revision_number == 1
    assert releases[0].canonical_url.endswith("/news-details/2026/example-release")
    assert releases[0].published_at == datetime(2026, 7, 18, 12, 5, tzinfo=UTC)


def test_palantir_parser_detects_schema_drift_and_off_domain() -> None:
    with pytest.raises(ExternalSourceError, match="schema root"):
        parse_palantir_release_list(
            b'{"unexpected": []}', official_domains=("investors.palantir.com",)
        )
    with pytest.raises(ExternalSourceError, match="official domain"):
        parse_palantir_release_list(
            _pltr_payload(url="https://attacker.example/release"),
            official_domains=("investors.palantir.com",),
        )


def test_palantir_adapter_archives_revision_and_fixed_scope(tmp_path: Path) -> None:
    endpoint = (
        "https://investors.palantir.com/feed/PressRelease.svc/"
        "GetPressReleaseList?languageId=1&bodyType=1&year=2026"
        "&includeTags=true&pressReleaseDateFilter=1"
    )
    client, _ = _client(
        [
            _http_result(_pltr_payload(), url=endpoint, content_type="application/json"),
            _http_result(
                _pltr_payload(body="<p>Changed body</p>", revision=2),
                url=endpoint,
                content_type="application/json",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive)

    adapter.collect_once(year=2026, max_items=1)
    adapter.collect_once(year=2026, max_items=1)

    revisions = sorted(
        archive.iter_revisions(sources=("palantir_ir",)),
        key=lambda value: value.revision_sequence,
    )
    assert len(revisions) == 2
    assert all(value.affected_tickers == ("PLTR",) for value in revisions)
    assert all(
        value.source_title == "Palantir Announces Contract"
        for value in revisions
    )
    assert revisions[1].supersedes_revision_id == revisions[0].source_revision_id


def test_palantir_authoritative_a_b_a_reversion_preserves_all_revisions(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    client, _ = _client(
        [
            _http_result(
                _pltr_payload(body="<p>Version A</p>", revision=1),
                url=endpoint,
                content_type="application/json",
            ),
            _http_result(
                _pltr_payload(body="<p>Version B</p>", revision=2),
                url=endpoint,
                content_type="application/json",
            ),
            _http_result(
                _pltr_payload(body="<p>Version A</p>", revision=3),
                url=endpoint,
                content_type="application/json",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive)

    results = [adapter.collect_once(year=2026, max_items=1) for _ in range(3)]

    assert [value.new_count for value in results] == [1, 1, 1]
    revisions = sorted(
        archive.iter_revisions(sources=("palantir_ir",)),
        key=lambda value: value.revision_sequence,
    )
    assert [value.revision_sequence for value in revisions] == [1, 2, 3]
    assert revisions[2].canonical_content_hash == revisions[0].canonical_content_hash
    assert revisions[2].source_revision_id != revisions[0].source_revision_id
    assert revisions[2].supersedes_revision_id == revisions[1].source_revision_id


def test_palantir_duplicate_does_not_starve_later_new_release_within_scan_bound(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    existing_payload = json.loads(_pltr_payload())
    existing = existing_payload["GetPressReleaseListResult"][0]
    new_release = {
        **existing,
        "PressReleaseId": 902,
        "Headline": "Palantir Announces Second Contract",
        "Body": "<p>Second official release body</p>",
        "LinkToDetailPage": "/news-details/2026/second-release",
        "PressReleaseDate": "07/18/2026 07:05:00",
    }
    two_release_payload = json.dumps(
        {"GetPressReleaseListResult": [existing, new_release]}
    ).encode()
    client, session = _client(
        [
            _http_result(
                _pltr_payload(), url=endpoint, content_type="application/json"
            ),
            FakeResponse(
                content=two_release_payload,
                url=endpoint,
                headers={
                    "Content-Type": "application/json",
                    "ETag": '"pltr-v2"',
                },
            ),
            FakeResponse(status_code=304, url=endpoint),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(
        client=client,
        archive=archive,
        settings=PalantirIRSettings(max_items=2),
        now=lambda: NOW,
    )

    adapter.collect_once(year=2026, max_items=1)
    second = adapter.collect_once(year=2026, max_items=1)
    third = adapter.collect_once(year=2026, max_items=1)

    assert second.discovered_count == 1
    assert second.duplicate_count == 1
    assert second.new_count == 0
    assert second.pending_count == 1
    assert third.not_modified is True
    assert third.discovered_count == 1
    assert third.new_count == 1
    assert third.pending_count == 0
    assert len(session.calls) == 3
    revisions = tuple(archive.iter_revisions(sources=("palantir_ir",)))
    assert {value.source_fact_id for value in revisions} == {"901", "902"}


def test_palantir_two_new_items_max_one_then_304_drains_pending(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    first = json.loads(_pltr_payload())["GetPressReleaseListResult"][0]
    second = {
        **first,
        "PressReleaseId": 902,
        "RevisionNumber": 1,
        "Headline": "Second release",
        "Body": "<p>Second official body</p>",
        "LinkToDetailPage": "/news-details/2026/second-release",
        "PressReleaseDate": "07/18/2026 07:05:00",
    }
    payload = json.dumps(
        {"GetPressReleaseListResult": [first, second]}
    ).encode()
    client, session = _client(
        [
            FakeResponse(
                content=payload,
                url=endpoint,
                headers={
                    "Content-Type": "application/json",
                    "ETag": '"pltr-v1"',
                },
            ),
            FakeResponse(status_code=304, url=endpoint),
        ]
    )
    archive_path = tmp_path / "archive"
    archive = ExternalEventArchive(archive_path, now=lambda: NOW)
    adapter = PalantirIRAdapter(
        client=client,
        archive=archive,
        settings=PalantirIRSettings(max_items=2),
        now=lambda: NOW,
    )

    first_result = adapter.collect_once(year=2026, max_items=1)
    archive = ExternalEventArchive(archive_path, now=lambda: NOW)
    adapter = PalantirIRAdapter(
        client=client,
        archive=archive,
        settings=PalantirIRSettings(max_items=2),
        now=lambda: NOW,
    )
    second_result = adapter.collect_once(year=2026, max_items=1)

    assert (first_result.new_count, first_result.pending_count) == (1, 1)
    assert (second_result.new_count, second_result.pending_count) == (1, 0)
    assert second_result.not_modified is True
    assert len(session.calls) == 2
    assert {
        value.source_fact_id
        for value in archive.iter_revisions(sources=("palantir_ir",))
    } == {"901", "902"}


@pytest.mark.parametrize(
    ("second_revision", "message"),
    [
        pytest.param(1, "conflicts with current content", id="same-sequence"),
        pytest.param(1, "nonmonotonic", id="lower-than-current"),
    ],
)
def test_palantir_changed_content_rejects_ambiguous_or_nonmonotonic_revision(
    tmp_path: Path,
    second_revision: int,
    message: str,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    first_revision = 1 if message.startswith("conflicts") else 2
    client, _ = _client(
        [
            _http_result(
                _pltr_payload(body="<p>Version A</p>", revision=first_revision),
                url=endpoint,
                content_type="application/json",
            ),
            _http_result(
                _pltr_payload(body="<p>Version B</p>", revision=second_revision),
                url=endpoint,
                content_type="application/json",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive)
    adapter.collect_once(year=2026, max_items=1)

    with pytest.raises(ExternalSourceError, match=message):
        adapter.collect_once(year=2026, max_items=1)

    revisions = tuple(archive.iter_revisions(sources=("palantir_ir",)))
    assert len(revisions) == 1
    assert revisions[0].revision_sequence == first_revision


@pytest.mark.parametrize("revision", [True, "2", 1.5])
def test_palantir_parser_rejects_revision_number_type_drift(
    revision: object,
) -> None:
    payload = json.loads(_pltr_payload())
    payload["GetPressReleaseListResult"][0]["RevisionNumber"] = revision

    with pytest.raises(ExternalSourceError, match="revision number is invalid"):
        parse_palantir_release_list(
            json.dumps(payload).encode(),
            official_domains=("investors.palantir.com",),
        )


def test_palantir_zero_result_after_nonzero_is_drift(tmp_path: Path) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    client, _ = _client(
        [
            _http_result(_pltr_payload(), url=endpoint, content_type="application/json"),
            _http_result(
                b'{"GetPressReleaseListResult": []}',
                url=endpoint,
                content_type="application/json",
            ),
        ]
    )
    adapter = PalantirIRAdapter(
        client=client,
        archive=ExternalEventArchive(tmp_path / "archive", now=lambda: NOW),
    )
    adapter.collect_once(year=2026, max_items=1)
    with pytest.raises(ExternalSourceError, match="zero results"):
        adapter.collect_once(year=2026, max_items=1)


def test_palantir_earnings_page_url_matches_feed_default_aspx_form(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    client, _ = _client(
        [
            _http_result(
                _pltr_payload(
                    url="/news-details/2026/q1-results/default.aspx"
                ),
                url=endpoint,
                content_type="application/json",
            )
        ]
    )
    adapter = PalantirIRAdapter(
        client=client,
        archive=ExternalEventArchive(tmp_path / "archive", now=lambda: NOW),
    )

    release = adapter.find_release(
        year=2026,
        canonical_url="https://investors.palantir.com/news-details/2026/q1-results/",
    )

    assert release.press_release_id == "901"


def test_earnings_discovery_records_release_and_supporting_links_only() -> None:
    page = b"""<html><section><h2>Q1 2026 Earnings</h2>
      <a href="/news-details/2026/q1-results">Earnings Release</a>
      <a href="/files/q1-slides.pdf">Presentation</a>
      <a href="/webcast/q1">Webcast</a></section></html>"""

    packages = discover_earnings_packages(
        page,
        ticker="PLTR",
        base_url="https://investors.palantir.com/events.html",
        official_domains=("investors.palantir.com",),
    )

    assert packages[0].package_id == "PLTR:2026:Q1"
    assert packages[0].primary_url.endswith("/news-details/2026/q1-results")
    assert len(packages[0].supporting_urls) == 2
    assert dict(packages[0].supporting_document_roles) == {
        "https://investors.palantir.com/files/q1-slides.pdf": "PRESENTATION",
        "https://investors.palantir.com/webcast/q1": "WEBCAST",
    }


def test_earnings_discovery_does_not_treat_quarter_in_headline_as_period() -> None:
    page = b"""<html>
      <section><h2>Q1 2021 Earnings</h2>
        <a href="/news-details/2021/q1-results">Earnings Release</a></section>
      <section><h2>Q4 2020 Earnings</h2>
        <p>Full-year results expect Q1 2021 revenue growth.</p>
        <a href="/news-details/2021/q4-results">Earnings Release</a></section>
      </html>"""

    packages = discover_earnings_packages(
        page,
        ticker="PLTR",
        base_url="https://investors.palantir.com/events.html",
        official_domains=("investors.palantir.com",),
    )

    assert [value.package_id for value in packages] == [
        "PLTR:2021:Q1",
        "PLTR:2020:Q4",
    ]


def test_earnings_discovery_never_borrows_release_from_next_quarter() -> None:
    page = b"""<html><section>
      <div><h2>Q1 2026 Earnings</h2><p>Release not yet published.</p></div>
      <div><h2>Q2 2026 Earnings</h2>
        <a href="/news-details/2026/q2-results">Earnings Release</a></div>
      </section></html>"""

    packages = discover_earnings_packages(
        page,
        ticker="PLTR",
        base_url="https://investors.palantir.com/events.html",
        official_domains=("investors.palantir.com",),
    )

    assert [(value.package_id, value.primary_url) for value in packages] == [
        (
            "PLTR:2026:Q2",
            "https://investors.palantir.com/news-details/2026/q2-results",
        )
    ]


def test_lmt_earnings_discovery_joins_standalone_year_and_quarter_headings() -> None:
    page = b"""<html><section><h2>2026</h2>
      <div><h3>Q1</h3>
        <a href="/static-files/q1-release">Press Release</a>
        <a href="/static-files/q1-tables">Financial Table - PDF</a>
        <a href="/static-files/q1-podcast">Q1 2026 Podcast</a>
      </div></section>
      <section><h2>2025</h2><div><h3>Q4</h3>
        <a href="/static-files/q4-release">Press Release</a>
      </div></section></html>"""

    packages = discover_earnings_packages(
        page,
        ticker="LMT",
        base_url=(
            "https://investors.lockheedmartin.com/financial-information/"
            "quarterly-results/"
        ),
        official_domains=("investors.lockheedmartin.com",),
    )

    assert [value.package_id for value in packages] == [
        "LMT:2026:Q1",
        "LMT:2025:Q4",
    ]
    assert packages[0].primary_url.endswith("/static-files/q1-release")


def test_lmt_earnings_html_is_archived_without_fetching_supporting_audio(
    tmp_path: Path,
) -> None:
    page_url = (
        "https://investors.lockheedmartin.com/financial-information/"
        "quarterly-results"
    )
    page = b"""<html><div><h2>First Quarter 2026</h2>
      <a href="/q1-release">Press Release</a>
      <a href="/q1-podcast">Podcast</a></div></html>"""
    release_url = "https://investors.lockheedmartin.com/q1-release"
    client, session = _client(
        [
            _http_result(page, url=page_url, content_type="text/html"),
            _http_result(
                b"<html><main><p>LMT quarterly results</p></main></html>",
                url=release_url,
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    pltr = PalantirIRAdapter(client=client, archive=archive)
    settings = EarningsSettings(lmt_results_url=page_url)

    result = EarningsDiscoveryAdapter(
        client=client, archive=archive, palantir_ir=pltr, settings=settings
    ).collect_once(ticker="LMT", max_items=1)

    assert result.new_count == 1
    assert len(session.calls) == 2
    assert all("podcast" not in call[0] for call in session.calls)
    revision = next(archive.iter_revisions(sources=("company_earnings",)))
    assert revision.affected_tickers == ("LMT",)
    assert revision.earnings_package_id == "LMT:2026:Q1"
    assert revision.correlation_group_id == "earnings:LMT:2026:Q1"
    assert revision.relationship_types == ("SAME_EARNINGS_OCCURRENCE",)
    package_revisions = archive.iter_earnings_package_revisions(
        ticker="LMT", fiscal_year=2026, fiscal_quarter=1
    )
    assert len(package_revisions) == 1
    package = package_revisions[0]
    assert package["package_state"] == "PRIMARY_ARCHIVED"
    primary = package["primary_document"]
    assert primary["role"] == "EARNINGS_RELEASE"
    assert primary["source_fact_id"] == "LMT:2026:Q1:EARNINGS_RELEASE"
    assert primary["source_revision_id"] == revision.source_revision_id
    assert primary["discovered_url"] == release_url
    assert primary["official_url"] == release_url
    assert primary["content_type"] == "text/html"
    assert primary["raw_object_hash"] == revision.raw_object_hash
    assert primary["document_hash"] == revision.document_hash
    assert primary["normalized_text_hash"] == revision.normalized_text_hash
    assert primary["canonical_content_hash"] == revision.canonical_content_hash
    assert primary["source_title"] == "LMT 2026 Q1 Earnings Release"
    assert primary["source_published_at"] is None
    assert primary["system_observed_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert primary["source_available_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert primary["archived_at"] == revision.archived_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert revision.normalized_at is not None
    assert primary["normalized_at"] == revision.normalized_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert primary["collection_mode"] == "LIVE_SYSTEM"
    assert primary["acquired"] is True
    assert primary["classification_eligible"] is True
    assert primary["lineage"] == {
        "source": "company_earnings",
        "source_fact_id": "LMT:2026:Q1:EARNINGS_RELEASE",
        "source_revision_id": revision.source_revision_id,
        "supersedes_revision_id": None,
        "correlation_group_id": "earnings:LMT:2026:Q1",
        "relationship_types": ["SAME_EARNINGS_OCCURRENCE"],
    }
    assert package["supporting_documents"] == [
        {
            "role": "AUDIO",
            "url": "https://investors.lockheedmartin.com/q1-podcast",
            "acquired": False,
            "classification_eligible": False,
        },
    ]
    assert package["supporting_links_acquired"] is False


def test_lmt_earnings_pdf_is_archived_and_extracted_with_bounded_profile(
    tmp_path: Path,
) -> None:
    page_url = (
        "https://investors.lockheedmartin.com/financial-information/"
        "quarterly-results"
    )
    release_url = "https://investors.lockheedmartin.com/q1-release.pdf"
    page = b"""<html><div><h2>First Quarter 2026</h2>
      <a href="/q1-release.pdf">Press Release</a>
      <a href="/q1-webcast">Webcast</a></div></html>"""
    pdf = _text_pdf("Lockheed Martin earnings guidance and cash flow.")
    client, session = _client(
        [
            _http_result(page, url=page_url, content_type="text/html"),
            _http_result(pdf, url=release_url, content_type="application/pdf"),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(
            lmt_results_url=page_url,
            max_pdf_pages=5,
            max_pdf_text_characters=10_000,
        ),
    )

    result = adapter.collect_once(ticker="LMT", max_items=1)

    assert result.new_count == 1
    assert len(session.calls) == 2
    revision = next(archive.iter_revisions(sources=("company_earnings",)))
    assert revision.extractor_version == "external_pdf_text_v2_bounded"
    assert revision.normalizer_version == "external_pdf_text_v2_bounded"
    assert archive.read_object(revision.raw_object_hash, filename="original.pdf") == pdf
    normalized = archive.read_object(
        revision.normalized_text_hash or "",
        filename="normalized.txt",
    ).decode("utf-8")
    assert "Lockheed Martin earnings guidance and cash flow" in normalized
    package = archive.iter_earnings_package_revisions(
        ticker="LMT",
        fiscal_year=2026,
        fiscal_quarter=1,
    )[0]
    assert package["primary_document"]["content_type"] == "application/pdf"


def test_earnings_two_new_packages_max_one_then_304_drains_pending(
    tmp_path: Path,
) -> None:
    page_url = "https://investors.lockheedmartin.com/quarterly-results"
    q2_url = "https://investors.lockheedmartin.com/q2-release"
    q1_url = "https://investors.lockheedmartin.com/q1-release"
    page = b"""<html><body>
      <section><h2>Second Quarter 2026</h2>
        <a href="/q2-release">Press Release</a></section>
      <section><h2>First Quarter 2026</h2>
        <a href="/q1-release">Press Release</a></section>
      </body></html>"""
    client, session = _client(
        [
            FakeResponse(
                content=page,
                url=page_url,
                headers={"Content-Type": "text/html", "ETag": '"earn-v1"'},
            ),
            _http_result(_article("Q2 results"), url=q2_url, content_type="text/html"),
            FakeResponse(status_code=304, url=page_url),
            _http_result(_article("Q1 results"), url=q1_url, content_type="text/html"),
        ]
    )
    archive_path = tmp_path / "archive"
    archive = ExternalEventArchive(archive_path, now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(lmt_results_url=page_url, max_items=2),
        now=lambda: NOW,
    )

    first = adapter.collect_once(ticker="LMT", max_items=1)
    archive = ExternalEventArchive(archive_path, now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(lmt_results_url=page_url, max_items=2),
        now=lambda: NOW,
    )
    second = adapter.collect_once(ticker="LMT", max_items=1)

    assert (first.new_count, first.pending_count) == (1, 1)
    assert (second.new_count, second.pending_count) == (1, 0)
    assert second.not_modified is True
    document_calls = [
        url for url, _kwargs in session.calls if url in {q1_url, q2_url}
    ]
    assert document_calls == [q2_url, q1_url]
    revisions = tuple(archive.iter_revisions(sources=("company_earnings",)))
    assert {value.earnings_package_id for value in revisions} == {
        "LMT:2026:Q1",
        "LMT:2026:Q2",
    }


def test_earnings_bootstrap_packages_remain_excluded_on_later_poll(
    tmp_path: Path,
) -> None:
    page_url = "https://investors.lockheedmartin.com/quarterly-results"
    page = b"""<html><section><h2>First Quarter 2026</h2>
      <a href="/q1-release">Press Release</a></section></html>"""
    client, session = _client(
        [
            FakeResponse(
                content=page,
                url=page_url,
                headers={"Content-Type": "text/html", "ETag": '"earn-v1"'},
            ),
            FakeResponse(
                content=page,
                url=page_url,
                headers={"Content-Type": "text/html", "ETag": '"earn-v2"'},
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(lmt_results_url=page_url),
        now=lambda: NOW,
    )

    bootstrap = adapter.collect_once(
        ticker="LMT", max_items=1, establish_checkpoint=True
    )
    later = adapter.collect_once(ticker="LMT", max_items=1)

    assert bootstrap.skipped_count == 1
    assert later.skipped_count == 1
    assert later.pending_count == 0
    assert later.new_count == 0
    assert tuple(archive.iter_revisions(sources=("company_earnings",))) == ()
    assert len(session.calls) == 2


def test_earnings_package_link_change_creates_immutable_package_revision(
    tmp_path: Path,
) -> None:
    page_url = "https://investors.lockheedmartin.com/quarterly-results"
    page_one = b"""<html><div><h2>First Quarter 2026</h2>
      <a href="/q1-release">Press Release</a>
      <a href="/q1-audio-v1">Podcast</a></div></html>"""
    page_two = page_one.replace(b"q1-audio-v1", b"q1-audio-v2")
    article = b"<html><main><p>Same official results</p></main></html>"
    release_url = "https://investors.lockheedmartin.com/q1-release"
    client, session = _client(
        [
            _http_result(page_one, url=page_url, content_type="text/html"),
            _http_result(article, url=release_url, content_type="text/html"),
            _http_result(page_two, url=page_url, content_type="text/html"),
            _http_result(article, url=release_url, content_type="text/html"),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(lmt_results_url=page_url),
        now=lambda: NOW,
    )

    first = adapter.collect_once(ticker="LMT", max_items=1)
    second = adapter.collect_once(ticker="LMT", max_items=1)

    assert first.new_count == 1
    assert second.duplicate_count == 1
    packages = archive.iter_earnings_package_revisions(
        ticker="LMT", fiscal_year=2026, fiscal_quarter=1
    )
    assert len(packages) == 2
    assert len(session.calls) == 4
    assert not any("audio-v" in call[0] for call in session.calls)


def test_earnings_backfill_is_bounded_and_never_claims_complete_coverage(
    tmp_path: Path,
) -> None:
    page_url = "https://investors.lockheedmartin.com/quarterly-results"
    page = b"""<html><div><h2>First Quarter 2026</h2>
      <a href="/q1-release">Press Release</a></div></html>"""
    release_url = "https://investors.lockheedmartin.com/q1-release"
    client, _ = _client(
        [
            _http_result(page, url=page_url, content_type="text/html"),
            _http_result(
                b"<html><main><p>Backfilled results</p></main></html>",
                url=release_url,
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(lmt_results_url=page_url),
        now=lambda: NOW,
    )

    result = adapter.collect_backfill(
        ticker="LMT",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 7, 18, tzinfo=UTC),
        max_items=1,
    )

    assert result.new_count == 1
    coverage = archive.load_coverage("company_earnings:LMT")
    assert coverage is not None
    assert coverage.coverage_status is CoverageStatus.PARTIAL
    assert coverage.completed_backfill_ranges == ()
    assert archive.load_coverage("company_earnings") is None
    revision = next(archive.iter_revisions(sources=("company_earnings",)))
    assert revision.collection_mode == "BACKFILL"
    assert revision.source_title == "LMT 2026 Q1 Earnings Release"


def test_palantir_earnings_maps_events_link_to_official_json_body(
    tmp_path: Path,
) -> None:
    page_url = "https://investors.palantir.com/events.html"
    detail_url = "https://investors.palantir.com/news-details/2026/example-release"
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    page = b"""<html><section><h2>Q1 2026 Earnings</h2>
      <a href="/news-details/2026/example-release">Earnings Release</a>
      <a href="/webcast/q1">Webcast</a></section></html>"""
    client, session = _client(
        [
            _http_result(page, url=page_url, content_type="text/html"),
            _http_result(
                _pltr_payload(), url=endpoint, content_type="application/json"
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    pltr = PalantirIRAdapter(
        client=client, archive=archive, now=lambda: NOW
    )

    result = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=pltr,
        now=lambda: NOW,
    ).collect_once(ticker="PLTR", max_items=1)

    assert result.new_count == 1
    assert [call[0] for call in session.calls] == [
        canonicalize_url(page_url),
        canonicalize_url(endpoint),
    ]
    assert detail_url not in [call[0] for call in session.calls]
    revision = next(archive.iter_revisions(sources=("company_earnings",)))
    assert revision.source_type == "OFFICIAL_EARNINGS_RELEASE"
    assert revision.earnings_package_id == "PLTR:2026:Q1"
    assert revision.correlation_group_id == "earnings:PLTR:2026:Q1"
    assert revision.source_title == "Palantir Announces Contract"
    assert revision.collection_mode == "LIVE_SYSTEM"
    assert archive.load_coverage("company_earnings:PLTR") is not None
    assert archive.load_coverage("company_earnings:LMT") is None
    assert archive.load_coverage("company_earnings") is None


def test_bounded_poller_uses_injected_interval_without_real_sleep() -> None:
    class Collector:
        def __init__(self) -> None:
            self.calls = 0

        def collect_once(self, *, max_items: int) -> SourceCollectionResult:
            self.calls += 1
            return SourceCollectionResult(
                source="test",
                observed_at=NOW,
                not_modified=False,
                discovered_count=max_items,
                new_count=0,
                duplicate_count=0,
                revision_count=0,
                skipped_count=0,
                checkpoint_generation=self.calls,
            )

    collector = Collector()
    sleeps: list[float] = []
    results = BoundedSourcePoller(
        collector, poll_interval_seconds=30, sleeper=sleeps.append
    ).run(max_items=1, max_polls=3)

    assert len(results) == 3
    assert sleeps == [30, 30]


def test_bounded_poller_extends_only_explicit_continuous_session_coverage(
    tmp_path: Path,
) -> None:
    class Settings:
        source = "test_source"

    class Collector:
        def __init__(self) -> None:
            self.archive = ExternalEventArchive(tmp_path / "archive")
            self.settings = Settings()
            self.times = [
                datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
                datetime(2026, 7, 18, 12, 0, 30, tzinfo=UTC),
            ]

        def collect_once(self, *, max_items: int) -> SourceCollectionResult:
            del max_items
            observed = self.times.pop(0)
            return SourceCollectionResult(
                source=self.settings.source,
                observed_at=observed,
                not_modified=False,
                discovered_count=0,
                new_count=0,
                duplicate_count=0,
                revision_count=0,
                skipped_count=0,
                checkpoint_generation=1,
            )

    collector = Collector()
    BoundedSourcePoller(
        collector, poll_interval_seconds=30, sleeper=lambda _value: None
    ).run(max_items=1, max_polls=2)

    coverage = collector.archive.load_coverage("test_source")
    assert coverage is not None
    assert coverage.coverage_start == datetime(2026, 7, 18, 12, tzinfo=UTC)
    assert coverage.coverage_end == datetime(
        2026, 7, 18, 12, 0, 30, tzinfo=UTC
    )
    assert coverage.known_gaps == ()


def test_external_source_module_has_no_trading_or_provider_imports() -> None:
    text = (
        Path(__file__).parents[2]
        / "src"
        / "market_relay_engine"
        / "context"
        / "external_sources.py"
    ).read_text(encoding="utf-8")
    assert "market_relay_engine.risk" not in text
    assert "market_relay_engine.execution" not in text
    assert "market_relay_engine.model" not in text
    assert "google.genai" not in text
    assert "alpaca" not in text.lower()


def test_lmt_bootstrap_hash_skips_unchanged_but_acquires_changed_item(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    changed_feed = _rss().replace(b"Program update", b"Program revised")
    article_url = "https://news.lockheedmartin.com/release-one"
    client, session = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(changed_feed, url=feed_url, content_type="text/xml"),
            _http_result(
                _article("Revised official article"),
                url=article_url,
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
        now=lambda: NOW,
    )

    baseline = adapter.collect_once(max_items=1, establish_checkpoint=True)
    unchanged = adapter.collect_once(max_items=1)
    changed = adapter.collect_once(max_items=1)

    checkpoint = archive.get_checkpoint("lockheed_martin_rss")
    assert checkpoint is not None
    assert list(checkpoint["bootstrap_item_hashes"]) == [article_url]
    assert checkpoint["bootstrap_item_hash_version"] == "rss_bootstrap_discovery_v1"
    assert baseline.skipped_count == 1
    assert unchanged.skipped_count == 1
    assert changed.new_count == 1
    assert len(session.calls) == 4


def test_palantir_bootstrap_hash_skips_unchanged_but_acquires_revision(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    client, session = _client(
        [
            _http_result(_pltr_payload(), url=endpoint, content_type="application/json"),
            _http_result(_pltr_payload(), url=endpoint, content_type="application/json"),
            _http_result(
                _pltr_payload(body="<p>Changed official body</p>", revision=2),
                url=endpoint,
                content_type="application/json",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive, now=lambda: NOW)

    baseline = adapter.collect_once(
        year=2026, max_items=1, establish_checkpoint=True
    )
    unchanged = adapter.collect_once(year=2026, max_items=1)
    changed = adapter.collect_once(year=2026, max_items=1)

    checkpoint = archive.get_checkpoint("palantir_ir:2026")
    assert checkpoint is not None
    assert list(checkpoint["bootstrap_release_hashes"]) == ["901"]
    assert (
        checkpoint["bootstrap_release_hash_version"]
        == "pltr_bootstrap_discovery_v1"
    )
    assert baseline.skipped_count == 1
    assert unchanged.skipped_count == 1
    assert changed.new_count == 1
    assert len(session.calls) == 3


def test_earnings_bootstrap_hash_skips_unchanged_but_acquires_link_change(
    tmp_path: Path,
) -> None:
    page_url = "https://investors.lockheedmartin.com/quarterly-results"
    page_v1 = b"""<html><section><h2>First Quarter 2026</h2>
      <a href="/q1-release-v1">Press Release</a></section></html>"""
    page_v2 = page_v1.replace(b"q1-release-v1", b"q1-release-v2")
    release_url = "https://investors.lockheedmartin.com/q1-release-v2"
    client, session = _client(
        [
            _http_result(page_v1, url=page_url, content_type="text/html"),
            _http_result(page_v1, url=page_url, content_type="text/html"),
            _http_result(page_v2, url=page_url, content_type="text/html"),
            _http_result(
                _article("Changed official earnings release"),
                url=release_url,
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = EarningsDiscoveryAdapter(
        client=client,
        archive=archive,
        palantir_ir=PalantirIRAdapter(client=client, archive=archive),
        settings=EarningsSettings(lmt_results_url=page_url),
        now=lambda: NOW,
    )

    baseline = adapter.collect_once(
        ticker="LMT", max_items=1, establish_checkpoint=True
    )
    unchanged = adapter.collect_once(ticker="LMT", max_items=1)
    changed = adapter.collect_once(ticker="LMT", max_items=1)

    checkpoint = archive.get_checkpoint("company_earnings:LMT")
    assert checkpoint is not None
    assert list(checkpoint["bootstrap_package_hashes"]) == ["LMT:2026:Q1"]
    assert (
        checkpoint["bootstrap_package_hash_version"]
        == "earnings_bootstrap_discovery_v1"
    )
    assert baseline.skipped_count == 1
    assert unchanged.skipped_count == 1
    assert changed.new_count == 1
    assert len(session.calls) == 4


def test_http_source_health_is_safe_persistent_and_tracks_success(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    client, _session = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(
                _article("HEALTH_BODY_SENTINEL"),
                url="https://news.lockheedmartin.com/release-one",
                content_type="text/html",
            ),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
        now=lambda: NOW,
    )

    result = adapter.collect_once(max_items=1)
    health = adapter.get_health()

    assert isinstance(health, SourceHealthStatus)
    assert health.source == "lockheed_martin_rss"
    assert health.enabled is True
    assert health.last_successful_poll_at == NOW
    assert health.last_source_record_identity == (
        "https://news.lockheedmartin.com/release-one"
    )
    assert health.last_system_receipt_at == NOW
    assert health.last_source_published_at == datetime(
        2026, 7, 18, 11, tzinfo=UTC
    )
    assert health.failure_category is None
    assert health.consecutive_failure_count == 0
    assert health.new_record_count == 1
    assert health.duplicate_count == 0
    assert health.pending_classification_count == 1
    assert health.completed_classification_count == 0
    assert health.checkpoint_generation == result.checkpoint_generation
    assert "lmt_article_html_v1" in health.parser_extraction_version

    reopened_archive = ExternalEventArchive(archive.root, now=lambda: NOW)
    restarted = LockheedMartinRSSAdapter(
        client=_client([])[0], archive=reopened_archive, now=lambda: NOW
    )
    assert restarted.get_health() == health
    safe_payload = json.dumps(
        archive.get_checkpoint("health:lockheed_martin_rss"), sort_keys=True
    )
    assert "HEALTH_BODY_SENTINEL" not in safe_payload
    assert "Authorization" not in safe_payload


def test_http_source_health_tracks_safe_consecutive_failure_category(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    invalid = b'{"unexpected":"SCHEMA_BODY_SENTINEL"}'
    client, _session = _client(
        [
            _http_result(invalid, url=endpoint, content_type="application/json"),
            _http_result(invalid, url=endpoint, content_type="application/json"),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive, now=lambda: NOW)

    for expected_count in (1, 2):
        with pytest.raises(ExternalSourceError, match="schema root changed"):
            adapter.collect_once(year=2026, max_items=1)
        health = adapter.get_health(year=2026)
        assert health is not None
        assert health.failure_category == "PARSER_SCHEMA_DRIFT"
        assert health.consecutive_failure_count == expected_count
        assert health.last_failure_at == NOW

    safe_payload = json.dumps(
        archive.get_checkpoint("health:palantir_ir:2026"), sort_keys=True
    )
    assert "SCHEMA_BODY_SENTINEL" not in safe_payload


def test_palantir_schema_rejection_references_archived_raw_object_without_body(
    tmp_path: Path,
) -> None:
    endpoint = PalantirIRSettings().endpoint_template.format(year=2026)
    payload = b'{"unexpected":"REJECTED_SCHEMA_SENTINEL"}'
    client, _session = _client(
        [_http_result(payload, url=endpoint, content_type="application/json")]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = PalantirIRAdapter(client=client, archive=archive, now=lambda: NOW)

    with pytest.raises(ExternalSourceError, match="schema root changed"):
        adapter.collect_once(year=2026, max_items=1)

    rejected = [
        value
        for value in _observation_payloads(archive, "palantir_ir")
        if value.get("kind") == "REJECTED_HTTP_OBSERVATION"
    ]
    assert len(rejected) == 1
    assert rejected[0]["stage"] == "INDEX_PARSE"
    assert rejected[0]["failure_category"] == "PARSER_SCHEMA_DRIFT"
    object_hash = sha256(payload).hexdigest()
    assert rejected[0]["raw_object_hash"] == object_hash
    assert archive.read_object(object_hash, filename="original.json") == payload
    assert "REJECTED_SCHEMA_SENTINEL" not in json.dumps(rejected[0])


def test_lmt_empty_extraction_rejection_references_archived_raw_object(
    tmp_path: Path,
) -> None:
    feed_url = "https://news.lockheedmartin.com/news-releases?pagetemplate=rss"
    article_url = "https://news.lockheedmartin.com/release-one"
    article = b"<html><main>EMPTY_EXTRACTION_SENTINEL</main></html>"
    client, _session = _client(
        [
            _http_result(_rss(), url=feed_url, content_type="text/xml"),
            _http_result(article, url=article_url, content_type="text/html"),
        ]
    )
    archive = ExternalEventArchive(tmp_path / "archive", now=lambda: NOW)
    adapter = LockheedMartinRSSAdapter(
        client=client,
        archive=archive,
        settings=LockheedMartinRSSSettings(feed_url=feed_url),
        now=lambda: NOW,
    )

    with pytest.raises(ExternalSourceError, match="article extraction failed"):
        adapter.collect_once(max_items=1)

    rejected = [
        value
        for value in _observation_payloads(archive, "lockheed_martin_rss")
        if value.get("kind") == "REJECTED_HTTP_OBSERVATION"
    ]
    assert len(rejected) == 1
    assert rejected[0]["stage"] == "ARTICLE_EXTRACTION"
    assert rejected[0]["source_fact_id"] == article_url
    object_hash = sha256(article).hexdigest()
    assert rejected[0]["raw_object_hash"] == object_hash
    assert archive.read_object(object_hash, filename="original.html") == article
    assert "EMPTY_EXTRACTION_SENTINEL" not in json.dumps(rejected[0])
