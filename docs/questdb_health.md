# QuestDB Health Check

PR 11 adds the first QuestDB integration foundation for V2. It proves the repo
can load local QuestDB HTTP settings and check whether QuestDB is ready for the
future bot ledger.

QuestDB remains the bot ledger and black-box recorder only. It is for bot runs,
feature snapshots, model signals, cost estimates, risk decisions, context flags,
orders, fills, trade outcomes, latency metrics, system health, and future ledger
write failures.

QuestDB is not a historical market-data warehouse. Raw Databento trades, BBO,
MBP, OHLCV, bulk historical market data, and training market truth stay in local
official Databento DBN/Parquet files.

## Local Defaults

Default values:

```text
QUESTDB_HTTP_SCHEME=http
QUESTDB_HTTP_HOST=localhost
QUESTDB_HTTP_PORT=9000
QUESTDB_HEALTH_TIMEOUT_SECONDS=3
QUESTDB_PG_HOST=localhost
QUESTDB_PG_PORT=8812
```

Configuration precedence is:

```text
explicit script/function overrides
-> environment variables / .env
-> config/questdb.yaml
-> hardcoded defaults
```

## Health Check

Run the optional offline check:

```powershell
python scripts/check_questdb.py
```

Run the required server-laptop integration check with QuestDB running:

```powershell
python scripts/check_questdb.py --required
```

Codex and offline validation use optional mode because QuestDB may not be
running. Optional mode exits 0 when QuestDB is unavailable, but it distinguishes
connection failures from unhealthy responses:

- `[SKIP]` means QuestDB was not reachable at all.
- `[WARN]` means QuestDB responded but the `/exec` result was unhealthy.
- `[PASS]` means the SQL query engine accepted `SELECT 1`.

Required mode exits nonzero with `[FAIL]` for either unreachable or unhealthy
QuestDB.

The health check intentionally uses `/exec` with `SELECT 1` instead of only
`/health` because the future ledger writer needs the SQL query engine to be
ready, not merely the HTTP process to be alive.

## Not Added In PR 11

PR 11 does not add:

- SQL schema creation
- table creation
- inserts or writer classes
- JSONL fallback writing
- raw Databento market-data tables
- historical market-data warehouse behavior
- Databento API integration
- Alpaca or broker execution
- model training or inference
- risk engine logic
- live trading

## PR 12 Note

PR 12 is expected to add the QuestDB Ledger Schema SQL. Before PR 13 adds a
ledger writer, PR 12 should resolve the meaning of
`risk_decisions.context_snapshot_id`.

If PR 12 only defines `context_indicator_snapshots`, `context_ai_events`, and
`context_flags`, then `risk_decisions.context_snapshot_id` has no single target
table. The preferred PR 12 solution is a `context_state_snapshots` table that
captures one context state for a decision, with fields such as:

```text
snapshot_time
context_snapshot_id
ticker
active_indicator_ids_json
active_context_event_ids_json
active_context_flag_ids_json
context_summary_json
valid_until
run_id
session_id
schema_version
trace_id
```

PR 11 only carries this warning forward. It does not create the table or any
other schema.
