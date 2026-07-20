"""Strict provider-output schema for AI context classification."""

from __future__ import annotations

from enum import Enum
from typing import Any

from market_relay_engine.contracts.context import (
    ContextClassificationEventType,
    ContextRiskLevel,
    ContextUrgency,
)


CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1 = "context_classification_response_v1"
CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2 = "context_classification_response_v2"
# Backward-compatible name used by PR35-PR37 and existing configuration.
CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION = CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1
SUPPORTED_CONTEXT_FILTER_RESPONSE_SCHEMA_VERSIONS = frozenset(
    {
        CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1,
        CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
    }
)
DEFAULT_MAX_SUMMARY_CHARACTERS = 500


def _enum_values(enum_type: type[Enum]) -> list[str]:
    """Return contract enum values in their canonical declaration order."""
    return [str(member.value) for member in enum_type]


def build_context_filter_response_schema(
    *,
    max_summary_characters: int = DEFAULT_MAX_SUMMARY_CHARACTERS,
    response_schema_version: str = CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION,
    allowed_tickers: tuple[str, ...] = (),
    allowed_sectors: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the strict JSON Schema sent to and enforced around the provider.

    Classification enums are sourced directly from the PR34 contracts so the
    prompt and structured-output boundary cannot drift from those contracts.
    Trusted provenance and trading-action fields are deliberately absent. V1
    owns only semantic classification values; V2 additionally owns bounded
    scope suggestions from caller-supplied ticker and sector allowlists.
    """
    if (
        isinstance(max_summary_characters, bool)
        or not isinstance(max_summary_characters, int)
        or max_summary_characters < 1
    ):
        raise ValueError("max_summary_characters must be a positive integer")
    if response_schema_version not in SUPPORTED_CONTEXT_FILTER_RESPONSE_SCHEMA_VERSIONS:
        raise ValueError("unsupported context-filter response schema version")

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["VALID", "ABSTAINED"],
            },
            "event_type": {
                "type": "string",
                "enum": _enum_values(ContextClassificationEventType),
            },
            "risk_level": {
                "type": "string",
                "enum": _enum_values(ContextRiskLevel),
            },
            "urgency": {
                "type": "string",
                "enum": _enum_values(ContextUrgency),
            },
            "confidence": {
                "anyOf": [
                    {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    {"type": "null"},
                ]
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": max_summary_characters,
            },
        },
        "required": [
            "status",
            "event_type",
            "risk_level",
            "urgency",
            "confidence",
            "summary",
        ],
    }
    if response_schema_version == CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1:
        return schema

    tickers = _canonical_allowlist(allowed_tickers, "allowed_tickers")
    sectors = _canonical_allowlist(allowed_sectors, "allowed_sectors")
    scope_properties = {
        "affected_tickers": {
            "type": "array",
            "items": {"type": "string", "enum": tickers},
            "maxItems": len(tickers),
            "uniqueItems": True,
        },
        "affected_sectors": {
            "type": "array",
            "items": {"type": "string", "enum": sectors},
            "maxItems": len(sectors),
            "uniqueItems": True,
        },
        "global_relevance": {"type": "boolean"},
    }
    schema["properties"].update(scope_properties)
    schema["required"].extend(scope_properties)
    return schema


def _canonical_allowlist(value: object, field_name: str) -> list[str]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        normalized.add(item.strip().upper())
    return sorted(normalized)


__all__ = [
    "CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION",
    "CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V1",
    "CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2",
    "DEFAULT_MAX_SUMMARY_CHARACTERS",
    "SUPPORTED_CONTEXT_FILTER_RESPONSE_SCHEMA_VERSIONS",
    "build_context_filter_response_schema",
]
