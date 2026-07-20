"""Provider-neutral classification boundary and live Gemini implementation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
import logging
import os
import random
import re
from threading import Lock
from time import perf_counter, sleep
from typing import Any, Protocol

import httpx
import requests

from market_relay_engine.ai_context.prompting import (
    CONTEXT_FILTER_PROMPT_VERSION_V2,
    render_context_filter_prompt,
)
from market_relay_engine.ai_context.runtime_guards import (
    CachedClassification,
    ClassificationDedupCache,
    GeminiProcessRuntime,
    ProviderCallBudget,
    classification_fingerprint,
    get_gemini_process_runtime,
)
from market_relay_engine.ai_context.schema import (
    CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2,
    build_context_filter_response_schema,
)
from market_relay_engine.ai_context.settings import AIContextFilterSettings
from market_relay_engine.common.ids import new_record_id
from market_relay_engine.common.time import utc_now
from market_relay_engine.contracts.context import (
    ContextClassificationEventType,
    ContextClassificationRequest,
    ContextClassificationResponse,
    ContextClassificationStatus,
    ContextRiskLevel,
    ContextUrgency,
    ContextValidationResult,
)


LOGGER = logging.getLogger(__name__)
VALIDATOR_VERSION_V1 = "context_filter_validator_v1"
# Backward-compatible PR35 name.
VALIDATOR_VERSION = VALIDATOR_VERSION_V1
VALIDATOR_VERSION_V2 = "context_filter_validator_v2_scope"

_EXPECTED_PROVIDER_KEYS_V1 = {
    "status",
    "event_type",
    "risk_level",
    "urgency",
    "confidence",
    "summary",
}
_EXPECTED_PROVIDER_KEYS_V2 = _EXPECTED_PROVIDER_KEYS_V1 | {
    "affected_tickers",
    "affected_sectors",
    "global_relevance",
}
_SENTENCE_START = r"(?:^\s*|[.!?;:]\s+|\r?\n\s*)"
_TRADE_INSTRUMENT_NOUN = r"(?:stocks?|shares?|securit(?:y|ies)|equity|equities|positions?)"
_ADVICE_ACTOR = r"(?:investors?|traders?|clients?|shareholders?|you)"
_ADVICE_MODAL = r"(?:should|must|ought\s+to|need\s+to)"
_TRADE_ACTION = r"(?:buy(?:ing)?|sell(?:ing)?|hold(?:ing)?)"
_ORDER_MODIFIER = r"(?:buy|sell|market|limit|stop(?:[- ](?:limit|loss))?)"
_TRADING_INSTRUCTION_PATTERNS = (
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:buy|sell|hold)"
        r"(?:\s+(?:now|today|immediately|the\s+dip))?\s*(?:[.!?]|$)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+|do\s+not\s+)?(?:buy|sell|hold)\b(?!-)"
        rf"[^.!?]{{0,60}}\b{_TRADE_INSTRUMENT_NOUN}\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?i:(?:please\s+|do\s+not\s+)?(?:buy|sell|hold))"
        r"\s+[A-Z][A-Z0-9.-]{0,5}\b"
    ),
    re.compile(
        rf"\b{_ADVICE_ACTOR}\s+{_ADVICE_MODAL}\s+(?:not\s+)?(?:buy|sell|hold)\b(?!-)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_ADVICE_MODAL}\s+(?:not\s+)?(?:buy|sell|hold)\b(?!-)"
        rf"[^.!?]{{0,60}}\b{_TRADE_INSTRUMENT_NOUN}\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:recommend(?:s|ed|ing|ation)?|advis(?:e|es|ed|ing)|urge(?:s|d|ing)?)\b"
        rf"[^.!?]{{0,60}}\b{_TRADE_ACTION}\b(?!-)[^.!?]{{0,40}}"
        rf"\b{_TRADE_INSTRUMENT_NOUN}\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?i:\b(?:recommend(?:s|ed|ing|ation)?|advis(?:e|es|ed|ing)|"
        r"urge(?:s|d|ing)?)\b[^.!?]{0,60}\b"
        rf"{_TRADE_ACTION}\b(?!-)[^.!?]{{0,20}})"
        r"[A-Z][A-Z0-9.-]{0,5}\b"
        r"(?=\s*(?:(?i:(?:now|today|immediately)\s*)?[.!?](?:\s|$)|$))"
    ),
    re.compile(
        r"\b(?:recommend(?:s|ed|ing|ation)?|advis(?:e|es|ed|ing)|urge(?:s|d|ing)?)\b"
        rf"[^.!?]{{0,40}}\b{_ADVICE_ACTOR}\b[^.!?]{{0,30}}\b(?:buy|sell|hold)\b(?!-)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:buy|sell|hold)\s+(?:recommendation|rating|signal)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:go|stay)\s+(?:long|short)(?:\s+or\s+(?:long|short))?\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:enter|exit|open|close)"
        r"(?:\s+or\s+(?:enter|exit|open|close))?\s+"
        r"(?:(?:a|the|your)\s+)?(?:(?:long|short)\s+)?position\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_ADVICE_MODAL}\s+(?:enter|exit|open|close)\s+"
        r"(?:(?:a|the|your)\s+)?(?:(?:long|short)\s+)?position\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?i:(?:please\s+)?(?:do\s+not\s+)?"
        r"(?:enter|exit|open|close)"
        r"(?:\s+or\s+(?:enter|exit|open|close))?)\s+"
        r"[A-Z][A-Z0-9.-]{0,5}\b"
        r"(?:\s+(?i:position|trade))?"
        r"(?=\s*(?:(?i:now|today|immediately)\s*)?(?:[.!?](?:\s|$)|$))"
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+|do\s+not\s+)?"
        r"(?:place|submit|cancel)"
        r"(?:(?:,\s*(?:or\s+)?|\s+or\s+)(?:place|submit|cancel))*\s+"
        rf"(?:(?:an?|the|your)\s+)?(?:{_ORDER_MODIFIER}\s+)?order\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_ADVICE_MODAL}\s+(?:not\s+)?(?:place|submit|cancel)\s+"
        rf"(?:(?:an?|the|your)\s+)?(?:{_ORDER_MODIFIER}\s+)?order\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:use|apply|increase|decrease|reduce)\s+"
        r"(?:the\s+|your\s+)?leverage\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:recommend(?:s|ed|ing)?|advis(?:e|es|ed|ing))\b"
        r"[^.!?]{0,40}\b(?:using\s+)?leverage\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:increase|decrease|reduce|raise|use)"
        r"(?:\s+or\s+(?:increase|decrease|reduce|raise))?\s+"
        r"(?:(?:a|the|your)\s+)?(?:larger\s+|smaller\s+)?position[- ]siz(?:e|ing)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_ADVICE_MODAL}\s+(?:increase|decrease|reduce|raise|use)\s+"
        r"(?:(?:a|the|your)\s+)?(?:larger\s+|smaller\s+)?position[- ]siz(?:e|ing)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:increase|decrease|reduce|raise)\s+"
        r"(?:(?:a|the|your)\s+)?position\b(?:\s+(?:size\b|by\b|to\b))?",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:increase|decrease|reduce)\s+"
        r"(?:(?:the|your)\s+)?exposure\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?i:(?:please\s+)?(?:"
        r"allocate\s+\d+(?:\.\d+)?%\s+to|"
        r"take\s+(?:an?\s+)?\d+(?:\.\d+)?%\s+position\s+in)\s+)"
        r"[A-Z][A-Z0-9.-]{0,5}\b"
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:set|use|adopt)\s+"
        r"(?:(?:a|the|your)\s+)?price[- ]target\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:recommend(?:s|ed|ing)?|suggest(?:s|ed|ing)?)\b"
        r"[^.!?]{0,40}\bprice[- ]target\b|"
        r"\b(?:recommended|suggested)\s+price[- ]target\b|"
        r"\bprice[- ]target\s+(?:recommendation|advice)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}the\s+price[- ]target\s+(?:is|should\s+be)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(r"\border[- ]side\b", flags=re.IGNORECASE),
    re.compile(
        r"\b(?:order|trade|position)\s+quantity\b|"
        r"\bquantity\s+(?:of|for)\s+(?:(?:the|an?)\s+)?(?:order|trade|position)\b|"
        rf"{_SENTENCE_START}(?:please\s+)?(?:set|specify|use)\s+"
        r"(?:(?:the|your)\s+)?(?:(?:order|trade)\s+)?quantity\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"{_SENTENCE_START}(?:please\s+)?(?:use|select|choose)\s+"
        r"(?:(?:a|the|your)\s+)?(?:broker|brokerage)\b|"
        rf"{_SENTENCE_START}(?:please\s+)?(?:use|select|choose)\s+"
        r"[^.!?]{1,40}\s+as\s+(?:(?:a|the|your)\s+)?(?:broker|brokerage)\b|"
        rf"{_SENTENCE_START}(?:please\s+)?(?:route|send)\s+"
        r"[^.!?]{0,30}\b(?:order|trade)\b",
        flags=re.IGNORECASE,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ContextClassificationAttemptResult:
    """One logical attempt and its optional local validation record."""

    response: ContextClassificationResponse
    validation_result: ContextValidationResult | None = None


def merge_classification_scope(
    request: ContextClassificationRequest,
    response: ContextClassificationResponse,
) -> tuple[list[str], list[str], bool]:
    """Union trusted explicit scope with validated model scope deterministically."""

    if response.classification_request_id != request.classification_request_id:
        raise ValueError("request and response classification identities must match")
    tickers = sorted(
        {
            *(ticker.strip().upper() for ticker in request.affected_tickers),
            *(ticker.strip().upper() for ticker in response.affected_tickers),
        }
    )
    sectors = sorted(
        {
            *(sector.strip().upper() for sector in request.affected_sectors),
            *(sector.strip().upper() for sector in response.affected_sectors),
        }
    )
    return (
        tickers,
        sectors,
        bool(request.global_relevance) or bool(response.global_relevance),
    )


class ContextClassifier(Protocol):
    """Small provider-neutral classification interface."""

    def classify(
        self, request: ContextClassificationRequest
    ) -> ContextClassificationAttemptResult: ...


class InteractionTransport(Protocol):
    """One SDK invocation; repository retry ownership stays above this layer."""

    def create(
        self,
        *,
        model: str,
        prompt: str,
        response_schema: dict[str, object],
        temperature: float,
        max_output_tokens: int,
    ) -> object: ...


class _NoOpProviderDebugLogger:
    """Prevent provider SDK debug mode from logging credentials or bodies."""

    def debug(self, _message: str, *_args: object, **_kwargs: object) -> None:
        return None


class GeminiInteractionTransport:
    """Official ``google-genai`` Interactions API adapter."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        from google import genai
        from google.genai import types

        factory = client_factory or genai.Client
        self._client = factory(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        interactions = getattr(self._client, "interactions")
        sdk_configuration = getattr(interactions, "sdk_configuration", None)
        if sdk_configuration is not None:
            # google-genai's generated Interactions client logs API-key
            # headers, full prompts, and response bodies when its debug mode is
            # enabled.  This transport never permits that logger.
            setattr(
                sdk_configuration,
                "debug_logger",
                _NoOpProviderDebugLogger(),
            )

    def create(
        self,
        *,
        model: str,
        prompt: str,
        response_schema: dict[str, object],
        temperature: float,
        max_output_tokens: int,
    ) -> object:
        interactions = getattr(self._client, "interactions")
        return interactions.create(
            model=model,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": response_schema,
            },
            store=False,
            background=False,
            generation_config={
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            },
        )

    def close(self) -> None:
        """Release SDK HTTP resources held by the reusable client."""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True)
class _Failure:
    category: str
    summary: str
    retryable: bool


_MISSING_INTERACTION_FIELD = object()


def _interaction_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING_INTERACTION_FIELD)
    return getattr(value, name, _MISSING_INTERACTION_FIELD)


def _invalid_interaction_output_failure() -> _Failure:
    return _Failure(
        "PROVIDER_ERROR",
        "Gemini returned an invalid interaction output shape.",
        False,
    )


def _empty_interaction_output_failure() -> _Failure:
    return _Failure(
        "EMPTY_RESPONSE",
        "Gemini returned no structured classification output.",
        False,
    )


def _extract_interaction_output_text(
    interaction: object,
) -> tuple[str | None, _Failure | None]:
    """Read SDK output text first, then mirror its documented steps fallback."""

    try:
        sdk_output_text = _interaction_field(interaction, "output_text")
        if isinstance(sdk_output_text, str) and sdk_output_text.strip():
            return sdk_output_text, None
        if sdk_output_text not in (_MISSING_INTERACTION_FIELD, None) and not isinstance(
            sdk_output_text, str
        ):
            return None, _invalid_interaction_output_failure()
        recognized_empty_output = sdk_output_text is None or (
            isinstance(sdk_output_text, str) and not sdk_output_text.strip()
        )

        nested_output = _interaction_field(interaction, "output")
        if nested_output not in (_MISSING_INTERACTION_FIELD, None):
            nested_output_text = _interaction_field(nested_output, "text")
            if isinstance(nested_output_text, str) and nested_output_text.strip():
                return nested_output_text, None
            if nested_output_text is _MISSING_INTERACTION_FIELD:
                return None, _invalid_interaction_output_failure()
            if nested_output_text is not None and not isinstance(
                nested_output_text, str
            ):
                return None, _invalid_interaction_output_failure()
            recognized_empty_output = True
        elif nested_output is None:
            recognized_empty_output = True

        steps = _interaction_field(interaction, "steps")
        if steps is _MISSING_INTERACTION_FIELD:
            if not recognized_empty_output:
                return None, _invalid_interaction_output_failure()
            return None, _empty_interaction_output_failure()
        if steps is None or steps == []:
            return None, _empty_interaction_output_failure()
        if not isinstance(steps, (list, tuple)):
            return None, _invalid_interaction_output_failure()

        text_parts: list[str] = []
        collecting = False
        for step in reversed(steps):
            step_type = _interaction_field(step, "type")
            if step_type == "user_input":
                break
            if step_type != "model_output":
                if collecting:
                    break
                continue

            content = _interaction_field(step, "content")
            if not isinstance(content, (list, tuple)):
                if collecting:
                    break
                continue

            should_stop = False
            for item in reversed(content):
                if _interaction_field(item, "type") == "text":
                    collecting = True
                    text = _interaction_field(item, "text")
                    text_parts.append(text if isinstance(text, str) else "")
                elif collecting:
                    should_stop = True
                    break
            if should_stop:
                break

        reconstructed = "".join(reversed(text_parts))
        if not reconstructed.strip():
            return None, _empty_interaction_output_failure()
        return reconstructed, None
    except Exception:
        return None, _invalid_interaction_output_failure()


class GeminiContextClassifier:
    """Classify bounded untrusted text without granting trade authority."""

    def __init__(
        self,
        settings: AIContextFilterSettings,
        *,
        api_key: str | None = None,
        transport: InteractionTransport | None = None,
        runtime: GeminiProcessRuntime | None = None,
        cache: ClassificationDedupCache | None = None,
        budget: ProviderCallBudget | None = None,
        ticker_sector_hints: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = utc_now,
        monotonic_clock: Callable[[], float] = perf_counter,
        sleeper: Callable[[float], None] = sleep,
        random_value: Callable[[], float] = random.random,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._settings = settings
        self._api_key = api_key or os.environ.get(settings.api_key_env)
        self._transport = transport
        self._transport_lock = Lock()
        if runtime is not None and (cache is not None or budget is not None):
            raise ValueError("runtime cannot be combined with cache or budget")
        if runtime is None and cache is None and budget is None:
            runtime = get_gemini_process_runtime(
                cache_max_entries=settings.dedup_cache_max_entries,
                max_calls_per_minute=settings.max_provider_calls_per_minute,
                max_calls_per_run=settings.max_provider_calls_per_run,
            )
        self._cache = (
            runtime.cache
            if runtime is not None
            else cache
            if cache is not None
            else ClassificationDedupCache(settings.dedup_cache_max_entries)
        )
        self._budget = (
            runtime.budget
            if runtime is not None
            else budget
            if budget is not None
            else ProviderCallBudget(
                max_calls_per_minute=settings.max_provider_calls_per_minute,
                max_calls_per_run=settings.max_provider_calls_per_run,
            )
        )
        self._ticker_sector_hints = {
            str(ticker): str(sector)
            for ticker, sector in (ticker_sector_hints or {}).items()
        }
        self._scope_config_error = False
        canonical_scope_hints: dict[str, str] = {}
        for ticker, sector in (ticker_sector_hints or {}).items():
            if (
                not isinstance(ticker, str)
                or not ticker.strip()
                or not isinstance(sector, str)
                or not sector.strip()
            ):
                self._scope_config_error = True
                continue
            normalized_ticker = ticker.strip().upper()
            normalized_sector = sector.strip().upper()
            existing = canonical_scope_hints.get(normalized_ticker)
            if existing is not None and existing != normalized_sector:
                self._scope_config_error = True
                continue
            canonical_scope_hints[normalized_ticker] = normalized_sector
        self._canonical_ticker_sector_hints = canonical_scope_hints
        self._allowed_tickers = tuple(sorted(canonical_scope_hints))
        self._allowed_sectors = tuple(sorted(set(canonical_scope_hints.values())))
        self._now = now
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._random_value = random_value
        self._logger = logger

    def classify(
        self, request: ContextClassificationRequest
    ) -> ContextClassificationAttemptResult:
        """Create one logical attempt, coalescing concurrent work through the cache."""
        with self._cache.classification_lock():
            return self._classify_serialized(request)

    def _classify_serialized(
        self, request: ContextClassificationRequest
    ) -> ContextClassificationAttemptResult:
        attempt_id = new_record_id("classification_attempt")
        started = self._monotonic_clock()

        local_reason = self._validate_local_request(request)
        if local_reason is not None:
            return self._validation_rejected(
                request,
                attempt_id=attempt_id,
                reason_code=local_reason,
                provider_request_count=0,
                started=started,
            )

        if not self._settings.enabled:
            return self._provider_failed(
                request,
                attempt_id=attempt_id,
                failure=_Failure(
                    "CLASSIFIER_DISABLED",
                    "Gemini classification is disabled by configuration.",
                    False,
                ),
                provider_request_count=0,
                started=started,
            )

        try:
            response_schema = build_context_filter_response_schema(
                max_summary_characters=self._settings.max_summary_characters,
                response_schema_version=self._settings.response_schema_version,
                allowed_tickers=self._allowed_tickers,
                allowed_sectors=self._allowed_sectors,
            )
            if request.prompt_version == CONTEXT_FILTER_PROMPT_VERSION_V2:
                sector_hints = tuple(
                    sorted(
                        {
                            self._canonical_ticker_sector_hints[ticker]
                            for ticker in request.affected_tickers
                            if ticker in self._canonical_ticker_sector_hints
                        }
                    )
                )
            else:
                sector_hints = tuple(
                    sorted(
                        {
                            self._ticker_sector_hints[ticker]
                            for ticker in request.affected_tickers
                            if ticker in self._ticker_sector_hints
                        }
                    )
                )
            render_config_hash = _render_config_hash(
                response_schema=response_schema,
                sector_hints=sector_hints,
                settings=self._settings,
            )
        except (KeyError, TypeError, ValueError):
            return self._validation_rejected(
                request,
                attempt_id=attempt_id,
                reason_code="PROMPT_RENDER_FAILED",
                provider_request_count=0,
                started=started,
            )
        fingerprint = classification_fingerprint(
            request,
            model=self._settings.model,
            response_schema_version=self._settings.response_schema_version,
            sector_hints=sector_hints,
            render_config_hash=render_config_hash,
        )
        cached = self._cache.get(fingerprint)
        if cached is not None:
            return self._deduplicated_result(request, attempt_id=attempt_id, cached=cached)

        if self._transport is None and not self._api_key:
            return self._provider_failed(
                request,
                attempt_id=attempt_id,
                failure=_Failure(
                    "MISSING_API_KEY",
                    "Gemini API credentials are unavailable.",
                    False,
                ),
                provider_request_count=0,
                started=started,
            )

        try:
            prompt = render_context_filter_prompt(
                request,
                sector_hints=sector_hints,
                max_input_characters=self._settings.max_input_characters,
                max_summary_characters=self._settings.max_summary_characters,
                allowed_tickers=self._allowed_tickers,
                allowed_sectors=self._allowed_sectors,
                response_schema_version=self._settings.response_schema_version,
            )
            if len(prompt) > self._settings.max_prompt_characters:
                return self._validation_rejected(
                    request,
                    attempt_id=attempt_id,
                    reason_code="PROMPT_TOO_LONG",
                    provider_request_count=0,
                    started=started,
                )
        except (KeyError, TypeError, ValueError, RuntimeError, OSError):
            return self._validation_rejected(
                request,
                attempt_id=attempt_id,
                reason_code="PROMPT_RENDER_FAILED",
                provider_request_count=0,
                started=started,
            )
        try:
            transport = self._get_transport()
        except Exception:  # SDK initialization failure must not escape the boundary.
            return self._provider_failed(
                request,
                attempt_id=attempt_id,
                failure=_Failure(
                    "CLIENT_INITIALIZATION_FAILED",
                    "The Gemini client could not be initialized.",
                    False,
                ),
                provider_request_count=0,
                started=started,
            )

        provider_request_count = 0
        while True:
            if not self._budget.try_acquire():
                return self._provider_failed(
                    request,
                    attempt_id=attempt_id,
                    failure=_Failure(
                        "LOCAL_BUDGET_EXHAUSTED",
                        "The local Gemini provider-call budget is exhausted.",
                        False,
                    ),
                    provider_request_count=provider_request_count,
                    started=started,
                )
            provider_request_count += 1
            try:
                interaction = transport.create(
                    model=self._settings.model,
                    prompt=prompt,
                    response_schema=response_schema,
                    temperature=self._settings.temperature,
                    max_output_tokens=self._settings.max_output_tokens,
                )
                interaction_failure = _interaction_failure(interaction)
                if interaction_failure is not None:
                    if self._should_retry(interaction_failure, provider_request_count):
                        self._backoff(provider_request_count)
                        continue
                    return self._provider_failed(
                        request,
                        attempt_id=attempt_id,
                        failure=interaction_failure,
                        provider_request_count=provider_request_count,
                        started=started,
                    )
            except Exception as exc:  # Provider boundary converts all failures safely.
                failure = _exception_failure(exc)
                if self._should_retry(failure, provider_request_count):
                    self._backoff(provider_request_count)
                    continue
                return self._provider_failed(
                    request,
                    attempt_id=attempt_id,
                    failure=failure,
                    provider_request_count=provider_request_count,
                    started=started,
                )
            break

        output_text, output_failure = _extract_interaction_output_text(interaction)
        if output_failure is not None or output_text is None:
            return self._provider_failed(
                request,
                attempt_id=attempt_id,
                failure=output_failure or _invalid_interaction_output_failure(),
                provider_request_count=provider_request_count,
                started=started,
            )
        try:
            payload = json.loads(output_text, parse_constant=_reject_json_constant)
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._validation_rejected(
                request,
                attempt_id=attempt_id,
                reason_code="MALFORMED_JSON",
                provider_request_count=provider_request_count,
                started=started,
            )

        parsed, reason_code = self._validate_provider_payload(payload)
        if reason_code is not None or parsed is None:
            return self._validation_rejected(
                request,
                attempt_id=attempt_id,
                reason_code=reason_code or "SCHEMA_INVALID",
                provider_request_count=provider_request_count,
                started=started,
            )

        try:
            response = ContextClassificationResponse(
                classification_attempt_id=attempt_id,
                classification_request_id=request.classification_request_id,
                classified_at=self._now(),
                provider=self._settings.provider,
                model_version=self._settings.model,
                prompt_version=request.prompt_version,
                response_schema_version=self._settings.response_schema_version,
                classification_input_fingerprint=(
                    request.classification_input_fingerprint
                ),
                status=parsed["status"],
                provider_latency_ms=self._elapsed_ms(started),
                event_type=parsed["event_type"],
                risk_level=parsed["risk_level"],
                urgency=parsed["urgency"],
                confidence=parsed["confidence"],
                summary=parsed["summary"],
                affected_tickers=parsed["affected_tickers"],
                affected_sectors=parsed["affected_sectors"],
                global_relevance=parsed["global_relevance"],
                provider_request_count=provider_request_count,
                retry_count=max(0, provider_request_count - 1),
                trace_id=request.trace_id,
            )
        except (TypeError, ValueError):
            return self._validation_rejected(
                request,
                attempt_id=attempt_id,
                reason_code="CONTRACT_INVALID",
                provider_request_count=provider_request_count,
                started=started,
            )
        validation = self._successful_validation(request, response)
        result = ContextClassificationAttemptResult(
            response=response,
            validation_result=validation,
        )
        self._cache.put(fingerprint, CachedClassification(response=response))
        self._log_result(request, result)
        return result

    def close(self) -> None:
        """Close an owned or injected transport when it exposes ``close``."""
        transport = self._transport
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def _get_transport(self) -> InteractionTransport:
        transport = self._transport
        if transport is not None:
            return transport
        with self._transport_lock:
            if self._transport is None:
                self._transport = GeminiInteractionTransport(
                    api_key=self._api_key or "",
                    timeout_seconds=self._settings.timeout_seconds,
                )
            return self._transport

    def _validate_local_request(self, request: ContextClassificationRequest) -> str | None:
        if request.prompt_version != self._settings.prompt_version:
            return "PROMPT_VERSION_MISMATCH"
        if (
            request.response_schema_version is not None
            and request.response_schema_version != self._settings.response_schema_version
        ):
            return "RESPONSE_SCHEMA_VERSION_MISMATCH"
        if len(request.input_text) > self._settings.max_input_characters:
            return "INPUT_TOO_LONG"
        if request.prompt_version == CONTEXT_FILTER_PROMPT_VERSION_V2:
            if (
                self._scope_config_error
                or not self._allowed_tickers
                or not self._allowed_sectors
            ):
                return "INVALID_SCOPE_CONFIGURATION"
            if any(ticker not in self._allowed_tickers for ticker in request.affected_tickers):
                return "UNKNOWN_TRUSTED_TICKER_SCOPE"
            if any(sector not in self._allowed_sectors for sector in request.affected_sectors):
                return "UNKNOWN_TRUSTED_SECTOR_SCOPE"
        return None

    def _validate_provider_payload(
        self, payload: object
    ) -> tuple[dict[str, Any] | None, str | None]:
        is_v2 = (
            self._settings.response_schema_version
            == CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2
        )
        expected_keys = (
            _EXPECTED_PROVIDER_KEYS_V2 if is_v2 else _EXPECTED_PROVIDER_KEYS_V1
        )
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            return None, "SCHEMA_INVALID"
        status_raw = payload["status"]
        if status_raw not in {"VALID", "ABSTAINED"}:
            return None, "UNKNOWN_STATUS"
        try:
            event_type = ContextClassificationEventType(payload["event_type"])
            risk_level = ContextRiskLevel(payload["risk_level"])
            urgency = ContextUrgency(payload["urgency"])
        except (TypeError, ValueError):
            return None, "UNKNOWN_ENUM"
        confidence_raw = payload["confidence"]
        if confidence_raw is not None:
            if isinstance(confidence_raw, bool) or not isinstance(
                confidence_raw, (int, float)
            ):
                return None, "INVALID_CONFIDENCE"
            confidence = float(confidence_raw)
            if not 0.0 <= confidence <= 1.0:
                return None, "INVALID_CONFIDENCE"
        else:
            confidence = None
        summary = payload["summary"]
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > self._settings.max_summary_characters
        ):
            return None, "INVALID_SUMMARY"
        if contains_trading_instruction(summary):
            return None, "TRADING_INSTRUCTION_SUMMARY"
        if self._api_key and self._api_key in summary:
            return None, "SECRET_IN_SUMMARY"

        status = ContextClassificationStatus(status_raw)
        if status is ContextClassificationStatus.VALID:
            if (
                event_type is ContextClassificationEventType.UNKNOWN
                or risk_level is ContextRiskLevel.UNKNOWN
                or urgency is ContextUrgency.UNKNOWN
                or confidence is None
            ):
                return None, "INCOMPLETE_VALID_CLASSIFICATION"
        else:
            if (
                event_type is not ContextClassificationEventType.UNKNOWN
                or risk_level is not ContextRiskLevel.UNKNOWN
                or urgency is not ContextUrgency.UNKNOWN
                or confidence is not None
            ):
                return None, "INVALID_ABSTAINED_SHAPE"
        affected_tickers: list[str] = []
        affected_sectors: list[str] = []
        global_relevance: bool | None = None
        if is_v2:
            affected_tickers, scope_reason = _validated_scope_values(
                payload["affected_tickers"],
                allowed=self._allowed_tickers,
                label="TICKER",
            )
            if scope_reason is not None:
                return None, scope_reason
            affected_sectors, scope_reason = _validated_scope_values(
                payload["affected_sectors"],
                allowed=self._allowed_sectors,
                label="SECTOR",
            )
            if scope_reason is not None:
                return None, scope_reason
            if not isinstance(payload["global_relevance"], bool):
                return None, "INVALID_GLOBAL_RELEVANCE"
            global_relevance = payload["global_relevance"]
        return {
            "status": status,
            "event_type": event_type,
            "risk_level": risk_level,
            "urgency": urgency,
            "confidence": confidence,
            "summary": summary.strip(),
            "affected_tickers": affected_tickers,
            "affected_sectors": affected_sectors,
            "global_relevance": global_relevance,
        }, None

    def _deduplicated_result(
        self,
        request: ContextClassificationRequest,
        *,
        attempt_id: str,
        cached: CachedClassification,
    ) -> ContextClassificationAttemptResult:
        original = cached.response
        response = replace(
            original,
            classification_attempt_id=attempt_id,
            classification_request_id=request.classification_request_id,
            classified_at=self._now(),
            provider_latency_ms=0.0,
            provider_request_count=0,
            retry_count=0,
            deduplicated=True,
            reused_classification_attempt_id=original.classification_attempt_id,
            trace_id=request.trace_id,
        )
        result = ContextClassificationAttemptResult(
            response=response,
            validation_result=self._successful_validation(request, response),
        )
        self._log_result(request, result)
        return result

    def _successful_validation(
        self,
        request: ContextClassificationRequest,
        response: ContextClassificationResponse,
    ) -> ContextValidationResult:
        return ContextValidationResult(
            classification_request_id=request.classification_request_id,
            classification_attempt_id=response.classification_attempt_id,
            validation_outcome=True,
            reason_codes=[],
            validator_version=self._validator_version,
            validated_at=self._now(),
            trace_id=request.trace_id,
        )

    @property
    def _validator_version(self) -> str:
        if (
            self._settings.response_schema_version
            == CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION_V2
        ):
            return VALIDATOR_VERSION_V2
        return VALIDATOR_VERSION

    def _validation_rejected(
        self,
        request: ContextClassificationRequest,
        *,
        attempt_id: str,
        reason_code: str,
        provider_request_count: int,
        started: float,
    ) -> ContextClassificationAttemptResult:
        response = ContextClassificationResponse(
            classification_attempt_id=attempt_id,
            classification_request_id=request.classification_request_id,
            classified_at=self._now(),
            provider=self._settings.provider,
            model_version=self._settings.model,
            prompt_version=request.prompt_version,
            response_schema_version=self._settings.response_schema_version,
            classification_input_fingerprint=request.classification_input_fingerprint,
            status=ContextClassificationStatus.VALIDATION_REJECTED,
            provider_latency_ms=self._elapsed_ms(started),
            provider_request_count=provider_request_count,
            retry_count=max(0, provider_request_count - 1),
            trace_id=request.trace_id,
        )
        validation = ContextValidationResult(
            classification_request_id=request.classification_request_id,
            classification_attempt_id=attempt_id,
            validation_outcome=False,
            reason_codes=[reason_code],
            validator_version=self._validator_version,
            validated_at=self._now(),
            safe_detail="Gemini classification output failed local validation.",
            trace_id=request.trace_id,
        )
        result = ContextClassificationAttemptResult(
            response=response,
            validation_result=validation,
        )
        self._log_result(request, result)
        return result

    def _provider_failed(
        self,
        request: ContextClassificationRequest,
        *,
        attempt_id: str,
        failure: _Failure,
        provider_request_count: int,
        started: float,
    ) -> ContextClassificationAttemptResult:
        response = ContextClassificationResponse(
            classification_attempt_id=attempt_id,
            classification_request_id=request.classification_request_id,
            classified_at=self._now(),
            provider=self._settings.provider,
            model_version=self._settings.model,
            prompt_version=request.prompt_version,
            response_schema_version=self._settings.response_schema_version,
            classification_input_fingerprint=request.classification_input_fingerprint,
            status=ContextClassificationStatus.PROVIDER_FAILED,
            provider_latency_ms=self._elapsed_ms(started),
            safe_failure_category=failure.category,
            safe_failure_summary=failure.summary,
            provider_request_count=provider_request_count,
            retry_count=max(0, provider_request_count - 1),
            trace_id=request.trace_id,
        )
        result = ContextClassificationAttemptResult(response=response)
        self._log_result(request, result)
        return result

    def _should_retry(self, failure: _Failure, provider_request_count: int) -> bool:
        return failure.retryable and provider_request_count <= self._settings.max_retries

    def _backoff(self, provider_request_count: int) -> None:
        retry_index = max(0, provider_request_count - 1)
        base = min(
            self._settings.retry_max_delay_seconds,
            self._settings.retry_base_delay_seconds * (2**retry_index),
        )
        jitter = min(
            self._settings.retry_max_delay_seconds - base,
            max(0.0, self._random_value()) * self._settings.retry_base_delay_seconds,
        )
        self._sleeper(base + max(0.0, jitter))

    def _elapsed_ms(self, started: float) -> float:
        return max(0.0, (self._monotonic_clock() - started) * 1000.0)

    def _log_result(
        self,
        request: ContextClassificationRequest,
        result: ContextClassificationAttemptResult,
    ) -> None:
        response = result.response
        self._logger.info(
            "AI context classification request_id=%s attempt_id=%s status=%s "
            "provider=%s model=%s prompt_version=%s input_characters=%d "
            "provider_request_count=%d retry_count=%d deduplicated=%s "
            "latency_ms=%.3f failure_category=%s",
            request.classification_request_id,
            response.classification_attempt_id,
            response.status.value,
            response.provider,
            response.model_version,
            response.prompt_version,
            len(request.input_text),
            response.provider_request_count,
            response.retry_count,
            response.deduplicated,
            response.provider_latency_ms,
            response.safe_failure_category,
        )


def _exception_failure(exc: Exception) -> _Failure:
    from google.genai import errors
    # google-genai 2.10.0's Interactions resource uses this compatibility
    # hierarchy; the older ``google.genai.errors`` hierarchy remains relevant
    # to other SDK surfaces and injected transports.
    from google.genai._gaos.lib import compat_errors as interaction_errors

    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            requests.exceptions.Timeout,
            TimeoutError,
        ),
    ):
        return _Failure("TIMEOUT", "The Gemini request timed out.", True)
    if isinstance(
        exc,
        (
            httpx.NetworkError,
            requests.exceptions.ConnectionError,
            ConnectionError,
        ),
    ):
        return _Failure("NETWORK_INTERRUPTION", "The Gemini connection was interrupted.", True)
    if isinstance(exc, interaction_errors.APITimeoutError):
        return _Failure("TIMEOUT", "The Gemini request timed out.", True)
    if isinstance(exc, interaction_errors.APIConnectionError):
        return _Failure(
            "NETWORK_INTERRUPTION",
            "The Gemini connection was interrupted.",
            True,
        )
    if isinstance(exc, interaction_errors.APIError):
        code = int(getattr(exc, "status_code", 0) or 0)
        safe_markers = (
            f"{str(getattr(exc, 'message', '') or '')[:2000]} "
            f"{str(getattr(exc, 'body', '') or '')[:2000]}"
        ).upper()
        if "SAFETY" in safe_markers or "BLOCKED" in safe_markers:
            return _Failure(
                "SAFETY_BLOCKED",
                "Gemini blocked the content for safety.",
                False,
            )
        if code == 401 or any(
            marker in safe_markers
            for marker in ("API KEY NOT VALID", "API_KEY_INVALID", "INVALID API KEY")
        ):
            return _Failure(
                "AUTHENTICATION_FAILED",
                "Gemini authentication failed.",
                False,
            )
        if code == 403:
            return _Failure("PERMISSION_DENIED", "Gemini permission was denied.", False)
        if code == 429:
            return _Failure("RATE_LIMITED", "Gemini rate-limited the request.", True)
        if code == 408:
            return _Failure("TIMEOUT", "The Gemini request timed out.", True)
        if 500 <= code <= 599:
            return _Failure(
                "PROVIDER_UNAVAILABLE",
                "Gemini is temporarily unavailable.",
                True,
            )
        return _Failure("PROVIDER_ERROR", "Gemini rejected the request.", False)
    if isinstance(exc, errors.APIError):
        code = int(exc.code or 0)
        status = str(exc.status or "").upper()
        safe_markers = f"{status} {str(exc.message or '')[:2000].upper()}"
        if "SAFETY" in safe_markers or "BLOCKED" in safe_markers:
            return _Failure("SAFETY_BLOCKED", "Gemini blocked the content for safety.", False)
        if code == 401 or status == "UNAUTHENTICATED" or any(
            marker in safe_markers
            for marker in ("API KEY NOT VALID", "API_KEY_INVALID", "INVALID API KEY")
        ):
            return _Failure("AUTHENTICATION_FAILED", "Gemini authentication failed.", False)
        if code == 403 or status == "PERMISSION_DENIED":
            return _Failure("PERMISSION_DENIED", "Gemini permission was denied.", False)
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            return _Failure("RATE_LIMITED", "Gemini rate-limited the request.", True)
        if code == 408:
            return _Failure("TIMEOUT", "The Gemini request timed out.", True)
        if 500 <= code <= 599:
            return _Failure("PROVIDER_UNAVAILABLE", "Gemini is temporarily unavailable.", True)
        return _Failure("PROVIDER_ERROR", "Gemini rejected the request.", False)
    return _Failure("PROVIDER_ERROR", "Gemini classification failed.", False)


def _interaction_failure(interaction: object) -> _Failure | None:
    status = str(getattr(interaction, "status", "completed") or "").lower()
    if status == "completed":
        return None
    codes: list[int] = []
    safe_markers: list[str] = []
    for step in getattr(interaction, "steps", None) or []:
        error = getattr(step, "error", None)
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        details = getattr(error, "details", None)
        if isinstance(code, int) and not isinstance(code, bool):
            codes.append(code)
        if message is not None:
            safe_markers.append(str(message).upper()[:1000])
        if details is not None:
            safe_markers.append(str(details).upper()[:2000])
    combined = " ".join(safe_markers)
    if "SAFETY" in combined or "BLOCK" in combined:
        return _Failure("SAFETY_BLOCKED", "Gemini blocked the content for safety.", False)
    if 16 in codes or "UNAUTHENTICATED" in combined:
        return _Failure("AUTHENTICATION_FAILED", "Gemini authentication failed.", False)
    if 7 in codes or "PERMISSION_DENIED" in combined:
        return _Failure("PERMISSION_DENIED", "Gemini permission was denied.", False)
    if 4 in codes or "DEADLINE_EXCEEDED" in combined:
        return _Failure("TIMEOUT", "The Gemini request timed out.", True)
    if status == "budget_exceeded" or 8 in codes or "RESOURCE_EXHAUSTED" in combined:
        return _Failure("RATE_LIMITED", "Gemini rate-limited the request.", True)
    if any(code in {13, 14} for code in codes) or any(
        marker in combined for marker in ("INTERNAL", "UNAVAILABLE")
    ):
        return _Failure("PROVIDER_UNAVAILABLE", "Gemini is temporarily unavailable.", True)
    return _Failure("PROVIDER_FAILED", "Gemini did not complete the interaction.", False)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _validated_scope_values(
    value: object,
    *,
    allowed: tuple[str, ...],
    label: str,
) -> tuple[list[str], str | None]:
    if not isinstance(value, list):
        return [], f"INVALID_{label}_SCOPE"
    if any(not isinstance(item, str) or not item for item in value):
        return [], f"INVALID_{label}_SCOPE"
    if len(set(value)) != len(value):
        return [], f"DUPLICATE_{label}_SCOPE"
    if any(item not in allowed for item in value):
        return [], f"UNKNOWN_{label}_SCOPE"
    return sorted(value), None


def contains_trading_instruction(summary: str) -> bool:
    """Return whether a model summary violates the no-trading-instruction policy."""
    if not isinstance(summary, str):
        raise TypeError("summary must be a string")
    return any(pattern.search(summary) for pattern in _TRADING_INSTRUCTION_PATTERNS)


def _render_config_hash(
    *,
    response_schema: dict[str, object],
    sector_hints: tuple[str, ...],
    settings: AIContextFilterSettings,
) -> str:
    payload = {
        "response_schema": response_schema,
        "sector_hints": sector_hints,
        "max_input_characters": settings.max_input_characters,
        "max_prompt_characters": settings.max_prompt_characters,
        "max_output_tokens": settings.max_output_tokens,
        "temperature": settings.temperature,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "ContextClassificationAttemptResult",
    "ContextClassifier",
    "GeminiContextClassifier",
    "GeminiInteractionTransport",
    "InteractionTransport",
    "VALIDATOR_VERSION",
    "VALIDATOR_VERSION_V1",
    "VALIDATOR_VERSION_V2",
    "contains_trading_instruction",
    "merge_classification_scope",
]
