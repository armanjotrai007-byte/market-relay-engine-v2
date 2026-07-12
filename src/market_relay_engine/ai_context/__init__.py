"""Provider-neutral AI context classification support."""

from market_relay_engine.ai_context.classifier import (
    ContextClassificationAttemptResult,
    ContextClassifier,
    GeminiContextClassifier,
    GeminiInteractionTransport,
    InteractionTransport,
    contains_trading_instruction,
)
from market_relay_engine.ai_context.prompting import (
    DEFAULT_MAX_INPUT_CHARACTERS,
    SUPPORTED_PROMPT_VERSIONS,
    load_prompt_template,
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
    CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION,
    DEFAULT_MAX_SUMMARY_CHARACTERS,
    build_context_filter_response_schema,
)
from market_relay_engine.ai_context.settings import (
    AIContextFilterSettings,
    load_ai_context_filter_settings,
)

__all__ = [
    "AIContextFilterSettings",
    "CachedClassification",
    "ClassificationDedupCache",
    "CONTEXT_FILTER_RESPONSE_SCHEMA_VERSION",
    "ContextClassificationAttemptResult",
    "ContextClassifier",
    "DEFAULT_MAX_INPUT_CHARACTERS",
    "DEFAULT_MAX_SUMMARY_CHARACTERS",
    "SUPPORTED_PROMPT_VERSIONS",
    "GeminiContextClassifier",
    "GeminiInteractionTransport",
    "GeminiProcessRuntime",
    "InteractionTransport",
    "ProviderCallBudget",
    "build_context_filter_response_schema",
    "classification_fingerprint",
    "contains_trading_instruction",
    "get_gemini_process_runtime",
    "load_prompt_template",
    "load_ai_context_filter_settings",
    "render_context_filter_prompt",
]
