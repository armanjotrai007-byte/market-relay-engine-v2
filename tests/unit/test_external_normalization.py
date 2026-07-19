from __future__ import annotations

import pytest

from market_relay_engine.context.external_normalization import (
    ExternalNormalizationError,
    build_scope_aware_excerpt,
    canonicalize_url,
    extract_pdf_text,
    normalize_html_fragment,
    resolve_explicit_scope,
    union_scope,
)


APPROVED_TICKERS = ("PLTR", "LMT", "RTX")
APPROVED_SECTORS = ("DEFENSE", "ENERGY")


def _text_pdf(*page_texts: str) -> bytes:
    page_count = len(page_texts)
    font_id = 3 + (2 * page_count)
    page_ids = [3 + (2 * index) for index in range(page_count)]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{value} 0 R' for value in page_ids)}] "
            f"/Count {page_count} >>"
        ).encode("ascii"),
    ]
    for index, text_value in enumerate(page_texts):
        page_id = page_ids[index]
        content_id = page_id + 1
        escaped = text_value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                ).encode("ascii"),
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
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


def test_html_normalization_preserves_paragraphs_and_canonical_links() -> None:
    normalized = normalize_html_fragment(
        """
        <article>
          <p>First &amp; second.</p>
          <p>Read <a href="https://Example.com/path/?utm_source=x&amp;b=2&amp;a=1">details</a>.</p>
          <script>steal_credentials()</script>
        </article>
        """
    )

    assert "First & second." in normalized
    assert "details [https://example.com/path?a=1&b=2]" in normalized
    assert normalized.count("First & second.") == 1
    assert normalized.count("details [https://example.com/path?a=1&b=2]") == 1
    assert "steal_credentials" not in normalized
    assert "\n\n" in normalized


def test_scope_resolver_collects_all_tickers_sectors_and_global_relevance() -> None:
    scope = resolve_explicit_scope(
        (
            "Lockheed Martin and Palantir will support the defense industrial base "
            "while Federal Reserve rate policy affects the broader economy."
        ),
        approved_tickers=APPROVED_TICKERS,
    )

    assert scope.tickers == ("LMT", "PLTR")
    assert scope.sectors == ("DEFENSE",)
    assert scope.global_relevance is True
    assert {(span.kind, span.value) for span in scope.supporting_spans} == {
        ("TICKER", "LMT"),
        ("TICKER", "PLTR"),
        ("SECTOR", "DEFENSE"),
        ("GLOBAL", "GLOBAL"),
    }


def test_alias_orderings_produce_identical_scope_and_fingerprint() -> None:
    first = resolve_explicit_scope(
        "Lockheed Martin and Palantir discussed defense contractors.",
        approved_tickers=APPROVED_TICKERS,
    )
    second = resolve_explicit_scope(
        "Defense contractors heard from PLTR and LMT.",
        approved_tickers=APPROVED_TICKERS,
    )

    assert first.tickers == second.tickers == ("LMT", "PLTR")
    assert first.sectors == second.sectors == ("DEFENSE",)
    assert first.fingerprint == second.fingerprint


def test_union_scope_keeps_fixed_and_explicit_scope_and_validated_ai_scope() -> None:
    explicit = resolve_explicit_scope(
        "Palantir discussed the defense sector and global sanctions.",
        approved_tickers=APPROVED_TICKERS,
    )
    combined = union_scope(
        fixed_tickers=("LMT",),
        deterministic=explicit,
        ai_tickers=("RTX", "PLTR"),
        ai_sectors=("ENERGY", "DEFENSE"),
        ai_global_relevance=False,
        approved_tickers=APPROVED_TICKERS,
        approved_sectors=APPROVED_SECTORS,
    )

    assert combined.tickers == ("LMT", "PLTR", "RTX")
    assert combined.sectors == ("DEFENSE", "ENERGY")
    assert combined.global_relevance is True


def test_scope_aware_excerpt_includes_relevant_middle_passage() -> None:
    opening = "Opening company context. " + ("ordinary background " * 90)
    middle = (
        "Lockheed Martin described a constraint affecting the defense industrial base."
    )
    ending = "Routine appendix. " * 180
    document = opening + "\n\n" + middle + "\n\n" + ending
    assert document.index("Lockheed Martin") > 1_500
    scope = resolve_explicit_scope(document, approved_tickers=APPROVED_TICKERS)

    excerpt = build_scope_aware_excerpt(
        document,
        title="Quarterly release",
        scope=scope,
        max_characters=2_400,
    )

    assert excerpt.truncated is True
    assert excerpt.excerpt_character_count <= 2_400
    assert "Lockheed Martin" in excerpt.text
    assert "defense industrial base" in excerpt.text
    assert {(span.kind, span.value) for span in excerpt.included_spans} >= {
        ("TICKER", "LMT"),
        ("SECTOR", "DEFENSE"),
    }
    assert excerpt.omitted_scope_values == ()


def test_earnings_excerpt_prioritizes_guidance_in_long_document() -> None:
    document = (
        "Company quarterly announcement.\n"
        + ("Introductory material. " * 120)
        + "\nGUIDANCE\nRevenue guidance increased for the next fiscal year.\n"
        + ("Appendix material. " * 240)
    )
    excerpt = build_scope_aware_excerpt(
        document,
        title="Earnings Release",
        scope=resolve_explicit_scope(document, approved_tickers=APPROVED_TICKERS),
        max_characters=2_400,
        earnings=True,
    )

    assert excerpt.truncated is True
    assert "Revenue guidance increased" in excerpt.text
    assert "EARNINGS:GUIDANCE_OUTLOOK" in excerpt.text


def test_canonical_url_drops_tracking_and_normalizes_query_order() -> None:
    assert canonicalize_url(
        "HTTPS://Investors.Palantir.com/news/release/?utm_campaign=x&b=2&a=1#top"
    ) == "https://investors.palantir.com/news/release?a=1&b=2"


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "https://user:secret@example.com/release",
        "https://example.com/release?api_key=never-store-this",
        "https://example.com/release?access-token=never-store-this",
        "https://example.com/release?X-Amz-Signature=never-store-this",
    ),
)
def test_canonical_url_rejects_credentials(unsafe_url: str) -> None:
    with pytest.raises(ExternalNormalizationError, match="credential"):
        canonicalize_url(unsafe_url)


def test_pdf_text_extraction_is_deterministic_and_bounded() -> None:
    content = _text_pdf(
        "Lockheed Martin quarterly earnings release.",
        "Guidance and cash flow discussion.",
    )

    first = extract_pdf_text(content, max_pages=2, max_characters=2_000)
    second = extract_pdf_text(content, max_pages=2, max_characters=2_000)

    assert first == second
    assert "Lockheed Martin quarterly earnings release" in first
    assert "Guidance and cash flow discussion" in first
    with pytest.raises(ExternalNormalizationError, match="page limit"):
        extract_pdf_text(content, max_pages=1, max_characters=2_000)
    with pytest.raises(ExternalNormalizationError, match="text limit"):
        extract_pdf_text(
            _text_pdf("X" * 1_200),
            max_pages=1,
            max_characters=1_000,
        )
