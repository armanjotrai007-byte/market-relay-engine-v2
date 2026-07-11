# market-relay-engine-v2

`market-relay-engine-v2` is a local AI-assisted trading research and future paper/live execution system. It will eventually combine Databento market data, structured context, AI-interpreted context flags, a deterministic Python risk gate, Alpaca execution, and a QuestDB bot ledger.

GitHub is the official source of truth for this project. The actual trading laptop is a separate machine; it must pull this repository from GitHub and run the committed PowerShell validation commands locally. Do not rely on hidden local files, manual setup, or uncommitted changes.

## PR 1 Scope

This first PR establishes the clean, portable skeleton for Trading System V2:

- Python package layout under `src/market_relay_engine`
- Minimal project metadata and dependency files
- Safe placeholder environment and YAML config files
- Initial documentation for architecture, contracts, risk rules, weekly analysis, and runbook usage
- Local environment health check script
- PowerShell test runner
- Basic unit tests for imports, config files, YAML loading, and UTC time helpers
- Empty tracked data/log directories through `.gitkeep` files

## Not Included In PR 1

PR 1 intentionally does not include:

- Databento connectivity or DBN ingestion
- Alpaca broker connectivity
- QuestDB connection logic, schemas, or market-data ingestion
- EIA, FRED, USAspending, SEC EDGAR, yfinance, or other live API calls
- Model training or inference
- Reinforcement learning
- Notebook-only logic
- Live trading

No external services or APIs are touched by PR 1.

## Windows PowerShell Setup

Run these commands from a fresh clone on Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
pytest
python scripts/check_environment.py
python scripts/check_config.py
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

## Validation

The environment health check verifies Python 3.12+, required files, required directories, config placeholders, `.env.example`, and package imports:

```powershell
python scripts/check_environment.py
```

The full local validation runner executes the environment health check, config
validation, config-required QuestDB health check, QuestDB schema validation, QuestDB
writer validation, QuestDB analysis validation, contract validation, fixture
validation, local market-data checks, feature builder checks, feature parity
checks, cost model checks, label builder checks, and then pytest:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

These same commands must be run on the separate trading laptop after it pulls from GitHub.

## Configuration

Trading System V2 config files live under `config/`. They separate the final
10-stock tradable universe (`PLTR`, `LMT`, `RTX`, `GD`, `AVAV`, `XOM`, `OXY`,
`SLB`, `COP`, and `VLO`) from context symbols. The built structured sources are
enabled for bounded, explicit collection outside the per-tick loop; yfinance
remains development-only, unstructured sources and the AI context filter remain
disabled, QuestDB remains ledger-only, and live trading remains disabled.

Validate configuration locally with:

```powershell
python scripts/check_config.py
```

See `docs/configuration.md` for the config file map and safety rules.

PR26 adds a one-shot EIA WPSR collector for reviewed release-window flags and
sector-level `OIL` numeric research context. Repository configuration
intentionally enables it for explicit collection, but does not schedule it or
grant it independent trading authority. Run
`python scripts/check_eia_wpsr.py` for its offline fixture check and see
`docs/eia_wpsr.md` for schedule, timing, provenance, and live-read guidance.

## Phase 7 PR34 Contracts

PR34 defines provider-neutral raw-input, source-document, classification,
validation, research event/flag, and hypothetical shadow-evaluation contracts.
It also adds metadata-only QuestDB schemas for classification attempts and
shadow evaluations. The path is research-only: it does not call Gemini or SEC
EDGAR, archive documents, build a research cache, execute a shadow policy, or
change a real `RiskDecision`.

AI classification uses strict SEC 8-K/general event values. Deterministic Form 4
purchase/sale values have a separate enum and are not valid classification
responses. Only a bounded in-memory request may carry an excerpt; QuestDB must
not store source bodies, request excerpts, prompts, or full provider exceptions.

See `docs/data_contracts.md` and `docs/architecture.md`.

## QuestDB Health Check

The QuestDB health check validates the local HTTP `/exec` endpoint with
`SELECT 1`. Repository configuration intentionally requires the local service
by default:

```powershell
python scripts/check_questdb.py
```

`--required` makes that intent explicit on the server laptop:

```powershell
python scripts/check_questdb.py --required
```

See `docs/questdb_health.md` for the bot-ledger-only scope and PR34 migration
boundary.

## QuestDB Ledger Schema

The official QuestDB V2 ledger schema lives at:

```powershell
db/schema/questdb_ledger_v1.sql
```

It is a destructive local-development reset for the bot ledger only. It drops
old raw/PDF-era table names, drops existing V2 ledger tables, and recreates the
V2 ledger schema. QuestDB must not store raw Databento market data or act as a
historical market-data warehouse.

Run the offline schema validation without QuestDB:

```powershell
python scripts/check_questdb_schema.py
```

Do not use the reset against a persistent ledger. After PR34 is merged, stop
writers and upgrade an existing server with the idempotent additive migration:

```text
db/schema/questdb_pr34_add_phase7_context_ledger.sql
```

Record pre/post counts for `context_ai_events` and `context_flags`, rerun the
migration to prove idempotency, and confirm the counts are unchanged before
restarting writers. Never use `scripts/check_questdb_schema.py --apply` as a
persistent-ledger migration. See `docs/live_runbook.md` and
`docs/questdb_schema.md` for the exact operator procedure.

## QuestDB Ledger Writer

The QuestDB ledger writer maps project records into the current V2 ledger
tables and writes one safe `INSERT` at a time through the documented `/exec` GET
path. Offline validation does not require QuestDB:

```powershell
python scripts/check_questdb_writer.py
```

On the server laptop, run the required writer check only after health and schema
validation:

```powershell
python scripts/check_questdb_writer.py --required
```

See `docs/questdb_writer.md` for SQL length limits, escaping behavior, and PR 14
fallback notes.

## QuestDB Ledger Analysis

The QuestDB ledger analysis reader is read-only. It runs small `SELECT`/`WITH`
queries against existing V2 ledger tables through the documented `/exec` GET
path and produces basic counts, slippage, PnL, risk, and system health
summaries. It does not modify schema, write rows, tune risk, train models, or
trade.

Validate analysis behavior without QuestDB:

```powershell
python scripts/check_questdb_analysis.py
```

On the server laptop, after required health, schema, and writer checks:

```powershell
python scripts/check_questdb_analysis.py --required
```

See `docs/questdb_analysis.md` for read-only SQL guardrails and scope.

## Core Contracts

Core dataclass contracts live under `src/market_relay_engine/contracts`. They
define lightweight frozen record shapes, strict string enums, UTC timestamp
standards, UUID-based IDs, hash references, defensive collection copying, and
JSON serialization for market, feature, model, risk, context, execution,
ledger, and system-health layers.

Validate contract examples locally with:

```powershell
python scripts/check_contracts.py
```

## Test Fixtures

Reusable fake fixtures now live under `tests/fixtures/`. They provide stable
sample records and scenarios for tests without using real Databento DBN files or
external services.

Validate them locally with:

```powershell
python scripts/check_fixtures.py
```

See `docs/testing_fixtures.md` for fixture scope, scenario descriptions, and the
fake-data safety rules.

## Historical Parquet Reader

The local historical Parquet reader normalizes small local Parquet samples into
`MarketRecord` objects for future Databento historical workflows. Test Parquets
are generated fake files for reader mechanics only; they are not official
Databento schema fixtures. See `docs/historical_parquet_reader.md`.

Validate the reader without real market data:

```powershell
python scripts/check_historical_parquet.py
```

## DBN Inspection Utility

The local DBN inspection utility summarizes ignored Databento `.dbn` and
`.dbn.zst` files or batch folders without Databento cloud/API calls. File-info
mode works without the optional Databento package and does not read DBN record
contents. See `docs/dbn_inspection.md`.

Validate the inspector without real market data:

```powershell
python scripts/check_dbn_inspector.py
```

## Canonical Feature Builder

The canonical feature builder converts normalized `MarketRecord` objects into
`FeatureSnapshot` objects through one shared path for historical and future live
use. V1 features live inside `FeatureSnapshot.features`; the builder computes
basic quote normalization and small rolling-window features without DBN parsing,
QuestDB writes, model logic, or trading behavior. See
`docs/feature_builder.md`.

Validate the builder without external services:

```powershell
python scripts/check_feature_builder.py
```

## Historical/Live Feature Parity

Historical-style and live-style feature paths both use the canonical
`FeatureBuilder`. PR 8 adds deterministic parity helpers and tests for
equivalent event-time-ordered `MarketRecord` inputs, while keeping live-style
processing in caller arrival order. See `docs/feature_parity.md`.

Validate parity without external services:

```powershell
python scripts/check_feature_parity.py
```

## Cost Model V1

The cost model estimates whether a mid-to-mid expected move exceeds spread,
round-trip slippage, size penalty, missed-fill risk, and the minimum edge
buffer. PR 9 keeps this as a pure calculation module without labels, risk
logic, broker execution, QuestDB writes, live data, or external APIs. See
`docs/cost_model.md`.

Validate the cost model without external services:

```powershell
python scripts/check_cost_model.py
```

## Label Builder

The label builder creates deterministic cost-aware labels for future supervised
training. It combines an existing `FeatureSnapshot`, a future normalized
midprice observation at `1m`, `5m`, or `15m`, regular-hours protection, and the
PR 9 cost model to produce `profitable_after_costs`. PR 10 does not train a
model, run inference, call external APIs, read real market files, write QuestDB
records, or place broker orders. See `docs/label_builder.md`.

Validate the label builder without external services:

```powershell
python scripts/check_label_builder.py
```

## Safety Defaults

The repository defaults to local development and future paper trading. `.env.example` contains placeholder variable names only. Do not commit real secrets, live credentials, logs, Databento DBN files, Parquet data, QuestDB data folders, or generated API exports.

QuestDB is reserved for the bot ledger and black-box recorder: signals, risk
decisions, context metadata and flags, classification attempts, hypothetical
shadow evaluations, orders, fills, slippage, latency, PnL, outcomes, and system
health. It must not be used as a historical market-data warehouse or store full
filings, articles, social posts, normalized documents, prompts, request excerpts,
credentials, or full provider exceptions.

AI and external context have no direct trade, block, delay, or sizing authority.
The deterministic Python risk filter remains the final pre-trade authority, and
Alpaca remains paper-first.
