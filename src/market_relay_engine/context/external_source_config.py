"""Typed, fail-closed configuration for the external-event source pilot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from market_relay_engine.common.config import ConfigValidationError, load_yaml_config
from market_relay_engine.context.external_normalization import PDF_EXTRACTOR_VERSION


EXTERNAL_SOURCE_NAMES: tuple[str, ...] = (
    "veritawire_truth_social",
    "lockheed_martin_rss",
    "palantir_ir",
    "company_earnings",
)
EXTERNAL_CLASSIFICATION_PROMPT_VERSION = "context_filter_v2_scope"
EXTERNAL_CLASSIFICATION_SCHEMA_VERSION = "context_classification_response_v2"


@dataclass(frozen=True, kw_only=True)
class ExternalSourceCommonSettings:
    """Safety, archive, and reproducibility settings shared by pilot sources."""

    source: str
    enabled: bool
    purpose: str
    feeds_ai_context_filter: bool
    direct_trade_authority: bool
    used_in_per_signal_path: bool
    archive_path: Path
    adapter_version: str
    extraction_version: str
    normalizer_version: str
    excerpt_version: str
    scope_resolver_version: str
    classification_prompt_version: str
    classification_response_schema_version: str
    classification_enabled_by_default: bool
    questdb_write_enabled_by_default: bool
    bootstrap_mode: str
    backfill_enabled_by_default: bool

    def __post_init__(self) -> None:
        if self.source not in EXTERNAL_SOURCE_NAMES:
            raise ConfigValidationError(f"unsupported external source: {self.source}")
        if not self.purpose.strip():
            raise ConfigValidationError(f"{self.source}.purpose must be non-empty")
        if not self.feeds_ai_context_filter:
            raise ConfigValidationError(
                f"{self.source}.feeds_ai_context_filter must be true"
            )
        if self.direct_trade_authority:
            raise ConfigValidationError(
                f"{self.source}.direct_trade_authority must be false"
            )
        if self.used_in_per_signal_path:
            raise ConfigValidationError(
                f"{self.source}.used_in_per_signal_path must be false"
            )
        if self.classification_enabled_by_default:
            raise ConfigValidationError(
                f"{self.source}.classification_enabled_by_default must be false"
            )
        if self.questdb_write_enabled_by_default:
            raise ConfigValidationError(
                f"{self.source}.questdb_write_enabled_by_default must be false"
            )
        if self.backfill_enabled_by_default:
            raise ConfigValidationError(
                f"{self.source}.backfill_enabled_by_default must be false"
            )
        if self.bootstrap_mode not in {"live_only", "establish_checkpoint"}:
            raise ConfigValidationError(
                f"{self.source}.bootstrap_mode must be live_only or establish_checkpoint"
            )
        for field_name in (
            "adapter_version",
            "extraction_version",
            "normalizer_version",
            "excerpt_version",
            "scope_resolver_version",
        ):
            if not getattr(self, field_name).strip():
                raise ConfigValidationError(
                    f"{self.source}.{field_name} must be non-empty"
                )
        if self.classification_prompt_version != EXTERNAL_CLASSIFICATION_PROMPT_VERSION:
            raise ConfigValidationError(
                f"{self.source}.classification_prompt_version must be "
                f"{EXTERNAL_CLASSIFICATION_PROMPT_VERSION}"
            )
        if (
            self.classification_response_schema_version
            != EXTERNAL_CLASSIFICATION_SCHEMA_VERSION
        ):
            raise ConfigValidationError(
                f"{self.source}.classification_response_schema_version must be "
                f"{EXTERNAL_CLASSIFICATION_SCHEMA_VERSION}"
            )


@dataclass(frozen=True, kw_only=True)
class ExternalHTTPSourceSettings:
    """Bounded HTTP behavior shared by RSS, IR, and earnings adapters."""

    user_agent: str
    timeout_seconds: float
    max_retries: int
    retry_base_delay_seconds: float
    retry_max_delay_seconds: float
    max_response_bytes: int
    max_items_per_poll: int

    def __post_init__(self) -> None:
        if not self.user_agent.strip() or any(value in self.user_agent for value in "\r\n"):
            raise ConfigValidationError("external source user_agent must be safe and non-empty")
        _require_positive_number(self.timeout_seconds, "timeout_seconds")
        _require_bounded_integer(self.max_retries, "max_retries", minimum=0, maximum=3)
        _require_positive_number(
            self.retry_base_delay_seconds, "retry_base_delay_seconds"
        )
        _require_positive_number(
            self.retry_max_delay_seconds, "retry_max_delay_seconds"
        )
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ConfigValidationError(
                "retry_max_delay_seconds must be at least retry_base_delay_seconds"
            )
        _require_bounded_integer(
            self.max_response_bytes,
            "max_response_bytes",
            minimum=1024,
            maximum=50 * 1024 * 1024,
        )
        _require_bounded_integer(
            self.max_items_per_poll,
            "max_items_per_poll",
            minimum=1,
            maximum=1000,
        )


@dataclass(frozen=True, kw_only=True)
class VeritaWireSourceSettings:
    common: ExternalSourceCommonSettings
    api_key_env: str
    websocket_url: str
    connect_timeout_seconds: float
    close_timeout_seconds: float
    smoke_timeout_seconds: float
    ping_interval_seconds: float
    ping_timeout_seconds: float
    reconnect_base_delay_seconds: float
    reconnect_max_delay_seconds: float
    reconnect_jitter_fraction: float
    max_reconnect_attempts: int
    max_message_bytes: int
    max_records_per_run: int

    def __post_init__(self) -> None:
        if self.api_key_env != "VERITAWIRE_API_KEY":
            raise ConfigValidationError(
                "veritawire_truth_social.api_key_env must be VERITAWIRE_API_KEY"
            )
        _require_url(
            self.websocket_url,
            schemes={"wss"},
            domains={"veritawire.com"},
            field_name="veritawire_truth_social.websocket_url",
        )
        websocket = urlparse(self.websocket_url)
        if (
            websocket.netloc.lower() != "veritawire.com"
            or websocket.path != "/ws"
            or websocket.params
            or websocket.query
        ):
            raise ConfigValidationError(
                "veritawire_truth_social.websocket_url must be the exact official "
                "wss://veritawire.com/ws endpoint with header-only authentication"
            )
        for field_name in (
            "connect_timeout_seconds",
            "close_timeout_seconds",
            "smoke_timeout_seconds",
            "ping_interval_seconds",
            "ping_timeout_seconds",
            "reconnect_base_delay_seconds",
            "reconnect_max_delay_seconds",
        ):
            _require_positive_number(getattr(self, field_name), field_name)
        if self.reconnect_max_delay_seconds < self.reconnect_base_delay_seconds:
            raise ConfigValidationError(
                "reconnect_max_delay_seconds must be at least reconnect_base_delay_seconds"
            )
        if (
            isinstance(self.reconnect_jitter_fraction, bool)
            or not isinstance(self.reconnect_jitter_fraction, (int, float))
            or not 0 <= self.reconnect_jitter_fraction <= 1
        ):
            raise ConfigValidationError("reconnect_jitter_fraction must be from 0 through 1")
        _require_bounded_integer(
            self.max_reconnect_attempts,
            "max_reconnect_attempts",
            minimum=0,
            maximum=100,
        )
        _require_bounded_integer(
            self.max_message_bytes,
            "max_message_bytes",
            minimum=1024,
            maximum=10 * 1024 * 1024,
        )
        _require_bounded_integer(
            self.max_records_per_run,
            "max_records_per_run",
            minimum=1,
            maximum=1000,
        )


@dataclass(frozen=True, kw_only=True)
class LockheedMartinRSSSourceSettings:
    common: ExternalSourceCommonSettings
    http: ExternalHTTPSourceSettings
    feed_url: str
    allowed_domains: tuple[str, ...]
    ticker: str
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        if self.ticker != "LMT":
            raise ConfigValidationError("lockheed_martin_rss.ticker must be LMT")
        _require_url(
            self.feed_url,
            schemes={"https"},
            domains=set(self.allowed_domains),
            field_name="lockheed_martin_rss.feed_url",
        )
        _require_positive_number(self.poll_interval_seconds, "poll_interval_seconds")


@dataclass(frozen=True, kw_only=True)
class PalantirIRSourceSettings:
    common: ExternalSourceCommonSettings
    http: ExternalHTTPSourceSettings
    index_page_url: str
    year_list_url: str
    release_list_url_template: str
    allowed_domains: tuple[str, ...]
    ticker: str
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        if self.ticker != "PLTR":
            raise ConfigValidationError("palantir_ir.ticker must be PLTR")
        for field_name in ("index_page_url", "year_list_url"):
            _require_url(
                getattr(self, field_name),
                schemes={"https"},
                domains=set(self.allowed_domains),
                field_name=f"palantir_ir.{field_name}",
            )
        if self.release_list_url_template.count("{year}") != 1:
            raise ConfigValidationError(
                "palantir_ir.release_list_url_template must contain one {year} token"
            )
        _require_url(
            self.release_list_url_template.replace("{year}", "2026"),
            schemes={"https"},
            domains=set(self.allowed_domains),
            field_name="palantir_ir.release_list_url_template",
        )
        _require_positive_number(self.poll_interval_seconds, "poll_interval_seconds")


@dataclass(frozen=True, kw_only=True)
class CompanyEarningsSourceSettings:
    common: ExternalSourceCommonSettings
    http: ExternalHTTPSourceSettings
    pltr_events_url: str
    lmt_results_url: str
    pltr_allowed_domains: tuple[str, ...]
    lmt_allowed_domains: tuple[str, ...]
    tickers: tuple[str, ...]
    fast_poll_interval_seconds: float
    pdf_extraction_version: str
    max_pdf_pages: int
    max_pdf_text_characters: int

    def __post_init__(self) -> None:
        if self.tickers != ("PLTR", "LMT"):
            raise ConfigValidationError("company_earnings.tickers must be [PLTR, LMT]")
        for name, values in (
            ("pltr_allowed_domains", self.pltr_allowed_domains),
            ("lmt_allowed_domains", self.lmt_allowed_domains),
        ):
            if len(set(values)) != len(values):
                raise ConfigValidationError(f"company_earnings.{name} must be unique")
        _require_url(
            self.pltr_events_url,
            schemes={"https"},
            domains=set(self.pltr_allowed_domains),
            field_name="company_earnings.pltr_events_url",
        )
        _require_url(
            self.lmt_results_url,
            schemes={"https"},
            domains=set(self.lmt_allowed_domains),
            field_name="company_earnings.lmt_results_url",
        )
        _require_positive_number(
            self.fast_poll_interval_seconds, "fast_poll_interval_seconds"
        )
        if self.pdf_extraction_version != PDF_EXTRACTOR_VERSION:
            raise ConfigValidationError(
                "company_earnings.pdf_extraction_version is unsupported"
            )
        if self.max_pdf_pages < 1:
            raise ConfigValidationError("company_earnings.max_pdf_pages must be positive")
        if self.max_pdf_text_characters < 1_000:
            raise ConfigValidationError(
                "company_earnings.max_pdf_text_characters must be at least 1000"
            )


@dataclass(frozen=True, kw_only=True)
class ExternalEventSourcesSettings:
    veritawire: VeritaWireSourceSettings
    lmt_rss: LockheedMartinRSSSourceSettings
    pltr_ir: PalantirIRSourceSettings
    earnings: CompanyEarningsSourceSettings

    @property
    def archive_path(self) -> Path:
        paths = {
            self.veritawire.common.archive_path,
            self.lmt_rss.common.archive_path,
            self.pltr_ir.common.archive_path,
            self.earnings.common.archive_path,
        }
        if len(paths) != 1:
            raise ConfigValidationError(
                "pilot external sources must share one immutable archive root"
            )
        return next(iter(paths))


def load_external_event_source_settings(
    *,
    base_dir: Path,
    config: Mapping[str, Any] | None = None,
) -> ExternalEventSourcesSettings:
    """Load and validate all four disabled-by-default pilot source sections."""

    context_config = config or load_yaml_config("context_sources", base_dir=base_dir)
    unstructured = _mapping(context_config, "unstructured_sources", "context_sources")
    raw = {
        name: _mapping(unstructured, name, "unstructured_sources")
        for name in EXTERNAL_SOURCE_NAMES
    }
    settings = ExternalEventSourcesSettings(
        veritawire=_load_veritawire(raw["veritawire_truth_social"], base_dir),
        lmt_rss=_load_lmt(raw["lockheed_martin_rss"], base_dir),
        pltr_ir=_load_pltr(raw["palantir_ir"], base_dir),
        earnings=_load_earnings(raw["company_earnings"], base_dir),
    )
    settings.archive_path
    return settings


def _load_veritawire(
    value: Mapping[str, Any], base_dir: Path
) -> VeritaWireSourceSettings:
    return VeritaWireSourceSettings(
        common=_common("veritawire_truth_social", value, base_dir),
        api_key_env=_string(value, "api_key_env"),
        websocket_url=_string(value, "websocket_url"),
        connect_timeout_seconds=_number(value, "connect_timeout_seconds"),
        close_timeout_seconds=_number(value, "close_timeout_seconds"),
        smoke_timeout_seconds=_number(value, "smoke_timeout_seconds"),
        ping_interval_seconds=_number(value, "ping_interval_seconds"),
        ping_timeout_seconds=_number(value, "ping_timeout_seconds"),
        reconnect_base_delay_seconds=_number(value, "reconnect_base_delay_seconds"),
        reconnect_max_delay_seconds=_number(value, "reconnect_max_delay_seconds"),
        reconnect_jitter_fraction=_number(value, "reconnect_jitter_fraction"),
        max_reconnect_attempts=_integer(value, "max_reconnect_attempts"),
        max_message_bytes=_integer(value, "max_message_bytes"),
        max_records_per_run=_integer(value, "max_records_per_run"),
    )


def _load_lmt(
    value: Mapping[str, Any], base_dir: Path
) -> LockheedMartinRSSSourceSettings:
    return LockheedMartinRSSSourceSettings(
        common=_common("lockheed_martin_rss", value, base_dir),
        http=_http(value),
        feed_url=_string(value, "feed_url"),
        allowed_domains=_string_tuple(value, "allowed_domains"),
        ticker=_string(value, "ticker"),
        poll_interval_seconds=_number(value, "poll_interval_seconds"),
    )


def _load_pltr(
    value: Mapping[str, Any], base_dir: Path
) -> PalantirIRSourceSettings:
    return PalantirIRSourceSettings(
        common=_common("palantir_ir", value, base_dir),
        http=_http(value),
        index_page_url=_string(value, "index_page_url"),
        year_list_url=_string(value, "year_list_url"),
        release_list_url_template=_string(value, "release_list_url_template"),
        allowed_domains=_string_tuple(value, "allowed_domains"),
        ticker=_string(value, "ticker"),
        poll_interval_seconds=_number(value, "poll_interval_seconds"),
    )


def _load_earnings(
    value: Mapping[str, Any], base_dir: Path
) -> CompanyEarningsSourceSettings:
    return CompanyEarningsSourceSettings(
        common=_common("company_earnings", value, base_dir),
        http=_http(value),
        pltr_events_url=_string(value, "pltr_events_url"),
        lmt_results_url=_string(value, "lmt_results_url"),
        pltr_allowed_domains=_string_tuple(value, "pltr_allowed_domains"),
        lmt_allowed_domains=_string_tuple(value, "lmt_allowed_domains"),
        tickers=_string_tuple(value, "tickers"),
        fast_poll_interval_seconds=_number(value, "fast_poll_interval_seconds"),
        pdf_extraction_version=_string(value, "pdf_extraction_version"),
        max_pdf_pages=_integer(value, "max_pdf_pages"),
        max_pdf_text_characters=_integer(value, "max_pdf_text_characters"),
    )


def _common(
    name: str, value: Mapping[str, Any], base_dir: Path
) -> ExternalSourceCommonSettings:
    archive = _repository_archive_path(_string(value, "archive_path"), base_dir)
    return ExternalSourceCommonSettings(
        source=name,
        enabled=_boolean(value, "enabled"),
        purpose=_string(value, "purpose"),
        feeds_ai_context_filter=_boolean(value, "feeds_ai_context_filter"),
        direct_trade_authority=_boolean(value, "direct_trade_authority"),
        used_in_per_signal_path=_boolean(value, "used_in_per_signal_path"),
        archive_path=archive,
        adapter_version=_string(value, "adapter_version"),
        extraction_version=_string(value, "extraction_version"),
        normalizer_version=_string(value, "normalizer_version"),
        excerpt_version=_string(value, "excerpt_version"),
        scope_resolver_version=_string(value, "scope_resolver_version"),
        classification_prompt_version=_string(
            value, "classification_prompt_version"
        ),
        classification_response_schema_version=_string(
            value, "classification_response_schema_version"
        ),
        classification_enabled_by_default=_boolean(
            value, "classification_enabled_by_default"
        ),
        questdb_write_enabled_by_default=_boolean(
            value, "questdb_write_enabled_by_default"
        ),
        bootstrap_mode=_string(value, "bootstrap_mode"),
        backfill_enabled_by_default=_boolean(
            value, "backfill_enabled_by_default"
        ),
    )


def _http(value: Mapping[str, Any]) -> ExternalHTTPSourceSettings:
    return ExternalHTTPSourceSettings(
        user_agent=_string(value, "user_agent"),
        timeout_seconds=_number(value, "timeout_seconds"),
        max_retries=_integer(value, "max_retries"),
        retry_base_delay_seconds=_number(value, "retry_base_delay_seconds"),
        retry_max_delay_seconds=_number(value, "retry_max_delay_seconds"),
        max_response_bytes=_integer(value, "max_response_bytes"),
        max_items_per_poll=_integer(value, "max_items_per_poll"),
    )


def _repository_archive_path(value: str, base_dir: Path) -> Path:
    root = base_dir.resolve()
    resolved = (root / value).resolve()
    if root not in resolved.parents or resolved == root:
        raise ConfigValidationError("external archive_path must remain inside repository")
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigValidationError(
            "external archive_path must remain inside repository"
        ) from exc
    if relative.parts[:3] != ("data_lake", "context", "external_events"):
        raise ConfigValidationError(
            "external archive_path must be under data_lake/context/external_events"
        )
    return resolved


def _require_url(
    value: str,
    *,
    schemes: set[str],
    domains: set[str],
    field_name: str,
) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in schemes or parsed.hostname not in domains:
        raise ConfigValidationError(
            f"{field_name} must use {sorted(schemes)} on an approved official domain"
        )
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ConfigValidationError(f"{field_name} must not contain credentials or a fragment")


def _require_positive_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigValidationError(f"{name} must be a positive number")


def _require_bounded_integer(
    value: object, name: str, *, minimum: int, maximum: int
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ConfigValidationError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )


def _mapping(value: Mapping[str, Any], name: str, parent: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise ConfigValidationError(f"{parent}.{name} must be a mapping")
    return result


def _string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result.strip():
        raise ConfigValidationError(f"{name} must be a non-empty string")
    return result.strip()


def _boolean(value: Mapping[str, Any], name: str) -> bool:
    result = value.get(name)
    if not isinstance(result, bool):
        raise ConfigValidationError(f"{name} must be boolean")
    return result


def _integer(value: Mapping[str, Any], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ConfigValidationError(f"{name} must be an integer")
    return result


def _number(value: Mapping[str, Any], name: str) -> float:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ConfigValidationError(f"{name} must be a number")
    return float(result)


def _string_tuple(value: Mapping[str, Any], name: str) -> tuple[str, ...]:
    result = value.get(name)
    if not isinstance(result, list) or not result:
        raise ConfigValidationError(f"{name} must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in result):
        raise ConfigValidationError(f"{name} must contain non-empty strings")
    return tuple(item.strip() for item in result)


__all__ = [
    "CompanyEarningsSourceSettings",
    "EXTERNAL_CLASSIFICATION_PROMPT_VERSION",
    "EXTERNAL_CLASSIFICATION_SCHEMA_VERSION",
    "EXTERNAL_SOURCE_NAMES",
    "ExternalEventSourcesSettings",
    "ExternalHTTPSourceSettings",
    "ExternalSourceCommonSettings",
    "LockheedMartinRSSSourceSettings",
    "PalantirIRSourceSettings",
    "VeritaWireSourceSettings",
    "load_external_event_source_settings",
]
