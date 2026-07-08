# Context Source Smoke Testing

PR33 adds two complementary validations for decision-context inputs.

The deterministic integration tests are offline software-correctness checks. They prove fixed cache evidence and fixture-backed collector output can flow through `ContextStateCache` into `DecisionContextAssembler.build_for_decision(...)` and produce JSON-safe audit payloads.

The live source smoke script is a manual server-only operational check. It verifies configured source request/parsing paths can currently be reached, can materialize fresh in-process cache entries when data exists, and can pass those entries through the decision-context assembler. PR33 allows functional live context-source validation for built structured sources while preserving separate trading safety boundaries.

PR33 validates collector-to-cache-to-decision-context assembly. It does not validate context consumption by the risk filter, because that integration remains deliberately deferred.

## Manual-Only Boundary

`scripts/smoke_context_sources.py` is intentionally excluded from pytest, CI, scheduler flows, service startup, and ordinary checker scripts. It must be run only from an isolated server-side validation checkout or worktree, never from the active 24/7 service checkout.

The operator must pass the active server environment's `.env` by absolute path with `--env-file`. The file is never auto-discovered and must never be copied into the validation worktree. Values from the explicitly supplied file take precedence over inherited shell environment variables for that validation invocation.

The script refuses live setup unless both `--live` and an absolute existing `--env-file` are supplied. Its help path does not load `.env`, instantiate collectors, import live configuration, or contact sources.

## State And Writes

Each source uses a fresh in-process `ContextStateCache`. No active service cache, runtime state, scheduler, or process is read or modified.

Ordinary `--live --env-file` mode uses `write_questdb=False` and `questdb_required=False`. It performs no QuestDB writes and no fallback-ledger writes.

`--live --questdb --env-file` is an explicit operator decision. In that mode the script first reuses the existing QuestDB health/config, `QuestDBLedgerWriter`, `SystemHealthEvent`, and `QuestDBLedgerReader` contracts to persist and read back a clearly tagged `context_source_smoke` validation marker. It then requests each enabled source collector's existing QuestDB writer path with generated validation run/session IDs and reads back the collector-reported ledger tables through the existing read-only reader.

A successful QuestDB marker alone is not a successful context-source validation. In `--live --questdb` mode, at least one non-marker source must either complete source-specific `WRITTEN_READBACK` or return valid `EXPECTED_NO_DATA` with `NO_CONTEXT`. A materialized source configured not to write QuestDB is a failure in explicit `--questdb` mode. The standalone QuestDB checks remain the correct tools for database-only validation.

PR33 QuestDB validation preserves clearly tagged validation rows. It does not delete them, and it must never run destructive schema apply against the active server ledger.

USAspending uses a temporary checkpoint path under the isolated validation worktree so the real checkpoint lock and atomic write behavior are exercised without touching the production checkpoint path or the active service `data` directory.

## Validation Configuration

The repository config may enable the built PR33 structured context sources for functional connectivity. That does not enable live trading, direct AI orders, yfinance production-critical usage, QuestDB decision-loop reads, or context consumption by the risk filter.

Macro calendar is enabled by `structured_sources.macro_calendar.enabled` in `config/context_sources.yaml`. If the smoke output reports `SKIPPED_DISABLED`, the parsed value at that exact path is false.

FRED is enabled by `structured_sources.fred.enabled` and requires the configured FRED API key environment variable in the explicitly supplied `.env`.

Yfinance development-only material is enabled by `structured_sources.yfinance_dev_only.enabled`. A successful check proves only development-only connectivity/data material; it does not make yfinance eligible for approved risk context.

EIA WPSR numeric validation requires both `structured_sources.eia.enabled` in `config/context_sources.yaml` and `calendar_events.event_windows.eia.enabled` with reviewed `releases` in `config/calendar_events.yaml`. Numeric reachability also requires the configured EIA source key environment variable, such as `EIA_API_KEY`, in the explicitly supplied `.env`. Enabling only the numeric source without reviewed release windows is a configuration failure, not proof that the EIA API was reached.

USAspending validation requires `structured_sources.usaspending.enabled` and a reviewed recipient map at the configured `recipient_map_path` for award materialization. The committed default map is intentionally empty; when `validation_modes.usaspending.allow_health_only_without_recipient_mapping` is true, the smoke script may perform source-health-only connectivity validation without fake UEIs or award searches. Do not invent UEIs or point the smoke script at the production checkpoint path.

## Outcomes

`PASS` means an enabled source completed its real bounded request/parsing path or local artifact validation, materialized cache entries, and those entries were selected by the decision-context assembler with a JSON-safe audit payload.

`EXPECTED_NO_DATA` means an enabled source completed the real path but returned a documented valid empty/no-current-data result, such as no active macro events, no fresh EIA/yfinance data, stale-but-parsed FRED observations, or a successful USAspending search with no award material.

`SKIPPED_DISABLED` means the existing configuration disables that source capability, so no collector/probe/network setup was attempted.

`FAILED` means required enabled-source configuration, authentication, transport, parser, materialization, temporary checkpoint, collector, or assembler validation failed. Enabled scheduler-style non-attempt outcomes are failures, not successful smoke results.

The `source_ledger` column is separate from source outcome. `NOT_REQUESTED` means ordinary no-write mode. `WRITTEN_READBACK` means source-specific collector ledger writes were read back for the generated validation identity and the current collector's canonical source scope, so one source cannot satisfy another source's persistence check. `NO_CONTEXT` means the source returned a valid no-data result or no materialized context to persist. `NOT_CONFIGURED` means deployed source configuration does not enable that collector's ledger output. `FAILED` means a requested source-specific ledger write or read-back failed.

## Source Notes

Macro calendar is a local reviewed-artifact validation, not an online API health claim.

EIA WPSR has two distinct paths: local release-window material and numeric remote WPSR data. Numeric EIA reachability is claimed only when the explicit numeric probe completes the real bounded EIA request/parser/materialization path.

FRED, USAspending, and yfinance development-only checks use the existing collector/client code paths instead of fake ping endpoints or duplicate parsers. USAspending health-only mode reuses the existing source-health request and `last_updated` parser.

Yfinance material is tested only as development-only connectivity/data material. It remains permanently ineligible for `approved_risk_context`.

QuestDB remains bot-ledger-only. Context source collection may write validation or ledger rows only when explicitly requested by the relevant script mode; per-tick and per-signal decisions must read context from the in-memory cache, not QuestDB.

`all_structured_context` contains selected research-only, development-only, unknown, and approved entries. `approved_risk_context` contains only exact-policy-approved, centrally registered, known non-development entries; no PR33 test or document treats `all_structured_context` as currently risk-consumable.
