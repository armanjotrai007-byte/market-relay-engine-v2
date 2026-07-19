-- PR38 additive external-context metadata migration
-- Idempotent and non-destructive: existing column prefixes and rows are preserved
-- Run after the PR34 and PR35 migrations with ledger writers stopped

ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS affected_sectors_json STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS global_relevance BOOLEAN;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_available_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS system_observed_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS evidence_ready_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_fact_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS source_revision_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS revision_sequence LONG;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS supersedes_revision_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS lifecycle_state SYMBOL;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS lifecycle_effective_at TIMESTAMP;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS classification_input_fingerprint STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS complete_output_fingerprint STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS policy_output_fingerprint STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS canonical_classification_attempt_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS correlation_group_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS related_event_ids_json STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS relationship_types_json STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS classification_conflict_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS conflict_resolution_id STRING;
ALTER TABLE context_ai_events ADD COLUMN IF NOT EXISTS conflict_resolution_generation LONG;

ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS affected_sectors_json STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS global_relevance BOOLEAN;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS source_available_at TIMESTAMP;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS system_observed_at TIMESTAMP;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS evidence_ready_at TIMESTAMP;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS source_fact_id STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS source_revision_id STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS revision_sequence LONG;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS supersedes_revision_id STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS lifecycle_state SYMBOL;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS lifecycle_effective_at TIMESTAMP;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS classification_input_fingerprint STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS complete_output_fingerprint STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS policy_output_fingerprint STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS canonical_classification_attempt_id STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS classification_conflict_id STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS conflict_resolution_id STRING;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS conflict_resolution_generation LONG;
