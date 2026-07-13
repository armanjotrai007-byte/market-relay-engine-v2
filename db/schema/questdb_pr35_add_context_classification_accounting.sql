-- PR35 additive classification-attempt accounting migration
-- Idempotent and non-destructive: preserves all existing classification rows
-- Run after merge with Market Relay writer processes stopped
-- PR35 does not apply this migration or write to live QuestDB

ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS provider_request_count LONG;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS retry_count LONG;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS deduplicated BOOLEAN;
ALTER TABLE context_classification_attempts ADD COLUMN IF NOT EXISTS reused_classification_attempt_id STRING;
