"""Bounded, research-only SEC EDGAR ingestion for the approved issuers.

The module owns SEC discovery, immutable source archiving, durable SEC-native
classification suppression, bounded 8-K extraction, and deterministic Form 4
facts. It never updates risk, model, order, or execution state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
import html
import json
import logging
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Protocol
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

from market_relay_engine.ai_context import ContextClassifier
from market_relay_engine.ai_context.settings import AIContextFilterSettings
from market_relay_engine.common.config import ConfigValidationError, load_yaml_config
from market_relay_engine.common.serialization import to_json_dict
from market_relay_engine.common.time import utc_now
from market_relay_engine.context.sec_edgar_archive import SECArchiveError, SECEDGARArchive
from market_relay_engine.contracts.context import (
    ContextClassificationRequest,
    ContextClassificationStatus,
    ContextRawInput,
    ContextSourceDocument,
    DeterministicContextEventType,
)
from market_relay_engine.questdb.jsonl_fallback import (
    EmergencyJSONLLedgerFallback,
    EmergencyLedgerFallbackError,
)
from market_relay_engine.questdb.writer import context_classification_attempt_to_row


LOGGER = logging.getLogger(__name__)
SEC_SOURCE = "sec_edgar"
SUPPORTED_FORMS = frozenset({"8-K", "8-K/A", "4", "4/A"})
MAX_SEC_REQUEST_RATE_PER_SECOND = 8.0
EIGHT_K_EXTRACTION_VERSION = "SEC_8K_ITEMS_V2"
EIGHT_K_TRUNCATION_POLICY = "HEAD_V1"

_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_EDGAR_ACCEPTANCE_TIMEZONE = ZoneInfo("America/New_York")
_ITEM_RE = re.compile(
    r"(?im)^[ \t]*item[ \t]+([1-8]\.\d{2}|9\.01)[ \t]*[.:-]?[ \t]*(.*)$"
)
_RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})
_LEDGER_TIMESTAMP_FIELDS = frozenset(
    {
        "requested_at",
        "write_time",
        "source_published_at",
        "source_updated_at",
        "collected_at",
        "normalized_at",
        "classified_at",
        "validated_at",
    }
)
_RELEVANT_8K_ITEMS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate Financial Obligations",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Delisting or Failure to Satisfy Listing Rule",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
}


class SECEDGARConfigurationError(ValueError):
    """Raised when SEC contact configuration is unavailable or invalid."""


class SECEDGARHTTPError(RuntimeError):
    """Raised for a bounded SEC transport or response failure."""


class SECEDGARFairAccessError(SECEDGARHTTPError):
    """Raised when a potential SEC fair-access 403 must stop the run."""


class SECMappingDriftError(RuntimeError):
    """Raised when live SEC issuer identity disagrees with reviewed config."""


@dataclass(frozen=True, kw_only=True)
class SECIssuer:
    ticker: str
    issuer_name: str
    cik: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z]{1,10}", self.ticker):
            raise ConfigValidationError("SEC ticker must be uppercase letters")
        if not isinstance(self.issuer_name, str) or not self.issuer_name.strip():
            raise ConfigValidationError("SEC issuer_name must be non-empty")
        if not re.fullmatch(r"\d{10}", self.cik):
            raise ConfigValidationError("SEC CIK must be a zero-padded 10-digit string")


@dataclass(frozen=True, kw_only=True)
class SECEDGARSettings:
    enabled: bool
    organization_env: str
    contact_email_env: str
    timeout_seconds: float
    max_retries: int
    retry_base_delay_seconds: float
    retry_max_delay_seconds: float
    request_rate_per_second: float
    archive_path: Path
    direct_trade_authority: bool

    @classmethod
    def from_repository_config(
        cls, config: Mapping[str, Any], *, base_dir: Path
    ) -> "SECEDGARSettings":
        try:
            source = config["unstructured_sources"]["sec_edgar"]
        except (KeyError, TypeError) as exc:
            raise ConfigValidationError(
                "context_sources sec_edgar configuration is required"
            ) from exc
        if not isinstance(source, Mapping):
            raise ConfigValidationError("sec_edgar configuration must be a mapping")
        archive_path = source.get("archive_path")
        if not isinstance(archive_path, str) or not archive_path.strip():
            raise ConfigValidationError("sec_edgar.archive_path must be non-empty")
        root = base_dir.resolve()
        resolved = (root / archive_path).resolve()
        if root not in resolved.parents:
            raise ConfigValidationError(
                "sec_edgar.archive_path must remain inside repository"
            )
        settings = cls(
            enabled=_bool(source, "enabled"),
            organization_env=_string(source, "organization_env"),
            contact_email_env=_string(source, "contact_email_env"),
            timeout_seconds=_positive_float(source, "timeout_seconds"),
            max_retries=_non_negative_int(source, "max_retries"),
            retry_base_delay_seconds=_positive_float(
                source, "retry_base_delay_seconds"
            ),
            retry_max_delay_seconds=_positive_float(
                source, "retry_max_delay_seconds"
            ),
            request_rate_per_second=_positive_float(
                source, "request_rate_per_second"
            ),
            archive_path=resolved,
            direct_trade_authority=_bool(source, "direct_trade_authority"),
        )
        if settings.max_retries > 2:
            raise ConfigValidationError("sec_edgar.max_retries must not exceed 2")
        if settings.retry_max_delay_seconds < settings.retry_base_delay_seconds:
            raise ConfigValidationError(
                "sec_edgar.retry_max_delay_seconds must be at least the base delay"
            )
        if settings.request_rate_per_second > MAX_SEC_REQUEST_RATE_PER_SECOND:
            raise ConfigValidationError(
                "sec_edgar.request_rate_per_second must not exceed 8"
            )
        if settings.direct_trade_authority:
            raise ConfigValidationError("sec_edgar.direct_trade_authority must be false")
        return settings

    def user_agent(self, environment: Mapping[str, str | None]) -> str:
        organization = (environment.get(self.organization_env) or "").strip()
        email = (environment.get(self.contact_email_env) or "").strip()
        if not organization or not email:
            raise SECEDGARConfigurationError(
                f"SEC contact configuration requires {self.organization_env} "
                f"and {self.contact_email_env}"
            )
        if "@" not in email or any(value in organization + email for value in "\r\n"):
            raise SECEDGARConfigurationError("SEC contact identification is invalid")
        return f"{organization} {email}"


@dataclass(frozen=True, kw_only=True)
class SECFiling:
    ticker: str
    issuer_cik: str
    accession_number: str
    form_type: str
    filing_date: date
    primary_document: str
    filing_url: str
    collected_at: datetime
    acceptance_at: datetime | None = None
    amendment_of: str | None = None


@dataclass(frozen=True, kw_only=True)
class EightKSection:
    item_number: str
    item_title: str
    text: str


@dataclass(frozen=True, kw_only=True)
class PreparedEightKSection:
    item_number: str
    item_title: str
    full_text: str
    full_character_count: int
    full_section_hash: str
    excerpt: str
    excerpt_character_count: int
    excerpt_hash: str
    input_truncated: bool
    truncation_policy: str = EIGHT_K_TRUNCATION_POLICY
    extraction_version: str = EIGHT_K_EXTRACTION_VERSION


@dataclass(frozen=True, kw_only=True)
class Form4Transaction:
    security_kind: str
    transaction_code: str | None
    security_title: str | None
    transaction_date: date | None
    shares: float | None
    price_per_share: float | None
    acquired_disposed_code: str | None
    direct_or_indirect: str | None
    shares_owned_following: float | None
    exercise_price: float | None
    expiration_date: date | None
    underlying_security_title: str | None
    underlying_shares: float | None
    plan_10b5_1: bool | None
    promoted_event_type: DeterministicContextEventType | None
    aggregate_eligibility: str


@dataclass(frozen=True, kw_only=True)
class Form4ReportingOwner:
    cik: str | None
    name: str | None
    roles: tuple[str, ...]
    officer_title: str | None
    other_relationship_text: str | None


@dataclass(frozen=True, kw_only=True)
class Form4ResearchEvent:
    event_type: DeterministicContextEventType
    issuer_ticker: str
    issuer_cik: str
    accession_number: str
    reporting_owners: tuple[Form4ReportingOwner, ...]
    transaction_date: date | None
    available_at: datetime | None
    transaction_code: str
    shares: float | None
    price_per_share: float | None
    approximate_value: float | None
    direct_or_indirect: str | None
    shares_owned_following: float | None
    is_amendment: bool
    amends_accession: str | None
    aggregate_eligibility: str
    plan_10b5_1: bool | None


@dataclass(frozen=True, kw_only=True)
class ParsedForm4:
    issuer_ticker: str
    issuer_cik: str
    reporting_owners: tuple[Form4ReportingOwner, ...]
    transactions: tuple[Form4Transaction, ...]
    promoted_events: tuple[Form4ResearchEvent, ...]
    is_amendment: bool
    amends_accession: str | None


class SECTransport(Protocol):
    def get_json(self, url: str) -> Mapping[str, Any]: ...

    def get_bytes(self, url: str) -> bytes: ...


class SECEDGARHTTPClient:
    """Sequential, contact-identified HTTP client with bounded retries."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        max_retries: int,
        request_rate_per_second: float,
        retry_base_delay_seconds: float,
        retry_max_delay_seconds: float,
        session: requests.Session | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_rate_per_second <= 0 or request_rate_per_second > 8:
            raise SECEDGARConfigurationError(
                "SEC request rate must be positive and must not exceed 8"
            )
        self._session = session or requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        )
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay_seconds
        self._retry_max_delay = retry_max_delay_seconds
        self._minimum_interval = 1.0 / request_rate_per_second
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None

    def get_json(self, url: str) -> Mapping[str, Any]:
        payload = self._request(url)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SECEDGARHTTPError("SEC returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise SECEDGARHTTPError("SEC returned a non-object JSON payload")
        return value

    def get_bytes(self, url: str) -> bytes:
        return self._request(url)

    def _request(self, url: str) -> bytes:
        for attempt in range(self._max_retries + 1):
            self._pace_request()
            try:
                response = self._session.get(url, timeout=self._timeout_seconds)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self._max_retries:
                    raise SECEDGARHTTPError("SEC transport request failed") from exc
                self._sleep_retry(self._exponential_delay(attempt))
                continue

            status = response.status_code
            if status == 403:
                raise SECEDGARFairAccessError(
                    "SEC returned HTTP 403; stopping to protect fair access"
                )
            if status == 429:
                if attempt >= self._max_retries:
                    raise SECEDGARHTTPError("SEC returned HTTP 429")
                retry_after = self._retry_after_seconds(response.headers)
                self._sleep_retry(
                    retry_after
                    if retry_after is not None
                    else self._exponential_delay(attempt)
                )
                continue
            if status in _RETRYABLE_SERVER_STATUSES:
                if attempt >= self._max_retries:
                    raise SECEDGARHTTPError(f"SEC returned HTTP {status}")
                self._sleep_retry(self._exponential_delay(attempt))
                continue
            if status >= 400:
                raise SECEDGARHTTPError(f"SEC returned HTTP {status}")
            return response.content
        raise SECEDGARHTTPError("SEC request retry budget exhausted")

    def _pace_request(self) -> None:
        now = self._monotonic_clock()
        if self._last_request_at is not None:
            remaining = self._minimum_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_at = self._monotonic_clock()

    def _sleep_retry(self, delay: float) -> None:
        if delay > self._retry_max_delay:
            raise SECEDGARHTTPError(
                "SEC retry delay exceeds the configured bounded maximum"
            )
        if delay > 0:
            self._sleeper(delay)

    def _exponential_delay(self, attempt: int) -> float:
        return min(
            self._retry_base_delay * (2**attempt), self._retry_max_delay
        )

    def _retry_after_seconds(self, headers: Mapping[str, Any]) -> float | None:
        value = headers.get("Retry-After")
        if value is None:
            return None
        text = str(value).strip()
        try:
            return max(float(text), 0.0)
        except ValueError:
            try:
                target = parsedate_to_datetime(text)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                return max((target.astimezone(UTC) - self._wall_clock()).total_seconds(), 0)
            except (TypeError, ValueError, OverflowError):
                return None


def load_sec_issuers(*, base_dir: Path) -> tuple[SECIssuer, ...]:
    config = load_yaml_config("sec_edgar_tickers", base_dir=base_dir)
    values = config.get("issuers")
    if not isinstance(values, list):
        raise ConfigValidationError("sec_edgar_tickers.yaml requires issuers list")
    if not all(isinstance(item, Mapping) for item in values):
        raise ConfigValidationError("SEC issuers must be mappings")
    issuers = tuple(
        SECIssuer(
            ticker=_string(item, "ticker"),
            issuer_name=_string(item, "issuer_name"),
            cik=_string(item, "cik"),
        )
        for item in values
    )
    if (
        len(issuers) != 10
        or len({issuer.ticker for issuer in issuers}) != 10
        or len({issuer.cik for issuer in issuers}) != 10
    ):
        raise ConfigValidationError(
            "SEC issuer mapping must contain ten unique ticker/CIK pairs"
        )
    approved = {"PLTR", "LMT", "RTX", "GD", "AVAV", "XOM", "OXY", "SLB", "COP", "VLO"}
    if {issuer.ticker for issuer in issuers} != approved:
        raise ConfigValidationError(
            "SEC issuer mapping must exactly match approved universe"
        )
    return issuers


def normalize_accession_number(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("SEC accession number must be a string")
    raw = value.strip()
    if not re.fullmatch(r"(?:\d{18}|\d{10}-\d{2}-\d{6})", raw):
        raise ValueError("SEC accession number must contain 18 digits")
    compact = raw.replace("-", "")
    normalized = f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    if _ACCESSION_RE.fullmatch(normalized) is None:
        raise ValueError("SEC accession number is invalid")
    return normalized


def discover_filings(
    client: SECTransport,
    issuer: SECIssuer,
    *,
    forms: Iterable[str] = SUPPORTED_FORMS,
    start_date: date | None = None,
    end_date: date | None = None,
    collected_at: datetime | None = None,
) -> tuple[SECFiling, ...]:
    allowed = set(forms).intersection(SUPPORTED_FORMS)
    if not allowed:
        return ()
    payload = client.get_json(
        f"https://data.sec.gov/submissions/CIK{issuer.cik}.json"
    )
    _validate_submission_identity(payload, issuer)
    recent = (
        payload.get("filings", {}).get("recent", {})
        if isinstance(payload.get("filings"), Mapping)
        else {}
    )
    if not isinstance(recent, Mapping):
        raise SECEDGARHTTPError("SEC submissions recent filings are invalid")
    rows = _column_rows(recent)
    now = collected_at or utc_now()
    filings: list[SECFiling] = []
    for row in rows:
        form = str(row.get("form", "")).strip()
        if form not in allowed:
            continue
        filed = _parse_date(row.get("filingDate"))
        if filed is None:
            continue
        if start_date is not None and filed < start_date:
            continue
        if end_date is not None and filed > end_date:
            continue
        primary_document = str(row.get("primaryDocument", "")).strip()
        if not primary_document:
            continue
        accession = normalize_accession_number(str(row.get("accessionNumber", "")))
        filings.append(
            SECFiling(
                ticker=issuer.ticker,
                issuer_cik=issuer.cik,
                accession_number=accession,
                form_type=form,
                filing_date=filed,
                primary_document=primary_document,
                filing_url=filing_document_url(
                    issuer.cik, accession, primary_document
                ),
                collected_at=now,
                acceptance_at=_parse_timestamp(row.get("acceptanceDateTime")),
                amendment_of=None,
            )
        )
    return tuple(filings)


def filing_document_url(
    cik: str, accession_number: str, primary_document: str
) -> str:
    identity = _safe_sec_document_identity(primary_document, allow_renderer_path=True)
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession_number.replace('-', '')}/{identity}"
    )


def filing_index_url(filing: SECFiling) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(filing.issuer_cik)}/"
        f"{filing.accession_number.replace('-', '')}/index.json"
    )


def resolve_form4_xml_document(
    client: SECTransport, filing: SECFiling
) -> tuple[str, str, bytes]:
    """Return official XML identity, URL, and bytes for a Form 4 filing."""
    primary = _safe_sec_document_identity(
        filing.primary_document, allow_renderer_path=True
    )
    if primary.lower().endswith(".xml") and "/" not in primary:
        return primary, filing.filing_url, client.get_bytes(filing.filing_url)

    index = client.get_json(filing_index_url(filing))
    directory = index.get("directory")
    items = directory.get("item") if isinstance(directory, Mapping) else None
    if not isinstance(items, list):
        raise SECEDGARHTTPError("SEC filing index does not contain document items")
    candidates: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.lower().endswith(".xml"):
            continue
        safe_name = _safe_sec_document_identity(name, allow_renderer_path=False)
        lower = safe_name.lower()
        if any(
            marker in lower
            for marker in (
                "_cal.xml",
                "_def.xml",
                "_lab.xml",
                "_pre.xml",
                "filingsummary.xml",
            )
        ):
            continue
        candidates.append(safe_name)

    primary_name = PurePosixPath(primary).name
    if primary_name in candidates:
        selected = primary_name
    else:
        preferred = [
            name
            for name in candidates
            if "ownership" in name.lower() or "form4" in name.lower()
        ]
        if len(preferred) == 1:
            selected = preferred[0]
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            raise SECEDGARHTTPError(
                "SEC filing index does not identify one official Form 4 XML document"
            )
    url = filing_document_url(
        filing.issuer_cik, filing.accession_number, selected
    )
    return selected, url, client.get_bytes(url)


def extract_relevant_8k_sections(document: bytes) -> tuple[EightKSection, ...]:
    """Extract complete normalized relevant sections; never truncate here."""
    text = _normalize_document(document)
    matches = list(_ITEM_RE.finditer(text))
    selected: dict[str, tuple[int, EightKSection]] = {}
    for index, match in enumerate(matches):
        item_number = match.group(1)
        if item_number not in _RELEVANT_8K_ITEMS:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = re.sub(r"\s+", " ", text[match.start() : end]).strip()
        if not body:
            continue
        candidate = EightKSection(
            item_number=item_number,
            item_title=_RELEVANT_8K_ITEMS[item_number],
            text=body,
        )
        existing = selected.get(item_number)
        if existing is None or len(candidate.text) >= len(existing[1].text):
            selected[item_number] = (match.start(), candidate)
    return tuple(section for _, section in sorted(selected.values()))


def prepare_8k_section(
    section: EightKSection, *, max_input_characters: int
) -> PreparedEightKSection:
    if max_input_characters <= 0:
        raise ValueError("max_input_characters must be positive")
    full_hash = _text_hash(section.text)
    excerpt = section.text[:max_input_characters]
    return PreparedEightKSection(
        item_number=section.item_number,
        item_title=section.item_title,
        full_text=section.text,
        full_character_count=len(section.text),
        full_section_hash=full_hash,
        excerpt=excerpt,
        excerpt_character_count=len(excerpt),
        excerpt_hash=_text_hash(excerpt),
        input_truncated=len(excerpt) != len(section.text),
    )


def _parse_form4_reporting_owner(element: ET.Element) -> Form4ReportingOwner:
    return Form4ReportingOwner(
        cik=_xml_text(element, "rptOwnerCik"),
        name=_xml_text(element, "rptOwnerName"),
        roles=tuple(
            role
            for flag, role in (
                ("isDirector", "DIRECTOR"),
                ("isOfficer", "OFFICER"),
                ("isTenPercentOwner", "TEN_PERCENT_OWNER"),
                ("isOther", "OTHER"),
            )
            if _xml_text(element, flag) == "1"
        ),
        officer_title=_xml_text(element, "officerTitle"),
        other_relationship_text=_xml_text(element, "otherText"),
    )


def parse_form4(document: bytes, filing: SECFiling) -> ParsedForm4:
    """Parse official Form 4 XML without semantic inference or Gemini."""
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise ValueError("Form 4 XML is invalid") from exc
    ticker = _xml_text(root, "issuerTradingSymbol") or filing.ticker
    cik = (_xml_text(root, "issuerCik") or filing.issuer_cik).zfill(10)
    if ticker.upper() != filing.ticker or cik != filing.issuer_cik:
        raise SECMappingDriftError(
            f"SEC Form 4 issuer identity drift for {filing.ticker}; manual review required"
        )
    reporting_owners = tuple(
        _parse_form4_reporting_owner(element)
        for element in root
        if element.tag.rsplit("}", 1)[-1] == "reportingOwner"
    )
    is_amendment = filing.form_type == "4/A"
    if not is_amendment:
        aggregate_eligibility = "ELIGIBLE"
    elif filing.amendment_of is None:
        aggregate_eligibility = "AMENDMENT_UNRESOLVED"
    else:
        aggregate_eligibility = "AMENDMENT_RESOLVED"

    transactions: list[Form4Transaction] = []
    events: list[Form4ResearchEvent] = []
    for element_name, security_kind in (
        ("nonDerivativeTransaction", "NON_DERIVATIVE"),
        ("derivativeTransaction", "DERIVATIVE"),
    ):
        for transaction in _xml_elements(root, element_name):
            code = _xml_text(transaction, "transactionCode")
            shares = _xml_number(transaction, "transactionShares")
            price = _xml_number(transaction, "transactionPricePerShare")
            promoted_type: DeterministicContextEventType | None = None
            if security_kind == "NON_DERIVATIVE" and code == "P":
                promoted_type = (
                    DeterministicContextEventType.SEC_FORM4_OPEN_MARKET_PURCHASE
                )
            elif security_kind == "NON_DERIVATIVE" and code == "S":
                promoted_type = DeterministicContextEventType.SEC_FORM4_OPEN_MARKET_SALE
            parsed = Form4Transaction(
                security_kind=security_kind,
                transaction_code=code,
                security_title=_xml_text(transaction, "securityTitle"),
                transaction_date=_parse_date(_xml_text(transaction, "transactionDate")),
                shares=shares,
                price_per_share=price,
                acquired_disposed_code=_xml_text(
                    transaction, "transactionAcquiredDisposedCode"
                ),
                direct_or_indirect=_xml_text(
                    transaction, "directOrIndirectOwnership"
                ),
                shares_owned_following=_xml_number(
                    transaction, "sharesOwnedFollowingTransaction"
                ),
                exercise_price=_xml_number(transaction, "conversionOrExercisePrice"),
                expiration_date=_parse_date(_xml_text(transaction, "expirationDate")),
                underlying_security_title=_xml_text(
                    transaction, "underlyingSecurityTitle"
                ),
                underlying_shares=_xml_number(
                    transaction, "underlyingSecurityShares"
                ),
                plan_10b5_1=_xml_plan_indicator(transaction),
                promoted_event_type=promoted_type,
                aggregate_eligibility=aggregate_eligibility,
            )
            transactions.append(parsed)
            if promoted_type is not None:
                events.append(
                    Form4ResearchEvent(
                        event_type=promoted_type,
                        issuer_ticker=ticker,
                        issuer_cik=cik,
                        accession_number=filing.accession_number,
                        reporting_owners=reporting_owners,
                        transaction_date=parsed.transaction_date,
                        available_at=filing.acceptance_at or filing.collected_at,
                        transaction_code=code or "",
                        shares=shares,
                        price_per_share=price,
                        approximate_value=(
                            None if shares is None or price is None else shares * price
                        ),
                        direct_or_indirect=parsed.direct_or_indirect,
                        shares_owned_following=parsed.shares_owned_following,
                        is_amendment=is_amendment,
                        amends_accession=filing.amendment_of,
                        aggregate_eligibility=aggregate_eligibility,
                        plan_10b5_1=parsed.plan_10b5_1,
                    )
                )
    return ParsedForm4(
        issuer_ticker=ticker,
        issuer_cik=cik,
        reporting_owners=reporting_owners,
        transactions=tuple(transactions),
        promoted_events=tuple(events),
        is_amendment=is_amendment,
        amends_accession=filing.amendment_of,
    )


def default_aggregate_form4_events(
    events: Iterable[Form4ResearchEvent],
) -> tuple[Form4ResearchEvent, ...]:
    return tuple(
        event for event in events if event.aggregate_eligibility == "ELIGIBLE"
    )


class SECEDGARCollector:
    """Explicit collector with local research and optional ledger side effects."""

    def __init__(
        self,
        *,
        settings: SECEDGARSettings,
        issuers: Sequence[SECIssuer],
        client: SECTransport,
        archive: SECEDGARArchive,
        classifier: ContextClassifier | None = None,
        ai_settings: AIContextFilterSettings | None = None,
        ledger_writer: Any | None = None,
        fallback: EmergencyJSONLLedgerFallback | None = None,
    ) -> None:
        self.settings = settings
        self.issuers = {issuer.ticker: issuer for issuer in issuers}
        self.client = client
        self.archive = archive
        self.classifier = classifier
        self.ai_settings = ai_settings
        self.ledger_writer = ledger_writer
        self.fallback = fallback

    def collect(
        self,
        *,
        tickers: Sequence[str] | None = None,
        forms: Iterable[str] = SUPPORTED_FORMS,
        start_date: date | None = None,
        end_date: date | None = None,
        max_filings: int = 10,
        dry_run: bool = False,
        write_questdb: bool = False,
    ) -> dict[str, int]:
        if max_filings <= 0:
            raise ValueError("max_filings must be positive")
        selected = tuple(tickers or tuple(self.issuers))
        if set(selected).difference(self.issuers):
            raise ValueError("SEC collector requested an unapproved ticker")
        requested_forms = tuple(set(forms).intersection(SUPPORTED_FORMS))
        manifest = self.archive.load_manifest()
        counts = {
            "discovered": 0,
            "archived": 0,
            "classifications": 0,
            "persistent_suppressions": 0,
            "ledger_retries": 0,
            "form4_events": 0,
            "mapping_drift": 0,
        }
        if write_questdb and not dry_run:
            counts["ledger_retries"] += self._retry_pending_ledger(manifest)
        for ticker in selected:
            try:
                filings = discover_filings(
                    self.client,
                    self.issuers[ticker],
                    forms=requested_forms,
                    start_date=start_date,
                    end_date=end_date,
                )
            except SECMappingDriftError:
                counts["mapping_drift"] += 1
                continue
            for filing in filings:
                if counts["discovered"] >= max_filings:
                    return counts
                counts["discovered"] += 1
                if dry_run:
                    continue
                content, document_hash, state, was_archived = self._load_or_archive(
                    filing, manifest
                )
                effective_filing = _filing_with_archived_collected_at(filing, state)
                counts["archived"] += int(was_archived)
                if effective_filing.form_type.startswith("8-K"):
                    counts_for_filing = self._process_8k(
                        effective_filing,
                        document_hash,
                        content,
                        state,
                        manifest,
                        write_questdb,
                    )
                    for name, value in counts_for_filing.items():
                        counts[name] += value
                else:
                    counts["form4_events"] += self._process_form4(
                        effective_filing, document_hash, content, state, manifest
                    )
        return counts

    def _load_or_archive(
        self,
        filing: SECFiling,
        manifest: dict[str, Any],
    ) -> tuple[bytes, str, dict[str, Any], bool]:
        filings = manifest.setdefault("filings", {})
        state = filings.get(filing.accession_number)
        if state is not None:
            if (
                state.get("primary_document") != filing.primary_document
                or state.get("form_type") != filing.form_type
            ):
                raise SECArchiveError(
                    "SEC accession identity conflicts with archived filing metadata"
                )
            document_hash = str(state["document_hash"])
            extension = str(state["document_extension"])
            return (
                self.archive.read_document(document_hash, extension=extension),
                document_hash,
                state,
                False,
            )

        archived_metadata = self.archive.read_filing_metadata(
            filing.accession_number
        )
        if archived_metadata is not None:
            stable_identity = {
                "accession_number": filing.accession_number,
                "ticker": filing.ticker,
                "issuer_cik": filing.issuer_cik,
                "form_type": filing.form_type,
                "filing_date": filing.filing_date.isoformat(),
                "primary_document": filing.primary_document,
                "filing_url": filing.filing_url,
            }
            if any(
                archived_metadata.get(name) != expected
                for name, expected in stable_identity.items()
            ):
                raise SECArchiveError(
                    "SEC accession identity conflicts with immutable filing metadata"
                )
            durable_fields = (
                "document_hash",
                "official_document_identity",
                "official_document_url",
                "collected_at",
            )
            if any(
                not isinstance(archived_metadata.get(name), str)
                or not archived_metadata[name].strip()
                for name in durable_fields
            ):
                raise SECArchiveError("SEC immutable filing metadata is incomplete")
            document_hash = archived_metadata["document_hash"]
            official_url = archived_metadata["official_document_url"]
            extension = Path(urlparse(official_url).path).suffix or ".bin"
            content = self.archive.read_document(
                document_hash, extension=extension
            )
            state = {
                "form_type": archived_metadata["form_type"],
                "primary_document": archived_metadata["primary_document"],
                "official_document_identity": archived_metadata[
                    "official_document_identity"
                ],
                "official_document_url": official_url,
                "document_hash": document_hash,
                "document_extension": extension,
                "collected_at": archived_metadata["collected_at"],
                "classifications": {},
            }
            filings[filing.accession_number] = state
            self.archive.save_manifest(manifest)
            return content, document_hash, state, False

        if filing.form_type in {"4", "4/A"}:
            official_identity, official_url, content = resolve_form4_xml_document(
                self.client, filing
            )
        else:
            official_identity = filing.primary_document
            official_url = filing.filing_url
            content = self.client.get_bytes(official_url)
        extension = Path(urlparse(official_url).path).suffix or ".bin"
        document_hash = self.archive.archive_document(content, extension=extension)
        metadata = _filing_metadata(
            filing,
            document_hash,
            official_document_identity=official_identity,
            official_document_url=official_url,
        )
        self.archive.write_filing_once(filing.accession_number, metadata)
        state = {
            "form_type": filing.form_type,
            "primary_document": filing.primary_document,
            "official_document_identity": official_identity,
            "official_document_url": official_url,
            "document_hash": document_hash,
            "document_extension": extension,
            "collected_at": metadata["collected_at"],
            "classifications": {},
        }
        filings[filing.accession_number] = state
        self.archive.save_manifest(manifest)
        return content, document_hash, state, True

    def _process_8k(
        self,
        filing: SECFiling,
        document_hash: str,
        content: bytes,
        state: dict[str, Any],
        manifest: dict[str, Any],
        write_questdb: bool,
    ) -> dict[str, int]:
        self.archive.archive_normalized_text(
            document_hash, _normalize_document(content)
        )
        counts = {
            "classifications": 0,
            "persistent_suppressions": 0,
            "ledger_retries": 0,
        }
        for section in extract_relevant_8k_sections(content):
            full_hash = _text_hash(section.text)
            self.archive.archive_normalized_section(
                document_hash,
                item_number=section.item_number,
                section_hash=full_hash,
                text=section.text,
            )
            if self.classifier is None or self.ai_settings is None:
                continue
            prepared = prepare_8k_section(
                section,
                max_input_characters=self.ai_settings.max_input_characters,
            )
            config_hash = _classification_config_hash(self.ai_settings)
            key = _persistent_classification_key(
                filing,
                official_document_identity=str(state["official_document_identity"]),
                prepared=prepared,
                settings=self.ai_settings,
                config_hash=config_hash,
            )
            classifications = state.setdefault("classifications", {})
            saved = classifications.get(key)
            if isinstance(saved, dict) and saved.get("classification_complete") is True:
                counts["persistent_suppressions"] += 1
                continue

            request = _classification_request(
                filing,
                document_hash,
                prepared,
                self.ai_settings.prompt_version,
            )
            result = self.classifier.classify(request)
            response = result.response
            counts["classifications"] += 1
            if response.status in {
                ContextClassificationStatus.VALID,
                ContextClassificationStatus.ABSTAINED,
            }:
                row = context_classification_attempt_to_row(
                    request,
                    response,
                    validation_result=result.validation_result,
                )
                saved = _durable_classification_result(
                    key=key,
                    filing=filing,
                    document_hash=document_hash,
                    prepared=prepared,
                    response_schema_version=self.ai_settings.response_schema_version,
                    config_hash=config_hash,
                    request=request,
                    response=response,
                    ledger_row=row,
                )
                classifications[key] = saved
                self.archive.save_manifest(manifest)
                if write_questdb:
                    self._write_saved_ledger(
                        saved,
                        request=request,
                        response=response,
                        validation_result=result.validation_result,
                    )
                    self.archive.save_manifest(manifest)
            elif write_questdb:
                self._write_transient_attempt(
                    request, response, result.validation_result
                )
        return counts

    def _process_form4(
        self,
        filing: SECFiling,
        document_hash: str,
        content: bytes,
        state: dict[str, Any],
        manifest: dict[str, Any],
    ) -> int:
        path = self.archive.form4 / f"{filing.accession_number}.json"
        if path.exists():
            return 0
        parsed = parse_form4(content, filing)
        payload = {
            "filing": _filing_metadata(
                filing,
                document_hash,
                official_document_identity=str(state["official_document_identity"]),
                official_document_url=str(state["official_document_url"]),
            ),
            "issuer_ticker": parsed.issuer_ticker,
            "issuer_cik": parsed.issuer_cik,
            "reporting_owners": [
                _form4_reporting_owner_payload(value)
                for value in parsed.reporting_owners
            ],
            "is_amendment": parsed.is_amendment,
            "amends_accession": parsed.amends_accession,
            "normalized_transactions": [
                _form4_transaction_payload(value) for value in parsed.transactions
            ],
            "research_events": [
                _form4_event_payload(value) for value in parsed.promoted_events
            ],
        }
        self.archive.write_form4_once(filing.accession_number, payload)
        return len(parsed.promoted_events)

    def _retry_pending_ledger(self, manifest: dict[str, Any]) -> int:
        """Retry saved safe rows without requiring SEC text or Gemini."""
        retried = 0
        for state in manifest.get("filings", {}).values():
            if not isinstance(state, Mapping):
                continue
            classifications = state.get("classifications", {})
            if not isinstance(classifications, Mapping):
                continue
            for saved in classifications.values():
                if (
                    not isinstance(saved, dict)
                    or saved.get("classification_complete") is not True
                    or saved.get("ledger_write_status") == "QUESTDB_WRITTEN"
                ):
                    continue
                self._write_saved_ledger(saved)
                self.archive.save_manifest(manifest)
                retried += 1
        return retried

    def _write_saved_ledger(
        self,
        saved: dict[str, Any],
        *,
        request: ContextClassificationRequest | None = None,
        response: Any | None = None,
        validation_result: Any | None = None,
    ) -> None:
        row = _restore_ledger_row(saved["ledger_row"])
        try:
            if self.ledger_writer is None:
                raise RuntimeError("QuestDB writer unavailable")
            if request is not None and response is not None:
                self.ledger_writer.write_context_classification_attempt(
                    request, response, validation_result
                )
            else:
                self.ledger_writer.write_row(
                    "context_classification_attempts", row
                )
        except Exception as exc:  # noqa: BLE001 - record only safe failure type.
            saved["ledger_write_status"] = self._append_fallback(
                row, failure_type=type(exc).__name__
            )
        else:
            saved["ledger_write_status"] = "QUESTDB_WRITTEN"

    def _write_transient_attempt(
        self,
        request: ContextClassificationRequest,
        response: Any,
        validation_result: Any | None,
    ) -> None:
        row = context_classification_attempt_to_row(
            request, response, validation_result=validation_result
        )
        try:
            if self.ledger_writer is None:
                raise RuntimeError("QuestDB writer unavailable")
            self.ledger_writer.write_context_classification_attempt(
                request, response, validation_result
            )
        except Exception as exc:  # noqa: BLE001 - safe fallback boundary.
            self._append_fallback(row, failure_type=type(exc).__name__)

    def _append_fallback(
        self, row: Mapping[str, Any], *, failure_type: str
    ) -> str:
        if self.fallback is None:
            raise EmergencyLedgerFallbackError(
                "SEC classification-attempt fallback is unavailable"
            )
        try:
            self.fallback.append_record(
                record_type="context_classification_attempt",
                target_table="context_classification_attempts",
                record_id=str(row["classification_attempt_id"]),
                event_time=row["requested_at"],
                source=SEC_SOURCE,
                ticker_or_sector=json.loads(str(row["affected_tickers_json"]))[0],
                primary_write_failure={
                    "failure_code": "QUESTDB_CONTEXT_CLASSIFICATION_WRITE_FAILED",
                    "failure_type": failure_type,
                    "target_table": "context_classification_attempts",
                },
                payload={
                    "context_classification_attempt": dict(row),
                    "write_request": {"questdb_required": False},
                },
            )
        except Exception:  # noqa: BLE001 - surface only a safe failure category.
            raise EmergencyLedgerFallbackError(
                "SEC classification-attempt fallback write failed"
            ) from None
        return "FALLBACK_WRITTEN_QUESTDB_PENDING"


def _classification_request(
    filing: SECFiling,
    document_hash: str,
    section: PreparedEightKSection,
    prompt_version: str,
) -> ContextClassificationRequest:
    locator = f"{filing.accession_number}:{section.item_number}"
    source_document_key = _stable_hash(
        {
            "accession_number": filing.accession_number,
            "document_hash": document_hash,
            "item_number": section.item_number,
            "full_section_hash": section.full_section_hash,
        }
    )
    raw_input_key = _stable_hash(
        {
            "accession_number": filing.accession_number,
            "document_hash": document_hash,
            "item_number": section.item_number,
            "full_section_hash": section.full_section_hash,
            "excerpt_hash": section.excerpt_hash,
        }
    )
    raw = ContextRawInput(
        raw_input_id=f"raw_input_sec_{raw_input_key[:32]}",
        source=SEC_SOURCE,
        source_type="sec_8k_item_excerpt",
        source_platform="sec_edgar",
        source_uri=filing.filing_url,
        source_locator=locator,
        raw_input_hash=section.excerpt_hash,
        affected_tickers=[filing.ticker],
        source_published_at=filing.acceptance_at,
        collected_at=filing.collected_at,
    )
    document = ContextSourceDocument(
        source_document_id=f"source_document_sec_{source_document_key[:32]}",
        raw_input_id=raw.raw_input_id,
        source=raw.source,
        source_type="sec_8k_item",
        source_platform=raw.source_platform,
        source_uri=raw.source_uri,
        source_locator=raw.source_locator,
        raw_input_hash=raw.raw_input_hash,
        document_hash=document_hash,
        affected_tickers=raw.affected_tickers,
        source_published_at=raw.source_published_at,
        collected_at=raw.collected_at,
        normalized_at=filing.collected_at,
    )
    return ContextClassificationRequest(
        requested_at=utc_now(),
        source=SEC_SOURCE,
        source_type="sec_8k_item",
        source_platform="sec_edgar",
        source_uri=filing.filing_url,
        source_locator=locator,
        raw_input_id=raw.raw_input_id,
        source_document_id=document.source_document_id,
        raw_input_hash=raw.raw_input_hash,
        document_hash=document.document_hash,
        affected_tickers=[filing.ticker],
        input_text=section.excerpt,
        prompt_version=prompt_version,
        source_published_at=filing.acceptance_at,
        collected_at=filing.collected_at,
        normalized_at=filing.collected_at,
    )


def _durable_classification_result(
    *,
    key: str,
    filing: SECFiling,
    document_hash: str,
    prepared: PreparedEightKSection,
    response_schema_version: str,
    config_hash: str,
    request: ContextClassificationRequest,
    response: Any,
    ledger_row: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "persistent_classification_key": key,
        "classification_complete": True,
        "classification_request_id": request.classification_request_id,
        "classification_attempt_id": response.classification_attempt_id,
        "status": response.status.value,
        "event_type": response.event_type.value,
        "risk_level": response.risk_level.value,
        "urgency": response.urgency.value,
        "confidence": response.confidence,
        "summary": response.summary,
        "classified_at": response.classified_at.isoformat(),
        "provider": response.provider,
        "model_version": response.model_version,
        "prompt_version": response.prompt_version,
        "response_schema_version": response_schema_version,
        "classification_config_hash": config_hash,
        "accession_number": filing.accession_number,
        "document_hash": document_hash,
        "full_section_character_count": prepared.full_character_count,
        "full_section_hash": prepared.full_section_hash,
        "excerpt_character_count": prepared.excerpt_character_count,
        "excerpt_hash": prepared.excerpt_hash,
        "input_truncated": prepared.input_truncated,
        "truncation_policy": prepared.truncation_policy,
        "extraction_version": prepared.extraction_version,
        "item_number": prepared.item_number,
        "ledger_write_status": "NOT_REQUESTED",
        "ledger_row": to_json_dict(dict(ledger_row)),
    }


def _persistent_classification_key(
    filing: SECFiling,
    *,
    official_document_identity: str,
    prepared: PreparedEightKSection,
    settings: AIContextFilterSettings,
    config_hash: str,
) -> str:
    return _stable_hash(
        {
            "accession_number": filing.accession_number,
            "official_document_identity": official_document_identity,
            "item_number": prepared.item_number,
            "full_section_hash": prepared.full_section_hash,
            "excerpt_hash": prepared.excerpt_hash,
            "extraction_version": prepared.extraction_version,
            "prompt_version": settings.prompt_version,
            "model_version": settings.model,
            "response_schema_version": settings.response_schema_version,
            "classification_config_hash": config_hash,
        }
    )


def _classification_config_hash(settings: AIContextFilterSettings) -> str:
    return _stable_hash(
        {
            "max_input_characters": settings.max_input_characters,
            "max_prompt_characters": settings.max_prompt_characters,
            "max_summary_characters": settings.max_summary_characters,
            "max_output_tokens": settings.max_output_tokens,
            "temperature": settings.temperature,
        }
    )


def _restore_ledger_row(value: Mapping[str, Any]) -> dict[str, Any]:
    restored = dict(value)
    for name in _LEDGER_TIMESTAMP_FIELDS:
        raw = restored.get(name)
        if isinstance(raw, str):
            restored[name] = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return restored


def _filing_metadata(
    filing: SECFiling,
    document_hash: str,
    *,
    official_document_identity: str,
    official_document_url: str,
) -> dict[str, Any]:
    return {
        "ticker": filing.ticker,
        "issuer_cik": filing.issuer_cik,
        "accession_number": filing.accession_number,
        "form_type": filing.form_type,
        "filing_date": filing.filing_date.isoformat(),
        "acceptance_at": _iso(filing.acceptance_at),
        "primary_document": filing.primary_document,
        "filing_url": filing.filing_url,
        "official_document_identity": official_document_identity,
        "official_document_url": official_document_url,
        "amendment_of": filing.amendment_of,
        "collected_at": _iso(filing.collected_at),
        "document_hash": document_hash,
    }


def _filing_with_archived_collected_at(
    filing: SECFiling, state: Mapping[str, Any]
) -> SECFiling:
    raw = state.get("collected_at")
    if not isinstance(raw, str) or not raw.strip():
        raise SECArchiveError("SEC archived collected_at is missing")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SECArchiveError(
                "SEC archived collected_at must be timezone-aware"
            )
        collected_at = parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SECArchiveError(
            "SEC archived collected_at cannot be normalized to UTC"
        ) from exc
    return replace(filing, collected_at=collected_at)


def _form4_reporting_owner_payload(value: Form4ReportingOwner) -> dict[str, Any]:
    return {
        "cik": value.cik,
        "name": value.name,
        "roles": list(value.roles),
        "officer_title": value.officer_title,
        "other_relationship_text": value.other_relationship_text,
    }


def _form4_transaction_payload(value: Form4Transaction) -> dict[str, Any]:
    return {
        "security_kind": value.security_kind,
        "transaction_code": value.transaction_code,
        "security_title": value.security_title,
        "transaction_date": _date_iso(value.transaction_date),
        "shares": value.shares,
        "price_per_share": value.price_per_share,
        "acquired_disposed_code": value.acquired_disposed_code,
        "direct_or_indirect": value.direct_or_indirect,
        "shares_owned_following": value.shares_owned_following,
        "exercise_price": value.exercise_price,
        "expiration_date": _date_iso(value.expiration_date),
        "underlying_security_title": value.underlying_security_title,
        "underlying_shares": value.underlying_shares,
        "plan_10b5_1": value.plan_10b5_1,
        "promoted_event_type": (
            None
            if value.promoted_event_type is None
            else value.promoted_event_type.value
        ),
        "aggregate_eligibility": value.aggregate_eligibility,
    }


def _form4_event_payload(value: Form4ResearchEvent) -> dict[str, Any]:
    return {
        "event_type": value.event_type.value,
        "issuer_ticker": value.issuer_ticker,
        "issuer_cik": value.issuer_cik,
        "accession_number": value.accession_number,
        "reporting_owners": [
            _form4_reporting_owner_payload(owner)
            for owner in value.reporting_owners
        ],
        "transaction_date": _date_iso(value.transaction_date),
        "available_at": _iso(value.available_at),
        "transaction_code": value.transaction_code,
        "shares": value.shares,
        "price_per_share": value.price_per_share,
        "approximate_value": value.approximate_value,
        "direct_or_indirect": value.direct_or_indirect,
        "shares_owned_following": value.shares_owned_following,
        "is_amendment": value.is_amendment,
        "amends_accession": value.amends_accession,
        "aggregate_eligibility": value.aggregate_eligibility,
        "plan_10b5_1": value.plan_10b5_1,
    }


def _validate_submission_identity(
    payload: Mapping[str, Any], issuer: SECIssuer
) -> None:
    raw_cik = payload.get("cik")
    try:
        returned_cik = str(int(str(raw_cik))).zfill(10)
    except (TypeError, ValueError) as exc:
        raise SECMappingDriftError(
            f"SEC mapping drift for {issuer.ticker}: submissions CIK unavailable"
        ) from exc
    tickers = payload.get("tickers")
    returned_tickers = {
        str(value).upper() for value in tickers if isinstance(value, str)
    } if isinstance(tickers, list) else set()
    if returned_cik != issuer.cik or issuer.ticker not in returned_tickers:
        raise SECMappingDriftError(
            f"SEC mapping drift for {issuer.ticker}; manual review required"
        )
    returned_name = payload.get("name")
    if (
        isinstance(returned_name, str)
        and returned_name.strip()
        and _normalized_name(returned_name) != _normalized_name(issuer.issuer_name)
    ):
        LOGGER.warning(
            "SEC issuer-name drift warning ticker=%s cik=%s",
            issuer.ticker,
            issuer.cik,
        )


def _safe_sec_document_identity(value: str, *, allow_renderer_path: bool) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or ".." in path.parts or urlparse(normalized).scheme:
        raise ValueError("SEC document identity is invalid")
    if not allow_renderer_path and len(path.parts) != 1:
        raise ValueError("SEC document identity must be a filename")
    return normalized


def _normalize_document(document: bytes) -> str:
    text = document.decode("utf-8", errors="replace")
    text = re.sub(
        r"(?is)<(?:br|/?p|/?div|/?tr|/?li|/?h[1-6])\b[^>]*>", "\n", text
    )
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return html.unescape(text).replace("\u00a0", " ")


def _column_rows(columns: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    lengths = [len(value) for value in columns.values() if isinstance(value, list)]
    if not lengths:
        return ()
    return tuple(
        {
            key: value[index]
            if isinstance(value, list) and index < len(value)
            else None
            for key, value in columns.items()
        }
        for index in range(max(lengths))
    )


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{14}", raw):
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(
                tzinfo=_EDGAR_ACCEPTANCE_TIMEZONE
            ).astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _xml_elements(root: ET.Element, name: str) -> tuple[ET.Element, ...]:
    return tuple(
        element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == name
    )


def _xml_text(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != name:
            continue
        if element.text and element.text.strip():
            return element.text.strip()
        for child in element.iter():
            if (
                child is not element
                and child.tag.rsplit("}", 1)[-1] == "value"
                and child.text
                and child.text.strip()
            ):
                return child.text.strip()
    return None


def _xml_number(root: ET.Element, name: str) -> float | None:
    value = _xml_text(root, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _xml_plan_indicator(transaction: ET.Element) -> bool | None:
    value = _xml_text(transaction, "tradingPlan10b5")
    if value is None:
        return None
    if value.lower() in {"1", "true"}:
        return True
    if value.lower() in {"0", "false"}:
        return False
    return None


def _stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _text_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result.strip():
        raise ConfigValidationError(f"SEC {name} must be a non-empty string")
    return result.strip()


def _bool(value: Mapping[str, Any], name: str) -> bool:
    result = value.get(name)
    if not isinstance(result, bool):
        raise ConfigValidationError(f"sec_edgar.{name} must be bool")
    return result


def _non_negative_int(value: Mapping[str, Any], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ConfigValidationError(f"sec_edgar.{name} must be non-negative int")
    return result


def _positive_float(value: Mapping[str, Any], name: str) -> float:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ConfigValidationError(f"sec_edgar.{name} must be numeric")
    converted = float(result)
    if converted <= 0:
        raise ConfigValidationError(f"sec_edgar.{name} must be positive")
    return converted


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _date_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "EIGHT_K_EXTRACTION_VERSION",
    "EIGHT_K_TRUNCATION_POLICY",
    "MAX_SEC_REQUEST_RATE_PER_SECOND",
    "EightKSection",
    "Form4ReportingOwner",
    "Form4ResearchEvent",
    "Form4Transaction",
    "ParsedForm4",
    "PreparedEightKSection",
    "SECEDGARCollector",
    "SECEDGARConfigurationError",
    "SECEDGARFairAccessError",
    "SECEDGARHTTPClient",
    "SECEDGARHTTPError",
    "SECEDGARSettings",
    "SECFiling",
    "SECIssuer",
    "SECMappingDriftError",
    "default_aggregate_form4_events",
    "discover_filings",
    "extract_relevant_8k_sections",
    "filing_document_url",
    "filing_index_url",
    "load_sec_issuers",
    "normalize_accession_number",
    "parse_form4",
    "prepare_8k_section",
    "resolve_form4_xml_document",
]
