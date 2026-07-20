# AGENTS.md — Repository Guidance

## Project

`market-relay-engine-v2` is a local trading research and paper-execution system.
It combines Databento market data, one canonical feature path, structured and
AI-classified research context, a deterministic Python risk gate, Alpaca paper
execution, and a QuestDB audit ledger.

Start every Codex response with `Arman,`.

## Before changing anything

1. Run `git status --short` and preserve every existing user change. Never
   reset, clean, discard, or overwrite unrelated work.
2. Read `handoff.md`, the relevant focused document under `docs/`, and the
   implementation and tests for the subsystem being changed.
3. Derive commands and conventions from this repository. Do not invent a
   formatter, linter, type checker, CI workflow, scheduler, or runtime command.
4. For complex work, state a short plan before editing. Resolve discoverable
   facts by inspection; report a precise blocker when a fact cannot be proved.
5. Keep changes narrow. Do not combine unrelated architecture layers.

See `docs/development.md` for setup, validation, generated files, and the full
definition of done.

## Repository map

- `src/market_relay_engine/`: application package.
- `tests/unit/` and `tests/integration/`: pytest coverage.
- `scripts/check_*.py`: focused validation and explicitly gated smoke tools.
- `scripts/run_tests.ps1`: repository-wide Windows validation runner.
- `config/`: committed non-secret operating configuration.
- `db/schema/`: destructive reset schema and ordered additive migrations.
- `docs/`: architecture, contracts, source, operations, and subsystem guides.
- `data/`, `data_lake/`, `logs/`: ignored local runtime output, not source.

## Setup and commands

The supported environment is Windows PowerShell with Python 3.12 or newer.
`requirements.txt` is the dependency source; `pyproject.toml` supplies package
metadata and pytest configuration but intentionally has no dependency list.

```powershell
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -e .
```

Fast offline baseline:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_environment.py
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

Run the checker and focused tests for every changed subsystem. The full runner
is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

`run_tests.ps1` calls the configured QuestDB health check, which is required by
default. A local QuestDB outage is an explicit environmental blocker, not a
reason to skip or claim the full runner passed. Normal pytest and offline
checkers must not make live source, Gemini, Alpaca, or broker calls. The runner
does not invoke every specialized Phase 7 checker, so task-relevant focused
checkers remain required.

There is currently no configured repository command for linting, formatting,
static type checking, a distributable build, or CI under `.github/workflows`.
Do not claim those checks ran. Use existing checkers, pytest, and
`git diff --check` unless a task explicitly adds reviewed tooling.

## Non-negotiable architecture

- Historical market truth comes from official Databento DBN/Parquet data.
  QuestDB is never a historical market-data warehouse.
- Historical, backtest, paper, and live paths share the canonical
  `feature_builder.py`; do not create notebook-only feature logic.
- Live DBN is decoded into stable in-memory features. Models never consume raw
  DBN bytes.
- Never query QuestDB, disk archives, or external sources in the per-tick or
  per-signal selection path. Live context reads from bounded in-memory state.
- QuestDB is metadata/audit only: signals, decisions, context metadata, orders,
  fills, latency, slippage, PnL, outcomes, and health.
- AI and external news/social context are research-only. They cannot change
  model output, approve/block/resize/delay an actual order, call Alpaca, or
  alter positions.
- The deterministic Python risk filter is the final pre-trade authority.
- Missing or stale required live context must remain conservative; do not infer
  that unavailable context is safe.
- Alpaca remains paper-first; live trading is disabled by default.
- Do not chase microsecond/sub-second alpha through Alpaca. Target realistic
  horizons such as 30 seconds to five minutes or longer, and measure execution
  quality from the first paper run.
- Treat model confidence as untrusted until calibrated and validated. Do not
  tune filters aggressively from a short sample such as one week.
- Reinforcement learning is later work only after supervised modeling, the cost
  model, paper trading, and a realistic simulator are proven.
- yfinance is development-only and never production-critical.
- Log every model signal, including blocked signals. Preserve leak-free
  timestamps, source lineage, deterministic profiles, and as-of behavior.
- Default shadow context policy remains `NO_CHANGE` unless a separately
  reviewed research policy explicitly says otherwise.

External-event rules and the PR35–PR37 compatibility boundary are documented in
`docs/external_event_ingestion.md` and `docs/context_shadow_evaluation.md`.

## Code and schema conventions

- Prefer small typed Python modules and deterministic functions over frameworks.
- Add unit tests for important rules and integration tests at contract/storage
  boundaries. Keep offline tests fixture-backed and free of real credentials.
- Reuse public contracts and existing classifier, validator, ledger converters,
  caches, and shadow evaluator. Do not create competing pipelines.
- Fail closed on ambiguous source identity, timestamps, schema drift,
  classification ownership, lifecycle order, or incomplete research coverage.
- Preserve backward-compatible fingerprints and serialized shapes unless the
  task explicitly versions them and adds regression fixtures.
- QuestDB column order is an API. When changing it, update the writer
  `TABLE_COLUMNS`, destructive reset schema, additive migration, migration
  validator, checker, tests, and docs together. Append nullable columns; do not
  broadly relax prefix/order checks.
- Use `apply_patch` for manual edits. Do not run formatters that rewrite
  unrelated files.

## Secrets, archives, and generated files

- Never print or commit `.env`, credentials, tokens, downloaded source bodies,
  provider responses, or model checkpoints. `.env.example` contains blank safe
  names only.
- The VeritaWire key name is `VERITAWIRE_API_KEY`.
- Do not inspect `.env` unless a specifically gated live check requires it.
- Do not hand-edit or commit runtime output under `data/`, `data_lake/`,
  `logs/`, `archives/`, `manifests/`, local QuestDB directories, caches,
  `__pycache__/`, `.pytest_cache/`, `build/`, `dist/`, or `*.egg-info/`.
- Tiny deterministic fixtures under `tests/fixtures/` are source files. They
  must be synthetic or sanitized and contain no real credentials or downloaded
  confidential payloads.
- Do not edit generated archive manifests/checkpoints to make a test pass; fix
  the owning code or fixture.

## Done when

A task is complete only when:

- The requested behavior and relevant docs agree, with no unrelated behavior
  change.
- Architecture, research-only, secret, and paper-trading boundaries still hold.
- Important success, failure, restart, and as-of cases have deterministic tests.
- Focused checkers/tests pass; full pytest passes when the task warrants it.
- `scripts/run_tests.ps1` passes when its required local services are available,
  or the exact environmental blocker and the checks that did pass are reported.
- `git diff --check` passes and the complete diff is reviewed.
- `git status --short` contains no credentials, downloaded data, caches, or
  accidental files.
- The final handoff lists files changed, commands run, results, blockers,
  limitations, and a concrete follow-up when work remains.
