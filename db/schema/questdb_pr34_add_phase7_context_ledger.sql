-- PR34 additive Phase 7 context-ledger migration
-- Idempotent and non-destructive: preserves all existing context rows
-- Run after merge with Market Relay writer processes stopped
-- Do not substitute the destructive questdb_ledger_v1.sql reset schema

ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS raw_input_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_document_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS classification_request_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS classification_attempt_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS validation_result_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_type SYMBOL;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_platform SYMBOL;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_uri STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_locator STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS document_hash STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS collected_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS normalized_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS classified_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS validated_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS provider SYMBOL;

ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS context_event_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS raw_input_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS source_document_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS classification_request_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS classification_attempt_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS validation_result_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS source_type SYMBOL;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS source_id STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS source_platform SYMBOL;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS source_uri STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS source_locator STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS document_hash STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS raw_input_hash STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS available_at TIMESTAMP;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS validated_at TIMESTAMP;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS reason_codes_json STRING;
ALTER TABLE context_flags ADD COLUMN IF NOT EXISTS summary STRING;

CREATE TABLE IF NOT EXISTS context_classification_attempts (
    requested_at TIMESTAMP,
    write_time TIMESTAMP,
    classification_attempt_id STRING,
    classification_request_id STRING,
    raw_input_id STRING,
    source_document_id STRING,
    source SYMBOL,
    source_type SYMBOL,
    source_platform SYMBOL,
    source_uri STRING,
    source_locator STRING,
    affected_tickers_json STRING,
    raw_input_hash STRING,
    document_hash STRING,
    source_published_at TIMESTAMP,
    source_updated_at TIMESTAMP,
    collected_at TIMESTAMP,
    normalized_at TIMESTAMP,
    classified_at TIMESTAMP,
    provider SYMBOL,
    model_version SYMBOL,
    prompt_version SYMBOL,
    status SYMBOL,
    event_type SYMBOL,
    risk_level SYMBOL,
    urgency SYMBOL,
    confidence DOUBLE,
    summary STRING,
    validation_result_id STRING,
    validation_outcome BOOLEAN,
    validation_reason_codes_json STRING,
    validator_version SYMBOL,
    validated_at TIMESTAMP,
    provider_latency_ms DOUBLE,
    safe_failure_category SYMBOL,
    safe_failure_summary STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(requested_at) PARTITION BY DAY;

CREATE TABLE IF NOT EXISTS shadow_context_policy_evaluations (
    decision_evaluation_time TIMESTAMP,
    write_time TIMESTAMP,
    shadow_evaluation_id STRING,
    model_signal_id STRING,
    risk_decision_id STRING,
    matched_context_event_ids_json STRING,
    matched_context_flag_ids_json STRING,
    shadow_context_fingerprint STRING,
    policy_version SYMBOL,
    policy_config_hash STRING,
    hypothetical_action SYMBOL,
    proposed_size_factor DOUBLE,
    reason_codes_json STRING,
    run_id STRING,
    session_id STRING,
    schema_version SYMBOL,
    trace_id STRING
) TIMESTAMP(decision_evaluation_time) PARTITION BY DAY;
