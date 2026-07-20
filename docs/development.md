# Development and Validation

This guide records commands and conventions that are present in this
repository. It does not define a new build or release process.

## Supported local environment

- Windows PowerShell.
- Python 3.12 or newer (`pyproject.toml` requires `>=3.12`).
- A repository-local `.venv`.
- Dependencies installed from `requirements.txt`.
- Editable package installation from `pyproject.toml`.

Create a fresh environment from the repository root:

```powershell
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -e .
```

Using the explicit interpreter avoids accidentally running checks with a
different global Python. Activating `.venv` is optional.

`requirements.txt` is authoritative for dependencies. The `[project]`
dependency list in `pyproject.toml` is currently empty, so `pip install -e .`
alone is insufficient.

## Build and tooling status

The editable install above is the configured package setup/build operation.
There is no reviewed command for building wheel or source-distribution
artifacts.

The repository currently has no configured:

- Linter.
- Code formatter.
- Static type checker.
- Coverage threshold.
- Pre-commit configuration.
- GitHub Actions workflow under `.github/workflows`.

Do not invent or claim any of those checks. `git diff --check`, focused
repository checkers, and pytest are the available verification tools. Adding a
tool is a separate reviewed behavior/tooling change, not a documentation fix.

## Test and checker commands

Fast offline baseline:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_environment.py
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

Run one test module while iterating:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_research_projection.py
```

Run a test by node ID:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_research_projection.py::test_name
```

Replace the example path/name with a test that actually exists. Pytest settings
come from `pyproject.toml`: tests live under `tests/`, `src/` is on the import
path, and output is quiet by default.

Focused checkers are ordinary Python entry points under `scripts/check_*.py`.
Use the checker belonging to each changed subsystem. Core examples are:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" scripts/check_gemini_context.py
& ".\.venv\Scripts\python.exe" scripts/check_sec_edgar.py
& ".\.venv\Scripts\python.exe" scripts/check_context_shadow_evaluation.py
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_writer.py
```

Checkers are offline by default unless their focused documentation explicitly
says otherwise. A script absent from the checked-out branch cannot be replaced
with a guessed command.

The repository-wide Windows runner is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

The runner prefers `.venv\Scripts\python.exe` and falls back to `python`. It
executes environment/configuration validation, the configured QuestDB health
check, schema/writer/analysis checks, contracts and fixtures, market-data and
feature checks, cost/label/risk checks, execution checks, context checks, and
the full pytest suite.

It does not currently invoke every specialized Phase 7 checker. Run the
task-relevant Gemini, SEC, shadow-evaluation, external-source, or other focused
checker explicitly even when the full runner passes.

Repository configuration makes QuestDB health required. Consequently,
`run_tests.ps1` is not a service-free offline command. If QuestDB is unavailable:

1. Record that exact environmental blocker.
2. Run and report the relevant offline checkers and full pytest separately.
3. Do not report the full runner as passing.

## Live checks

Network, Gemini, QuestDB writes, and broker-adjacent work require explicit
flags. A normal checker or pytest run must not enable them.

Examples already provided by the repository include:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_gemini_context.py --live --required
& ".\.venv\Scripts\python.exe" scripts/check_sec_edgar.py --live --ticker LMT --form 8-K --max-filings 1
& ".\.venv\Scripts\python.exe" scripts/check_yfinance_proxy.py --live
```

External news/social commands and their independent `--classify` and
`--questdb` gates are documented in `external_event_ingestion.md`.

Before any live check:

- Read the focused source document and `--help` output.
- Use a finite timeout and bounded item count.
- Confirm which service calls and local writes it performs.
- Load credentials only from the ignored `.env` and never print them.
- Do not infer broker or trading authority from source connectivity.

## Working-tree and file conventions

Always inspect `git status --short` before editing. Existing changes belong to
the user unless proved otherwise. Do not reset, clean, restore, or overwrite
them.

Source files normally edited and committed:

- Python under `src/market_relay_engine/`.
- Deterministic tests and sanitized fixtures under `tests/`.
- Checkers and explicit tools under `scripts/`.
- Non-secret configuration under `config/`.
- Reviewed SQL under `db/schema/`.
- Documentation under `docs/` and repository root Markdown.

Ignored/generated files that agents must not hand-edit or commit:

- `.env`, other local environment files, and `.venv/`.
- `data/`, `data_lake/`, `logs/`, `archives/`, and `manifests/` contents except
  committed `.gitkeep` placeholders.
- Emergency JSONL, spool/dead-letter records, source archives, checkpoints,
  mutable manifests, and downloaded DBN/Parquet/source documents.
- QuestDB binaries/data directories and local SQLite/database files.
- `__pycache__/`, `.pytest_cache/`, coverage/lint/type caches, `build/`, `dist/`,
  and `*.egg-info/`.
- Model checkpoints unless a task explicitly approves a reviewed artifact.

Tiny fixtures may be committed only when deterministic, synthetic or manually
sanitized, and free of real credentials and unneeded source text.

## QuestDB change convention

QuestDB table column order is validated as a contract. A schema change is not
complete unless these move together:

- Writer `TABLE_COLUMNS` and row converter.
- Destructive reset schema for disposable development databases.
- Ordered additive, non-destructive migration for persistent ledgers.
- Migration validator and `scripts/check_questdb_schema.py`.
- Writer/schema tests and focused documentation.

Preserve the existing column prefix and append reviewed nullable suffixes. Do
not weaken ordering checks merely to accept a mismatch. Never apply the
destructive reset schema to a persistent ledger.

## Change workflow

1. Inspect status, handoff, focused docs, implementation, and tests.
2. State a short implementation plan for nontrivial work.
3. Make the smallest coherent change and preserve public compatibility unless
   an explicit versioned change is required.
4. Add deterministic tests for success, rejection, restart/interruption, and
   as-of behavior where relevant.
5. Run focused checkers/tests, then full pytest.
6. Run the PowerShell runner when its required services are available.
7. Run `git diff --check`, inspect the complete diff, and inspect final status.
8. Update focused documentation and handoff.

## Definition of done

A future Codex session should not call work complete until:

- Requested behavior is implemented without unrelated application changes.
- Architecture, paper-trading, research-only, and secret boundaries are intact.
- Tests cover the important rules and regressions introduced by the task.
- Focused checks and full pytest pass.
- Full PowerShell validation passes, or an exact environmental blocker is
  listed with all successfully completed substitute checks.
- No credentials, downloaded data, generated archives, caches, or local
  database state appears in `git status --short`.
- `git diff --check` passes and the complete diff has been reviewed.
- Relevant docs explain configuration, operation, limitations, and recovery.
- Final delivery lists files changed, verification commands and results,
  blockers, known limitations, and recommended follow-up.

## Known repository-level unknowns

- There is no committed CI workflow, so local validation is the only documented
  verification authority.
- There is no configured lint, format, typecheck, packaging-build, or coverage
  acceptance command.
- Persistent QuestDB backup/restore policy is operator-local; the repository
  documents safe migration ordering but does not automate backups.
