"""Validated settings for the provider-neutral AI context classifier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_relay_engine.ai_context.prompting import SUPPORTED_PROMPT_VERSIONS
from market_relay_engine.ai_context.schema import CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION
from market_relay_engine.common.config import ConfigValidationError, load_yaml_config


@dataclass(frozen=True, kw_only=True)
class AIContextFilterSettings:
    """Bounded runtime settings loaded from ``context_sources.yaml``."""

    enabled: bool
    provider: str
    model: str
    api_key_env: str
    prompt_version: str
    response_schema_version: str
    timeout_seconds: float
    max_retries: int
    retry_base_delay_seconds: float
    retry_max_delay_seconds: float
    max_input_characters: int
    max_prompt_characters: int
    max_summary_characters: int
    max_output_tokens: int
    max_provider_calls_per_minute: int
    max_provider_calls_per_run: int
    dedup_cache_max_entries: int
    temperature: float
    direct_trade_authority: bool

    def __post_init__(self) -> None:
        for name in (
            "provider",
            "model",
            "api_key_env",
            "prompt_version",
            "response_schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigValidationError(f"ai_context_filter.{name} must be non-empty")
        if self.provider != "gemini":
            raise ConfigValidationError("ai_context_filter.provider must be gemini")
        if self.prompt_version not in SUPPORTED_PROMPT_VERSIONS:
            raise ConfigValidationError("ai_context_filter.prompt_version is unsupported")
        if self.response_schema_version != CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION:
            raise ConfigValidationError(
                "ai_context_filter.response_schema_version is unsupported"
            )
        if not isinstance(self.enabled, bool):
            raise ConfigValidationError("ai_context_filter.enabled must be bool")
        if self.direct_trade_authority is not False:
            raise ConfigValidationError(
                "ai_context_filter.direct_trade_authority must be false"
            )
        _positive_float(self.timeout_seconds, "timeout_seconds")
        _non_negative_int(self.max_retries, "max_retries")
        if self.max_retries > 2:
            raise ConfigValidationError("ai_context_filter.max_retries must not exceed 2")
        _positive_float(self.retry_base_delay_seconds, "retry_base_delay_seconds")
        _positive_float(self.retry_max_delay_seconds, "retry_max_delay_seconds")
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ConfigValidationError(
                "ai_context_filter.retry_max_delay_seconds must be at least the base delay"
            )
        for name in (
            "max_input_characters",
            "max_prompt_characters",
            "max_summary_characters",
            "max_output_tokens",
            "max_provider_calls_per_minute",
            "max_provider_calls_per_run",
            "dedup_cache_max_entries",
        ):
            _positive_int(getattr(self, name), name)
        if isinstance(self.temperature, bool) or not isinstance(
            self.temperature, (int, float)
        ):
            raise ConfigValidationError("ai_context_filter.temperature must be numeric")
        if float(self.temperature) != 0.0:
            raise ConfigValidationError("ai_context_filter.temperature must be 0")


def load_ai_context_filter_settings(
    *,
    base_dir: str | Path | None = None,
    enabled_override: bool | None = None,
) -> AIContextFilterSettings:
    """Load and validate the existing AI-context section."""
    config = load_yaml_config("context_sources", base_dir=base_dir)
    raw = config.get("ai_context_filter")
    if not isinstance(raw, dict):
        raise ConfigValidationError("context_sources.yaml requires ai_context_filter mapping")
    values: dict[str, Any] = dict(raw)
    if enabled_override is not None:
        if not isinstance(enabled_override, bool):
            raise ConfigValidationError("enabled_override must be bool")
        values["enabled"] = enabled_override
    try:
        return AIContextFilterSettings(
            enabled=values["enabled"],
            provider=values["provider"],
            model=values["model"],
            api_key_env=values["api_key_env"],
            prompt_version=values["prompt_version"],
            response_schema_version=values["response_schema_version"],
            timeout_seconds=values["timeout_seconds"],
            max_retries=values["max_retries"],
            retry_base_delay_seconds=values["retry_base_delay_seconds"],
            retry_max_delay_seconds=values["retry_max_delay_seconds"],
            max_input_characters=values["max_input_characters"],
            max_prompt_characters=values["max_prompt_characters"],
            max_summary_characters=values["max_summary_characters"],
            max_output_tokens=values["max_output_tokens"],
            max_provider_calls_per_minute=values[
                "max_provider_calls_per_minute"
            ],
            max_provider_calls_per_run=values["max_provider_calls_per_run"],
            dedup_cache_max_entries=values["dedup_cache_max_entries"],
            temperature=values["temperature"],
            direct_trade_authority=values["direct_trade_authority"],
        )
    except KeyError as exc:
        raise ConfigValidationError(
            f"ai_context_filter missing required setting: {exc.args[0]}"
        ) from exc


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValidationError(f"ai_context_filter.{name} must be numeric")
    converted = float(value)
    if converted <= 0.0:
        raise ConfigValidationError(f"ai_context_filter.{name} must be positive")
    return converted


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigValidationError(f"ai_context_filter.{name} must be a positive int")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigValidationError(
            f"ai_context_filter.{name} must be a non-negative int"
        )
    return value


__all__ = ["AIContextFilterSettings", "load_ai_context_filter_settings"]
