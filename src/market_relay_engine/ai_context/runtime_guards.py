"""Small process-local guards for Gemini classification calls."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import Lock
from time import monotonic

from market_relay_engine.contracts.context import (
    ContextClassificationRequest,
    ContextClassificationResponse,
)


@dataclass(frozen=True, kw_only=True)
class CachedClassification:
    """Cacheable model-owned classification and its original attempt ID."""

    response: ContextClassificationResponse


class ClassificationDedupCache:
    """Bounded process-local LRU cache; persistence is intentionally absent."""

    def __init__(self, max_entries: int = 256) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive int")
        self._max_entries = max_entries
        self._items: OrderedDict[str, CachedClassification] = OrderedDict()
        self._lock = Lock()
        self._classification_lock = Lock()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def get(self, fingerprint: str) -> CachedClassification | None:
        with self._lock:
            item = self._items.get(fingerprint)
            if item is not None:
                self._items.move_to_end(fingerprint)
            return item

    def put(self, fingerprint: str, item: CachedClassification) -> None:
        with self._lock:
            self._items[fingerprint] = item
            self._items.move_to_end(fingerprint)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def classification_lock(self) -> Lock:
        """Serialize cache-miss provider work so identical concurrent calls coalesce."""
        return self._classification_lock


def classification_fingerprint(
    request: ContextClassificationRequest,
    *,
    model: str,
    response_schema_version: str,
    sector_hints: tuple[str, ...] = (),
    render_config_hash: str | None = None,
) -> str:
    """Hash bounded trusted identity fields, never source text itself."""
    if request.classification_input_fingerprint is not None:
        # External archives calculate the fully pinned semantic identity before
        # constructing the ephemeral request.  Reuse it verbatim so generated
        # request IDs and collection timestamps cannot trigger another call.
        return request.classification_input_fingerprint
    payload = {
        "raw_input_hash": request.raw_input_hash,
        "document_hash": request.document_hash,
        "source_document_id": request.source_document_id,
        "affected_tickers": sorted(request.affected_tickers),
        "source_type": request.source_type,
        "prompt_version": request.prompt_version,
        "model": model,
        "response_schema_version": response_schema_version,
        "sector_hints": sorted(sector_hints),
        "render_config_hash": render_config_hash,
    }
    if request.affected_sectors or request.global_relevance is not None:
        payload["affected_sectors"] = sorted(request.affected_sectors)
        payload["global_relevance"] = request.global_relevance
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


class ProviderCallBudget:
    """Atomic process-local run and rolling-minute provider-call limits."""

    def __init__(
        self,
        *,
        max_calls_per_minute: int,
        max_calls_per_run: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        for value, name in (
            (max_calls_per_minute, "max_calls_per_minute"),
            (max_calls_per_run, "max_calls_per_run"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int")
        self._max_calls_per_minute = max_calls_per_minute
        self._max_calls_per_run = max_calls_per_run
        self._clock = clock
        self._run_count = 0
        self._minute_calls: deque[float] = deque()
        self._lock = Lock()

    @property
    def run_count(self) -> int:
        with self._lock:
            return self._run_count

    def try_acquire(self) -> bool:
        with self._lock:
            now = float(self._clock())
            cutoff = now - 60.0
            while self._minute_calls and self._minute_calls[0] <= cutoff:
                self._minute_calls.popleft()
            if self._run_count >= self._max_calls_per_run:
                return False
            if len(self._minute_calls) >= self._max_calls_per_minute:
                return False
            self._run_count += 1
            self._minute_calls.append(now)
            return True


@dataclass(frozen=True, kw_only=True)
class GeminiProcessRuntime:
    """Shared process boundary for deduplication and provider-call budgets."""

    cache: ClassificationDedupCache
    budget: ProviderCallBudget


_PROCESS_RUNTIMES: dict[tuple[int, int, int], GeminiProcessRuntime] = {}
_PROCESS_RUNTIMES_LOCK = Lock()


def get_gemini_process_runtime(
    *,
    cache_max_entries: int,
    max_calls_per_minute: int,
    max_calls_per_run: int,
) -> GeminiProcessRuntime:
    """Return the shared bounded runtime for one set of configured limits."""
    key = (cache_max_entries, max_calls_per_minute, max_calls_per_run)
    with _PROCESS_RUNTIMES_LOCK:
        runtime = _PROCESS_RUNTIMES.get(key)
        if runtime is None:
            runtime = GeminiProcessRuntime(
                cache=ClassificationDedupCache(cache_max_entries),
                budget=ProviderCallBudget(
                    max_calls_per_minute=max_calls_per_minute,
                    max_calls_per_run=max_calls_per_run,
                ),
            )
            _PROCESS_RUNTIMES[key] = runtime
        return runtime


__all__ = [
    "CachedClassification",
    "ClassificationDedupCache",
    "GeminiProcessRuntime",
    "ProviderCallBudget",
    "classification_fingerprint",
    "get_gemini_process_runtime",
]
