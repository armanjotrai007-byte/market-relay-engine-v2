# QuestDB Ledger Schema

The canonical fresh-install schema is:

```text
db/schema/questdb_ledger_v1.sql
```

It is a destructive local-development reset: it drops and recreates ledger
tables. Use it only for a fresh or disposable database whose contents may be
destroyed. It is never the migration path for a persistent ledger.

QuestDB remains a bot ledger and black-box recorder. It must not contain raw
Databento market data, full SEC filings, full articles or social posts, full
normalized context documents, classification request excerpts, raw prompts,
credentials, secrets, full provider exceptions, or tracebacks.

## PR34 Phase 7 tables

`context_classification_attempts` stores audit metadata for one classification
attempt: request/attempt/raw/document IDs, source identity and locator, ticker
list JSON, hashes, source/collection/normalization/request/classification times,
provider/model/prompt versions, strict status and classification enums,
confidence, concise summary, validation metadata, provider latency, safe
failure category/summary, run/session/schema IDs, and trace ID. It contains no
input text, prompt body, document body, or exception detail.

PR35 appends provider-request count, retry count, deduplication state, and an
optional reused-attempt ID. One row still represents one logical
`classify()` attempt; internal Gemini HTTP retries do not create extra rows.

`shadow_context_policy_evaluations` stores a research-only comparison at one
`decision_evaluation_time`: evaluation/model-signal/optional-risk-decision IDs,
matched event and flag ID lists, context fingerprint, policy version and config
hash, hypothetical action, optional proposed size factor, reason codes,
run/session/schema IDs, and trace ID. Its action never replaces the real
`RiskDecision`.

PR34 also appends nullable Phase 7 lineage, source, hash, and UTC timestamp
columns to the existing `context_ai_events` and `context_flags` tables. Existing
legacy rows remain valid. `context_flags` additionally stores reason-code JSON
and a concise summary; neither legacy table gains a raw-text column.

## Persistent-ledger migration

Existing servers must run this additive migration after PR34 is merged and
before starting a PR34 writer:

```text
db/schema/questdb_pr34_add_phase7_context_ledger.sql
```

The file uses one `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statement per new
legacy-table column and `CREATE TABLE IF NOT EXISTS` for each new table. It has
no `DROP`, `TRUNCATE`, `RENAME`, `INSERT`, `UPDATE`, or `DELETE` statement, so it
can be rerun after a partial application and preserves existing rows.

After the PR34 migration, PR35 deployments must run this second additive,
idempotent migration before starting a PR35 writer:

```text
db/schema/questdb_pr35_add_context_classification_accounting.sql
```

It appends only the four nullable accounting/deduplication columns. PR35 does
not apply the migration automatically and neither the classifier nor its live
checker writes to QuestDB.

Before applying it, back up the QuestDB volume, stop writers, and record counts
for `context_ai_events` and `context_flags`. Execute the migration statements in
file order through the local QuestDB Web Console, rerun them to prove
idempotency, then verify both legacy counts are unchanged and both new tables
exist with zero rows. The exact operator checklist and recovery procedure are in
`docs/live_runbook.md`.

Do not run either of these against a persistent ledger as part of the upgrade:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py --apply --required
```

```text
db/schema/questdb_ledger_v1.sql
```

Both belong to explicit destructive reset workflows, not migration.

## Offline validation

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py
```

The checker validates fresh-schema definitions, additive migration safety and
idempotent forms, exact writer/config table agreement, context-snapshot linkage,
forbidden raw-data names, and the absence of source/prompt/exception text
columns. It reads SQL only and does not contact QuestDB.

`context_state_snapshots` remains the ledger target referenced by
`risk_decisions.context_snapshot_id`; PR34 does not change that real
decision-context architecture.
