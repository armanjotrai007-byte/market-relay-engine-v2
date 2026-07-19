"""Versioned prompt loading and deterministic context-prompt rendering."""

from __future__ import annotations

from datetime import datetime
from importlib import resources
import json
from typing import Any, Iterable

from market_relay_engine.ai_context.schema import (
    CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1,
    CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
    DEFAULT_MAX_SUMMARY_CHARACTERS,
    build_context_filter_response_schema,
)
from market_relay_engine.contracts.context import (
    ContextClassificationEventType,
    ContextClassificationRequest,
    ContextRiskLevel,
    ContextUrgency,
)


DEFAULT_MAX_INPUT_CHARACTERS = 12_000
CONTEXT_FILTER_PROMPT_VERSION_V1 = "context_filter_v1"
CONTEXT_FILTER_PROMPT_VERSION_V2 = "context_filter_v2_scope"
SUPPORTED_PROMPT_VERSIONS = frozenset(
    {CONTEXT_FILTER_PROMPT_VERSION_V1, CONTEXT_FILTER_PROMPT_VERSION_V2}
)

_PROMPT_FILE_BY_VERSION = {
    CONTEXT_FILTER_PROMPT_VERSION_V1: "context_filter_v1.md",
    CONTEXT_FILTER_PROMPT_VERSION_V2: "context_filter_v2_scope.md",
}
_RESPONSE_SCHEMA_BY_PROMPT_VERSION = {
    CONTEXT_FILTER_PROMPT_VERSION_V1: CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1,
    CONTEXT_FILTER_PROMPT_VERSION_V2: CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
}
_PLACEHOLDERS = {
    "@@TRUSTED_METADATA_JSON@@",
    "@@ALLOWED_EVENT_TYPES_JSON@@",
    "@@ALLOWED_RISK_LEVELS_JSON@@",
    "@@ALLOWED_URGENCY_VALUES_JSON@@",
    "@@RESPONSE_SCHEMA_JSON@@",
    "@@UNTRUSTED_SOURCE_TEXT_JSON@@",
}


def load_prompt_template(prompt_version: str) -> str:
    """Load one allow-listed, packaged prompt template by version."""
    if not isinstance(prompt_version, str) or not prompt_version.strip():
        raise ValueError("prompt_version must be a non-empty string")
    try:
        filename = _PROMPT_FILE_BY_VERSION[prompt_version]
    except KeyError as exc:
        raise ValueError(f"unsupported prompt version: {prompt_version}") from exc

    template = (
        resources.files("market_relay_engine.ai_context")
        .joinpath("prompts", filename)
        .read_text(encoding="utf-8")
    )
    missing = sorted(token for token in _PLACEHOLDERS if token not in template)
    if missing:
        raise RuntimeError(
            f"prompt template {prompt_version} is missing required placeholders"
        )
    return template


def _prompt_json(value: object) -> str:
    """Encode prompt data without allowing text to inject section delimiters."""
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _bounded_positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _trusted_metadata(
    request: ContextClassificationRequest,
    sector_hints: Iterable[str],
    *,
    allowed_tickers: tuple[str, ...] = (),
    allowed_sectors: tuple[str, ...] = (),
) -> dict[str, Any]:
    if isinstance(sector_hints, (str, bytes)):
        raise ValueError("sector_hints must be an iterable of non-empty strings")
    sectors = list(sector_hints)
    if any(not isinstance(item, str) or not item.strip() for item in sectors):
        raise ValueError("sector_hints must contain only non-empty strings")
    if request.classification_input_fingerprint is not None:
        # External archives own a canonical semantic request fingerprint that
        # deliberately excludes observation IDs and timestamps.  Keep the
        # provider-visible trusted metadata aligned with that identity so a
        # later observation/backfill can safely reuse the first result.
        metadata = {
            "affected_tickers": list(request.affected_tickers),
            "document_hash": request.document_hash,
            "sector_hints": sectors,
            "source": request.source,
            "source_platform": request.source_platform,
            "source_type": request.source_type,
        }
    else:
        metadata = {
            "affected_tickers": list(request.affected_tickers),
            "collected_at": _isoformat(request.collected_at),
            "document_hash": request.document_hash,
            "normalized_at": _isoformat(request.normalized_at),
            "raw_input_hash": request.raw_input_hash,
            "raw_input_id": request.raw_input_id,
            "sector_hints": sectors,
            "source": request.source,
            "source_document_id": request.source_document_id,
            "source_locator": request.source_locator,
            "source_platform": request.source_platform,
            "source_published_at": _isoformat(request.source_published_at),
            "source_type": request.source_type,
            "source_updated_at": _isoformat(request.source_updated_at),
            "source_uri": request.source_uri,
        }
    if request.prompt_version == CONTEXT_FILTER_PROMPT_VERSION_V2:
        metadata.update(
            {
                "affected_sectors": list(request.affected_sectors),
                "global_relevance": request.global_relevance,
                "allowed_tickers": list(allowed_tickers),
                "allowed_sectors": list(allowed_sectors),
            }
        )
    return metadata


def render_context_filter_prompt(
    request: ContextClassificationRequest,
    *,
    sector_hints: Iterable[str] = (),
    max_input_characters: int = DEFAULT_MAX_INPUT_CHARACTERS,
    max_summary_characters: int = DEFAULT_MAX_SUMMARY_CHARACTERS,
    allowed_tickers: tuple[str, ...] = (),
    allowed_sectors: tuple[str, ...] = (),
    response_schema_version: str | None = None,
) -> str:
    """Render a source-neutral prompt with bounded, isolated untrusted text."""
    if not isinstance(request, ContextClassificationRequest):
        raise TypeError("request must be a ContextClassificationRequest")
    input_limit = _bounded_positive_integer(
        max_input_characters,
        "max_input_characters",
    )
    template = load_prompt_template(request.prompt_version)
    expected_schema_version = _RESPONSE_SCHEMA_BY_PROMPT_VERSION[
        request.prompt_version
    ]
    selected_schema_version = response_schema_version or expected_schema_version
    if selected_schema_version != expected_schema_version:
        raise ValueError("prompt and response schema versions must match")
    source_text = request.input_text[:input_limit]
    source_payload = {
        "character_count": len(source_text),
        "text": source_text,
        "truncated": len(request.input_text) > input_limit,
    }
    response_schema = build_context_filter_response_schema(
        max_summary_characters=max_summary_characters,
        response_schema_version=selected_schema_version,
        allowed_tickers=allowed_tickers,
        allowed_sectors=allowed_sectors,
    )
    if selected_schema_version == CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2:
        canonical_allowed_tickers = tuple(
            response_schema["properties"]["affected_tickers"]["items"]["enum"]
        )
        canonical_allowed_sectors = tuple(
            response_schema["properties"]["affected_sectors"]["items"]["enum"]
        )
    else:
        canonical_allowed_tickers = ()
        canonical_allowed_sectors = ()
    replacements = {
        "@@TRUSTED_METADATA_JSON@@": _prompt_json(
            _trusted_metadata(
                request,
                sector_hints,
                allowed_tickers=canonical_allowed_tickers,
                allowed_sectors=canonical_allowed_sectors,
            )
        ),
        "@@ALLOWED_EVENT_TYPES_JSON@@": _prompt_json(
            [member.value for member in ContextClassificationEventType]
        ),
        "@@ALLOWED_RISK_LEVELS_JSON@@": _prompt_json(
            [member.value for member in ContextRiskLevel]
        ),
        "@@ALLOWED_URGENCY_VALUES_JSON@@": _prompt_json(
            [member.value for member in ContextUrgency]
        ),
        "@@RESPONSE_SCHEMA_JSON@@": _prompt_json(response_schema),
        "@@UNTRUSTED_SOURCE_TEXT_JSON@@": _prompt_json(source_payload),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    if any(token in rendered for token in _PLACEHOLDERS):
        raise RuntimeError("prompt rendering left an unresolved placeholder")
    return rendered


__all__ = [
    "CONTEXT_FILTER_PROMPT_VERSION_V1",
    "CONTEXT_FILTER_PROMPT_VERSION_V2",
    "DEFAULT_MAX_INPUT_CHARACTERS",
    "SUPPORTED_PROMPT_VERSIONS",
    "load_prompt_template",
    "render_context_filter_prompt",
]
