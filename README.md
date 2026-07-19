# market-relay-engine-v2

`market-relay-engine-v2` is a local AI-assisted trading research and
paper-execution system. It combines official Databento market data, one
canonical feature path, structured and AI-classified research context, a
deterministic Python risk gate, Alpaca paper execution, and a QuestDB bot
ledger.

GitHub is the project source of truth. The trading laptop is a separate machine
that must pull committed code and run the repository validation locally. Never
depend on hidden setup or uncommitted source changes.

## Safety boundary

- Historical market truth is official Databento DBN/Parquet data, not QuestDB.
- Historical and live processing use the same canonical feature builder.
- Per-tick and per-signal paths read bounded in-memory state; they do not query
  QuestDB, disk archives, or network sources.
- QuestDB stores bot/audit metadata, not raw market history, source documents,
  classifier inputs, prompts, or provider bodies.
- AI context, SEC filings, company news, earnings releases, and social posts are
  research-only. They cannot alter model output, real risk decisions, order
  timing or size, Alpaca calls, or positions.
- The deterministic Python risk filter remains the final pre-trade authority.
- Alpaca is paper-first and live trading is disabled by default.
- The default shadow context policy is `NO_CHANGE`.

See [architecture](docs/architecture.md), [data contracts](docs/data_contracts.md),
and [agent guidance](AGENTS.md) before changing those boundaries.

## Repository layout

```text
config/                    committed non-secret operating configuration
db/schema/                 QuestDB reset schema and additive migrations
docs/                      architecture, source, and operating guides
scripts/                   focused checkers and explicit runtime tools
src/market_relay_engine/   Python package
tests/unit/                deterministic component tests
tests/integration/         subsystem-boundary tests
data/, data_lake/, logs/   ignored local runtime output
```

## Windows PowerShell setup

Python 3.12 or newer is required. `requirements.txt` is the dependency source;
`pyproject.toml` supplies package metadata and pytest settings.

```powershell
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -e .
```

The editable install is the repository’s setup/build step. No separate
distributable-build command is configured.

## Validation

Fast offline baseline:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_environment.py
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

Run the focused checker and tests for each changed subsystem. Important Phase 7
offline checks include:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" scripts/check_gemini_context.py
& ".\.venv\Scripts\python.exe" scripts/check_sec_edgar.py
& ".\.venv\Scripts\python.exe" scripts/check_context_shadow_evaluation.py
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_writer.py
```

The external-source checker is offline by default. If it is unavailable on the
checked-out branch, that pilot has not landed yet; do not substitute an
invented command.

Repository-wide validation:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

The full runner invokes the configured QuestDB health check, which is required
by default. If local QuestDB is unavailable, report that environmental blocker
and separately report the offline checks that passed.

No repository linter, formatter, static type checker, or GitHub Actions
workflow is currently configured. Do not claim those checks ran. More detail is
in [Development and validation](docs/development.md).

## Configuration and secrets

Configuration lives under `config/` and is validated by
`scripts/check_config.py`. The current tradable universe is:

```text
PLTR LMT RTX GD AVAV XOM OXY SLB COP VLO
```

None is approved for live trading by default. EIA, FRED, USAspending, the local
macro calendar, and the yfinance development proxy support bounded explicit
collection outside the decision loop. Unstructured sources and automatic AI
classification remain disabled by default.

Copy only needed blank names from `.env.example` into the ignored `.env`.
Never commit or print `.env`. Relevant Phase 7 names include:

```text
GEMINI_API_KEY
SEC_ORGANIZATION
SEC_CONTACT_EMAIL
VERITAWIRE_API_KEY
```

`VERITAWARE_API_KEY` is a spelling error and is not a supported committed name.
See [Configuration](docs/configuration.md).

## Current Phase 7 research flow

```text
source archive
-> ContextRawInput
-> ContextSourceDocument
-> ContextClassificationRequest
-> existing Gemini classifier and validator
-> validated ContextAIEvent
-> explicit bounded ResearchEvidence preparation
-> in-memory as-of selection
-> ShadowContextPolicyEvaluation (default NO_CHANGE)
```

PR35 owns the strict Gemini classifier. PR36 owns the content-addressed SEC
archive and collector. PR37 owns leak-free bounded research evidence selection
and the shadow evaluator. The external-event pilot extends those seams for:

- Donald Trump Truth Social posts delivered by VeritaWire.
- Lockheed Martin’s official all-news RSS and linked releases.
- Palantir’s official investor-relations release endpoint.
- Official PLTR and LMT earnings releases.

External records retain source lifecycle revisions, independent readiness
times, canonical classification ownership, non-merging relationships, explicit
coverage, and union ticker/sector/global scope. Details and gated commands are
in [External event ingestion](docs/external_event_ingestion.md).

## QuestDB

QuestDB is a metadata-only black-box recorder. Health uses the local HTTP
`/exec` endpoint and is required by repository configuration:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_questdb.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb.py --required
```

`db/schema/questdb_ledger_v1.sql` is a destructive reset for a disposable local
ledger. Never use it to upgrade a persistent server. Apply reviewed additive
migrations in file order with writers stopped, then run schema and writer
checks. See [QuestDB schema](docs/questdb_schema.md),
[writer](docs/questdb_writer.md), and [live runbook](docs/live_runbook.md).

## Focused documentation

- [Development and validation](docs/development.md)
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Data contracts](docs/data_contracts.md)
- [External event ingestion](docs/external_event_ingestion.md)
- [SEC EDGAR](docs/sec_edgar.md)
- [Shadow context evaluation](docs/context_shadow_evaluation.md)
- [QuestDB schema](docs/questdb_schema.md)
- [Live runbook](docs/live_runbook.md)
- [Testing fixtures](docs/testing_fixtures.md)

## Contribution completion criteria

Before handing off a change:

- Preserve unrelated work and review the complete diff.
- Add deterministic tests for important success, failure, restart, and as-of
  behavior.
- Run focused checks, full pytest, `git diff --check`, and the full PowerShell
  runner when its required local services are available.
- Confirm no secret, downloaded source body, generated archive, cache, or local
  database is tracked.
- Update the relevant focused documentation.
- Report files changed, verification and results, explicit blockers, known
  limitations, and concrete follow-up work.
