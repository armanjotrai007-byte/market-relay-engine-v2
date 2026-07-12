# QuestDB Ledger Writer

`QuestDBLedgerWriter` maps typed project records into the bot-ledger tables and
sends one bounded SQL insert at a time through the configured QuestDB HTTP
endpoint. It does not create a market-data warehouse, query QuestDB inside a
decision loop, call providers, or make risk/trading decisions.

## Safety guards

The writer checks the encoded `/exec` URL against
`QuestDBWriteConfig.max_sql_length_chars` before sending. Oversized rows are
rejected rather than truncated. Strings remove null bytes, replace ASCII
control characters, and escape apostrophes; JSON fields are stably serialized
before SQL quoting.

The separate emergency JSONL fallback preserves a versioned attempted ledger
row after a primary write failure. It is not a successful QuestDB insert and
does not add replay, retry, batching, or transaction semantics. The same raw
content restrictions apply to fallback rows.

## Phase 7 mappings

PR34 adds mappings and writer methods for:

- `ContextClassificationRequest` + `ContextClassificationResponse` + optional
  `ContextValidationResult` -> `context_classification_attempts`
- `ShadowContextPolicyEvaluation` ->
  `shadow_context_policy_evaluations`
- enriched `ContextAIEvent` -> `context_ai_events`
- enriched or legacy `ContextFlag` -> `context_flags`

Cross-record request/attempt IDs must agree. Enum fields are written as their
stable strings, ticker/event/flag/reason-code lists are compact JSON arrays,
and UTC timestamps remain explicit. Unknown columns are rejected against the
exact `TABLE_COLUMNS` definition.

The classification mapper never copies `ContextClassificationRequest.input_text`
to a row. It writes only IDs, hashes, source metadata, concise output,
validation facts, latency, and safe failure fields. Full documents, excerpts,
prompts, exceptions, tracebacks, secrets, and credentials are prohibited.

Provider failures expose only `safe_failure_category` and optional
`safe_failure_summary`. PR36 must record the full exception and traceback in
retained ignored local structured logs, redact credentials, and correlate those
logs with the same `classification_attempt_id`; PR34 does not implement that
runtime logging.

Shadow rows contain hypothetical research output only. A `BLOCK`,
`REDUCE_SIZE`, or `DELAY` value cannot alter a real `RiskDecision` or execution
path.

## Existing-row compatibility

Existing event/flag field sets and legacy ledger rows remain supported while
PR35 is pending; `ContextAIEvent` callers must now use the strict event, risk,
and urgency enums. Nullable Phase 7 metadata maps to SQL null, and legacy EIA
flags continue to populate established columns. When legacy provenance is
present, the publishing adapter validates top-level and companion
cache-provenance `available_at` values before it calls the writer; the writer
itself receives the typed flag, not cache details.

## Validation

Offline validation requires no QuestDB connection:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_questdb_writer.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_questdb_writer.py
```

Before using a PR34 writer against an existing persistent server, apply the
idempotent additive migration in
`db/schema/questdb_pr34_add_phase7_context_ledger.sql` using the procedure in
`docs/live_runbook.md`. Never use the destructive reset as an upgrade.

The writer still does not add provider calls, queues, background writing,
collector networking, model behavior, real risk integration, broker behavior,
or live trading.
