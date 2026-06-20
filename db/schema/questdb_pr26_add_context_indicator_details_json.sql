-- PR26 non-destructive one-time migration for existing QuestDB ledgers.
-- Run once before enabling live EIA WPSR ledger writes.
ALTER TABLE context_indicator_snapshots ADD COLUMN details_json STRING;
