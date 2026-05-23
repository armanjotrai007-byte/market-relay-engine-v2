# QuestDB Ledger Writer

PR 13 adds the first simple QuestDB writer for the V2 bot ledger. It maps
existing project records into rows for the official PR 12 ledger tables and
sends one `INSERT` at a time through QuestDB's documented `/exec` GET endpoint.

QuestDB remains bot ledger only. The writer does not create or write raw market
data tables, Databento warehouse tables, live trading records outside the ledger,
or training data.

## Write Path

The writer uses:

```text
GET /exec?query=...&fmt=json
```

In Python this is:

```python
requests.get(exec_url, params={"query": sql, "fmt": "json"}, timeout=...)
```

PR 13 intentionally does not switch to POST because the local QuestDB version
has not been proven to support POST for this path.

## Safety Guards

`QuestDBWriteConfig.max_sql_length_chars` defaults to `7000`. The guard is
checked against the encoded `/exec` GET URL, not just the raw SQL string,
because spaces, quotes, JSON punctuation, and timestamps expand when sent as
query parameters. If the encoded request is larger than the configured limit,
the writer raises `QuestDBWriteError` before sending the request. Oversized rows
are not truncated, and JSON fields are not dropped. PR 14 is expected to add
JSONL fallback for failed or oversized ledger writes; a future bulk ingestion
path can handle larger payloads later.

String literals are sanitized before SQL quoting:

- null bytes are removed
- ASCII control characters are replaced with spaces
- apostrophes are escaped by doubling

JSON fields are serialized to stable JSON first and then passed through the same
SQL string literal path. For example, `{"summary": "Fed's rate hike"}` is emitted
with `Fed''s` inside the SQL string literal.

## Supported Rows

PR 13 adds explicit mappers for feature snapshots, model signals, cost estimates,
risk decisions, context indicators, AI context events, context flags, context
state snapshots, orders, fills, trade outcomes, latency metrics, and system
health events.

`ContextStateSnapshot` is now the typed target for the
`context_state_snapshots` table. It captures the context state seen by the
deterministic risk gate without adding a future context-state cache.

Mappers generate `write_time` once when a row is built. If a caller passes an
explicit `write_time`, that exact UTC-aware value is preserved. `write_row()`
does not mutate or replace it, so future fallback replay can keep the original
attempted write time.

## Validation

Default offline validation does not require QuestDB:

```powershell
python scripts/check_questdb_writer.py
```

Required server-laptop validation writes tiny test rows after health and schema
validation:

```powershell
python scripts/check_questdb.py --required
python scripts/check_questdb_schema.py --apply --required
python scripts/check_questdb_writer.py --required
```

## Not Added

PR 13 does not add JSONL fallback, retries, queues, background writing, batching,
raw market-data writes, Databento API calls, Alpaca integration, model training,
risk engine logic, or live trading.
