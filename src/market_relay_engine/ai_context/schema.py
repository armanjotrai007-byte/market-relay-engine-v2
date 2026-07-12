"""Strict provider-output schema for AI context classification."""

from __future__ import annotations

from enum import Enum
from typing import Any

from market_relay_engine.contracts.context import (
    ContextClassificationEventType,
    ContextRiskLevel,
    ContextUrgency,
)


CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION = "context_classification_response_v1"
DEFAULT_MAX_SUMMARY_CHARACTERS = 500


def _enum_values(enum_type: type[Enum]) -> list[str]:
    """Return contract enum values in their canonical declaration order."""
    return [str(member.value) for member in enum_type]


def build_context_filter_response_schema(
    *,
    max_summary_characters: int = DEFAULT_MAX_SUMMARY_CHARACTERS,
) -> dict[str, Any]:
    """Build the strict JSON Schema sent to and enforced around the provider.

    Classification enums are sourced directly from the PR34 contracts so the
    prompt and structured-output boundary cannot drift from those contracts.
    Trusted provenance and trading-action fields are deliberately absent: the
    provider owns only the five classification values in this schema.
    """
    if (
        isinstance(max_summary_characters, bool)
        or not isinstance(max_summary_characters, int)
        or max_summary_characters < 1
    ):
        raise ValueError("max_summary_characters must be a positive integer")

    return {
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


__all__ = [
    "CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION",
    "DEFAULT_MAX_SUMMARY_CHARACTERS",
    "build_context_filter_response_schema",
]
