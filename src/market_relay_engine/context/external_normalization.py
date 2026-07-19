"""Deterministic text extraction, explicit-scope discovery, and excerpts."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import re
from typing import Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from pypdf import PdfReader


HTML_NORMALIZER_VERSION = "external_html_text_v1"
PDF_EXTRACTOR_VERSION = "external_pdf_text_v2_bounded"
SCOPE_RESOLVER_VERSION = "external_scope_v2"
EXCERPT_VERSION = "scope_aware_excerpt_v1"

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "accesstoken",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "password",
        "passwd",
        "secret",
        "signature",
        "sig",
        "token",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
    }
)


class ExternalNormalizationError(ValueError):
    """Raised when source text cannot be extracted without guessing."""


@dataclass(frozen=True, kw_only=True)
class ScopeSpan:
    kind: str
    value: str
    alias: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.kind not in {"TICKER", "SECTOR", "GLOBAL"}:
            raise ExternalNormalizationError("scope span kind is invalid")
        if not self.value or not self.alias:
            raise ExternalNormalizationError("scope span value and alias are required")
        if self.start < 0 or self.end <= self.start:
            raise ExternalNormalizationError("scope span offsets are invalid")


@dataclass(frozen=True, kw_only=True)
class ResolvedScope:
    tickers: tuple[str, ...] = field(default_factory=tuple)
    sectors: tuple[str, ...] = field(default_factory=tuple)
    global_relevance: bool = False
    supporting_spans: tuple[ScopeSpan, ...] = field(default_factory=tuple)
    resolver_version: str = SCOPE_RESOLVER_VERSION

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            [
                self.resolver_version,
                ",".join(self.tickers),
                ",".join(self.sectors),
                "1" if self.global_relevance else "0",
            ]
        )
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ScopeAwareExcerpt:
    text: str
    full_character_count: int
    excerpt_character_count: int
    full_hash: str
    excerpt_hash: str
    truncated: bool
    included_spans: tuple[ScopeSpan, ...]
    omitted_scope_values: tuple[str, ...]
    excerpt_version: str = EXCERPT_VERSION


DEFAULT_TICKER_ALIASES: Mapping[str, tuple[str, ...]] = {
    "LMT": ("$LMT", "LMT", "Lockheed Martin", "Lockheed"),
    "PLTR": ("$PLTR", "PLTR", "Palantir Technologies", "Palantir"),
}

DEFAULT_SECTOR_ALIASES: Mapping[str, tuple[str, ...]] = {
    "DEFENSE": (
        "defense industrial base",
        "defense industry",
        "defense sector",
        "defense contractors",
        "military contractors",
        "defense procurement",
    ),
    "ENERGY": (
        "oil and gas industry",
        "oil industry",
        "oil sector",
        "petroleum industry",
        "energy industry",
        "energy sector",
    ),
}

DEFAULT_GLOBAL_ALIASES: tuple[str, ...] = (
    "across-the-board tariffs",
    "worldwide tariffs",
    "global sanctions",
    "worldwide sanctions",
    "federal government shutdown",
    "federal budget",
    "Federal Reserve interest rates",
    "Federal Reserve rate policy",
)

_EARNINGS_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
    for name, pattern in (
        ("RESULTS_HIGHLIGHTS", r"^(?:financial )?(?:results|highlights|key results)\b.*$"),
        ("GUIDANCE_OUTLOOK", r"^(?:guidance|outlook|financial outlook)\b.*$"),
        ("SEGMENT_RESULTS", r"^(?:segment|business segment) results\b.*$"),
        ("BACKLOG_CUSTOMER", r"^(?:backlog|customers?|contracts?)\b.*$"),
        ("MARGIN_CASH_FLOW", r"^(?:margins?|cash flow|free cash flow)\b.*$"),
        ("MATERIAL_CHARGES", r"^(?:charges?|impairments?|restructuring)\b.*$"),
        ("OPERATIONAL_CONSTRAINTS", r"^(?:operational|supply chain|constraints?)\b.*$"),
    )
)


def normalize_html_fragment(html: str) -> str:
    """Normalize HTML without executing it or fetching linked resources."""
    if not isinstance(html, str):
        raise ExternalNormalizationError("HTML content must be a string")
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(("script", "style", "noscript", "template", "svg", "canvas")):
        element.decompose()
    for link in soup.find_all("a"):
        href = link.get("href")
        label = " ".join(link.get_text(" ", strip=True).split())
        if isinstance(href, str) and _safe_http_url(href):
            canonical = canonicalize_url(href)
            replacement = canonical if not label or label == canonical else f"{label} [{canonical}]"
        else:
            replacement = label
        link.replace_with(NavigableString(replacement))
    blocks: list[str] = []
    block_names = (
        "p",
        "div",
        "section",
        "article",
        "blockquote",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "pre",
    )
    for element in soup.find_all(block_names):
        if isinstance(element.parent, Tag) and element.parent.name in {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "pre"}:
            continue
        if element.name in {"div", "section", "article"} and element.find(block_names):
            # Container text is the concatenation of its child blocks; emitting
            # both would duplicate source content in the classifier input.
            continue
        value = " ".join(element.get_text(" ", strip=True).split())
        if value and (not blocks or value != blocks[-1]):
            blocks.append(value)
    if not blocks:
        value = " ".join(soup.get_text(" ", strip=True).split())
        if value:
            blocks.append(value)
    return _clean_text("\n\n".join(blocks))


def extract_article_html(
    html: bytes | str,
    *,
    selectors: Sequence[str],
    remove_selectors: Sequence[str] = ("nav", "footer", "form", ".cookie", ".breadcrumbs"),
) -> str:
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    if not isinstance(text, str):
        raise ExternalNormalizationError("article HTML must be bytes or string")
    soup = BeautifulSoup(text, "html.parser")
    for selector in remove_selectors:
        for element in soup.select(selector):
            element.decompose()
    body: Tag | None = None
    for selector in selectors:
        candidate = soup.select_one(selector)
        if isinstance(candidate, Tag):
            body = candidate
            break
    if body is None:
        raise ExternalNormalizationError("expected official article body was not found")
    normalized = normalize_html_fragment(str(body))
    if not normalized:
        raise ExternalNormalizationError("official article extraction produced empty text")
    return normalized


def extract_pdf_text(
    content: bytes,
    *,
    max_pages: int = 200,
    max_characters: int = 1_000_000,
) -> str:
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ExternalNormalizationError("PDF page limit must be positive")
    if (
        isinstance(max_characters, bool)
        or not isinstance(max_characters, int)
        or max_characters < 1_000
    ):
        raise ExternalNormalizationError("PDF text limit must be at least 1000")
    try:
        reader = PdfReader(BytesIO(content))
    except Exception as exc:  # pypdf raises several format-specific exceptions.
        raise ExternalNormalizationError("PDF could not be opened") from exc
    if reader.is_encrypted:
        raise ExternalNormalizationError("encrypted PDFs are unsupported")
    if len(reader.pages) > max_pages:
        raise ExternalNormalizationError("PDF exceeds the configured page limit")
    pages: list[str] = []
    character_count = 0
    for page in reader.pages:
        try:
            value = page.extract_text()
        except Exception as exc:
            raise ExternalNormalizationError("PDF text extraction failed") from exc
        if value:
            character_count += len(value)
            if character_count > max_characters:
                raise ExternalNormalizationError(
                    "PDF exceeds the configured text limit"
                )
            pages.append(value)
    normalized = _clean_text("\n\n".join(pages))
    if not normalized:
        raise ExternalNormalizationError("PDF contains no extractable text")
    if len(normalized) > max_characters:
        raise ExternalNormalizationError("PDF exceeds the configured text limit")
    return normalized


def resolve_explicit_scope(
    text: str,
    *,
    approved_tickers: Iterable[str],
    ticker_aliases: Mapping[str, Sequence[str]] = DEFAULT_TICKER_ALIASES,
    sector_aliases: Mapping[str, Sequence[str]] = DEFAULT_SECTOR_ALIASES,
    global_aliases: Sequence[str] = DEFAULT_GLOBAL_ALIASES,
) -> ResolvedScope:
    approved = {value.upper() for value in approved_tickers}
    candidates: list[ScopeSpan] = []
    for ticker, aliases in sorted(ticker_aliases.items()):
        ticker = ticker.upper()
        if ticker not in approved:
            continue
        span = _first_alias_span(text, aliases, kind="TICKER", value=ticker)
        if span is not None:
            candidates.append(span)
    for sector, aliases in sorted(sector_aliases.items()):
        span = _first_alias_span(text, aliases, kind="SECTOR", value=sector.upper())
        if span is not None:
            candidates.append(span)
    global_span = _first_alias_span(text, global_aliases, kind="GLOBAL", value="GLOBAL")
    if global_span is not None:
        candidates.append(global_span)
    candidates.sort(key=lambda value: (value.start, value.kind, value.value, value.alias.lower()))
    return ResolvedScope(
        tickers=tuple(sorted({value.value for value in candidates if value.kind == "TICKER"})),
        sectors=tuple(sorted({value.value for value in candidates if value.kind == "SECTOR"})),
        global_relevance=any(value.kind == "GLOBAL" for value in candidates),
        supporting_spans=tuple(candidates),
    )


def union_scope(
    *,
    fixed_tickers: Iterable[str] = (),
    deterministic: ResolvedScope | None = None,
    ai_tickers: Iterable[str] = (),
    ai_sectors: Iterable[str] = (),
    ai_global_relevance: bool = False,
    approved_tickers: Iterable[str],
    approved_sectors: Iterable[str],
) -> ResolvedScope:
    approved_ticker_set = {value.upper() for value in approved_tickers}
    approved_sector_set = {value.upper() for value in approved_sectors}
    deterministic = deterministic or ResolvedScope()
    tickers = {value.upper() for value in (*fixed_tickers, *deterministic.tickers, *ai_tickers)}
    sectors = {value.upper() for value in (*deterministic.sectors, *ai_sectors)}
    if not tickers.issubset(approved_ticker_set):
        raise ExternalNormalizationError("scope contains a ticker outside the approved universe")
    if not sectors.issubset(approved_sector_set):
        raise ExternalNormalizationError("scope contains an unreviewed sector")
    return ResolvedScope(
        tickers=tuple(sorted(tickers)),
        sectors=tuple(sorted(sectors)),
        global_relevance=bool(deterministic.global_relevance or ai_global_relevance),
        supporting_spans=deterministic.supporting_spans,
    )


def build_scope_aware_excerpt(
    text: str,
    *,
    title: str | None,
    scope: ResolvedScope,
    max_characters: int = 12_000,
    earnings: bool = False,
) -> ScopeAwareExcerpt:
    if not isinstance(text, str) or not text:
        raise ExternalNormalizationError("normalized text must be non-empty")
    if max_characters < 1_000:
        raise ExternalNormalizationError("excerpt budget is too small")
    full_hash = sha256(text.encode("utf-8")).hexdigest()
    title_text = "" if not title else " ".join(title.split())
    prefix = f"[TITLE]\n{title_text}\n\n" if title_text else ""
    complete_short_text = prefix + text
    if len(complete_short_text) <= max_characters:
        return ScopeAwareExcerpt(
            text=complete_short_text,
            full_character_count=len(text),
            excerpt_character_count=len(complete_short_text),
            full_hash=full_hash,
            excerpt_hash=sha256(complete_short_text.encode("utf-8")).hexdigest(),
            truncated=False,
            included_spans=scope.supporting_spans,
            omitted_scope_values=(),
        )
    pieces: list[tuple[int, int, str]] = []
    opening_end = min(len(text), 1_500)
    opening = text[:opening_end]
    pieces.append((0, opening_end, "OPENING"))
    for span in scope.supporting_spans:
        pieces.append((max(0, span.start - 240), min(len(text), span.end + 240), f"SCOPE:{span.kind}:{span.value}"))
    if earnings:
        for section_name, pattern in _EARNINGS_SECTION_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                pieces.append((match.start(), min(len(text), match.start() + 900), f"EARNINGS:{section_name}"))
    pieces = _coalesce_pieces(pieces)
    rendered: list[str] = [prefix] if prefix else []
    included_spans: list[ScopeSpan] = []
    included_scope_values: set[tuple[str, str]] = set()
    for start, end, label in pieces:
        marker = f"[SOURCE_SPAN {start}:{end} {label}]\n"
        value = marker + text[start:end].strip() + "\n\n"
        if sum(len(item) for item in rendered) + len(value) > max_characters:
            if label.startswith("SCOPE:"):
                continue
            remaining = max_characters - sum(len(item) for item in rendered)
            if remaining > len(marker) + 40:
                rendered.append(value[:remaining])
            break
        rendered.append(value)
        for span in scope.supporting_spans:
            if start <= span.start and span.end <= end:
                included_scope_values.add((span.kind, span.value))
                included_spans.append(span)
    required = {(value.kind, value.value) for value in scope.supporting_spans}
    omitted = tuple(sorted(f"{kind}:{value}" for kind, value in required - included_scope_values))
    if omitted:
        raise ExternalNormalizationError("scope-aware excerpt cannot include every supporting scope span")
    excerpt = "".join(rendered).strip()
    if len(excerpt) > max_characters:
        excerpt = excerpt[:max_characters]
    return ScopeAwareExcerpt(
        text=excerpt,
        full_character_count=len(text),
        excerpt_character_count=len(excerpt),
        full_hash=full_hash,
        excerpt_hash=sha256(excerpt.encode("utf-8")).hexdigest(),
        truncated=True,
        included_spans=tuple(dict.fromkeys(included_spans)),
        omitted_scope_values=(),
    )


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ExternalNormalizationError("URL must be absolute HTTP(S)")
    if parts.username is not None or parts.password is not None:
        raise ExternalNormalizationError("URL must not contain credentials")
    try:
        parts.port
    except ValueError as exc:
        raise ExternalNormalizationError("URL port is invalid") from exc
    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
        if normalized_key in _SENSITIVE_QUERY_KEYS:
            raise ExternalNormalizationError(
                "URL must not contain credential query parameters"
            )
        if key.lower().startswith("utm_") or key.lower() in {"fbclid", "gclid"}:
            continue
        query.append((key, value))
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def _first_alias_span(text: str, aliases: Sequence[str], *, kind: str, value: str) -> ScopeSpan | None:
    matches: list[ScopeSpan] = []
    for alias in sorted(set(aliases), key=lambda item: (-len(item), item.lower())):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
        match = pattern.search(text)
        if match is not None:
            matches.append(ScopeSpan(kind=kind, value=value, alias=alias, start=match.start(), end=match.end()))
    return min(matches, key=lambda item: (item.start, -len(item.alias), item.alias.lower())) if matches else None


def _coalesce_pieces(values: Sequence[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    ordered = sorted(values, key=lambda item: (item[0], item[1], item[2]))
    result: list[tuple[int, int, str]] = []
    for start, end, label in ordered:
        if result and start <= result[-1][1]:
            previous = result.pop()
            labels = sorted(set(previous[2].split("+") + label.split("+")))
            result.append((previous[0], max(previous[1], end), "+".join(labels)))
        else:
            result.append((start, end, label))
    return result


def _safe_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def _clean_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    result: list[str] = []
    blank = False
    for line in lines:
        if line:
            result.append(line)
            blank = False
        elif result and not blank:
            result.append("")
            blank = True
    return "\n".join(result).strip()


__all__ = [
    "DEFAULT_GLOBAL_ALIASES",
    "DEFAULT_SECTOR_ALIASES",
    "DEFAULT_TICKER_ALIASES",
    "EXCERPT_VERSION",
    "ExternalNormalizationError",
    "HTML_NORMALIZER_VERSION",
    "PDF_EXTRACTOR_VERSION",
    "ResolvedScope",
    "SCOPE_RESOLVER_VERSION",
    "ScopeAwareExcerpt",
    "ScopeSpan",
    "build_scope_aware_excerpt",
    "canonicalize_url",
    "extract_article_html",
    "extract_pdf_text",
    "normalize_html_fragment",
    "resolve_explicit_scope",
    "union_scope",
]
