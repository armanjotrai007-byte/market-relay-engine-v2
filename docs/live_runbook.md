# Live Runbook

The repository remains paper-first. Live trading is disabled, and PR34 does not
call brokers, context providers, Gemini, SEC EDGAR, or a running QuestDB during
normal validation.

## Offline PR34 validation

Run from the repository root with the checked-in virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py
& ".\.venv\Scripts\python.exe" -m pytest
```

These commands validate configuration, contracts, SQL text, writer mappings,
and tests without applying schema or contacting external services.

## Required QuestDB migration after merge

After PR34 is merged, every existing persistent QuestDB ledger must be upgraded
before a PR34 writer is started. The upgrade file is:

```text
db/schema/questdb_pr34_add_phase7_context_ledger.sql
```

It uses idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements for
`context_ai_events` and `context_flags`, followed by `CREATE TABLE IF NOT
EXISTS` for the two new Phase 7 tables. It does not drop, truncate, rename,
update, or delete data. Existing rows remain in place and receive null values in
new nullable columns.

Operator procedure:

1. Back up the persistent QuestDB volume according to the local operations
   policy and stop every process that writes ledger rows.
2. Record pre-migration row counts:

   ```sql
   SELECT count() FROM context_ai_events;
   SELECT count() FROM context_flags;
   ```

3. Record the existing columns with `table_columns('context_ai_events')` and
   `table_columns('context_flags')`.
4. In the local QuestDB Web Console, execute the statements from
   `db/schema/questdb_pr34_add_phase7_context_ledger.sql` in file order. Do not
   execute `db/schema/questdb_ledger_v1.sql` and do not use
   `scripts/check_questdb_schema.py --apply`; both are destructive reset paths.
5. Rerun the PR34 migration file once. A second successful run proves the
   `IF NOT EXISTS` migration is idempotent.
6. Confirm the two legacy table counts exactly equal the recorded pre-migration
   counts, their new columns exist, and the new tables
   `context_classification_attempts` and
   `shadow_context_policy_evaluations` exist with zero rows before writers run.
7. Run the normal offline schema and writer checks against the deployed commit,
   then restart ledger writers.

If execution stops partway, keep writers stopped and rerun the same additive
migration. Do not recover with the destructive reset. Escalate unexpected count
changes or column-type conflicts before restarting writers.

The destructive `db/schema/questdb_ledger_v1.sql` remains suitable only for a
fresh, disposable local database whose contents are intentionally being reset.

## Runtime boundaries

- QuestDB remains a bot ledger, never a historical market-data warehouse or
  per-signal context read path.
- Alpaca remains paper-only and requires explicit enablement for its separate
  reviewed workflows.
- yfinance is development-only, not production-critical.
- Phase 7 records are research-only and cannot alter real risk, sizing, model,
  broker, or execution behavior.
- PR34 does not implement Gemini or SEC collection. Provider and collector live
  tests belong to PR36 and PR38 respectively.
