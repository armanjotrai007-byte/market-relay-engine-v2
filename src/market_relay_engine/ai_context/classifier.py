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

from market_relay_engine.ai_context.prompting import render_context_filter_prompt
from market_relay_engine.ai_context.runtime_guards import (
    CachedClassification,
    ClassificationDedupCache,
    GeminiProcessRuntime,
    ProviderCallBudget,
    classification_fingerprint,
    get_gemini_process_runtime,
)
from market_relay_engine.ai_context.schema import build_context_filter_response_schema
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
VALIDATOR_VERSION = "context_filter_validator_v1"

_EXPECTED_PROVIDER_KEYS = {
    "status",
    "event_type",
    "risk_level",
    "urgency",
    "confidence",
    "summary",
}
_TRADING_INSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\b(?:buy|sell|hold)\b",
        r"\b(?:place|submit|cancel)\s+(?:an?\s+)?order\b",
        r"\bleverag(?:e|ed|ing)\b",
        r"\bposition[- ]siz(?:e|ing)\b",
        r"\bprice[- ]target\b",
    )
)


@dataclass(frozen=True, kw_only=True)
class ContextClassificationAttemptResult:
    """One logical attempt and its optional local validation record."""

    response: ContextClassificationResponse
    validation_result: ContextValidationResult | None = None


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
        timeout_seconds: float,
    ) -> object: ...


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

    def create(
        self,
        *,
        model: str,
        prompt: str,
        response_schema: dict[str, object],
        temperature: float,
        max_output_tokens: int,
        timeout_seconds: float,
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
            timeout=timeout_seconds,
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
                max_summary_characters=self._settings.max_summary_characters
            )
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
                    timeout_seconds=self._settings.timeout_seconds,
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

        output_text = getattr(interaction, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            return self._provider_failed(
                request,
                attempt_id=attempt_id,
                failure=_Failure(
                    "EMPTY_RESPONSE",
                    "Gemini returned no structured classification output.",
                    False,
                ),
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
                status=parsed["status"],
                provider_latency_ms=self._elapsed_ms(started),
                event_type=parsed["event_type"],
                risk_level=parsed["risk_level"],
                urgency=parsed["urgency"],
                confidence=parsed["confidence"],
                summary=parsed["summary"],
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
        if len(request.input_text) > self._settings.max_input_characters:
            return "INPUT_TOO_LONG"
        return None

    def _validate_provider_payload(
        self, payload: object
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(payload, dict) or set(payload) != _EXPECTED_PROVIDER_KEYS:
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
        return {
            "status": status,
            "event_type": event_type,
            "risk_level": risk_level,
            "urgency": urgency,
            "confidence": confidence,
            "summary": summary.strip(),
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
            validator_version=VALIDATOR_VERSION,
            validated_at=self._now(),
            trace_id=request.trace_id,
        )

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
            validator_version=VALIDATOR_VERSION,
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
    "contains_trading_instruction",
]
