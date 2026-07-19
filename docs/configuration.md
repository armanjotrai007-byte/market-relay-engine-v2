# Configuration

Trading System V2 configuration files live under `config/`.

Each file is safe to load locally without broker access, QuestDB writes, or live market data. Enabled online context sources still require their referenced environment variables and should be validated with the focused source-smoke tools, not by committing secrets.

## Files

- `symbols.yaml` defines the final 10-stock universe (`PLTR`, `LMT`, `RTX`, `GD`, `AVAV`, `XOM`, `OXY`, `SLB`, `COP`, and `VLO`) and separate context symbols. None of the tradable symbols is approved for live trading. PR25 uses fixed context proxy groups for SPY, QQQ, IWM, GLD, `^VIX`, XLE, XOP, OIH, XLI, PPA, and ITA.
- `context_sources.yaml` defines structured and unstructured context source
  settings. EIA, FRED, USAspending, the local macro calendar, and
  `yfinance_dev_only` are intentionally enabled for bounded use outside the
  per-tick loop. Yfinance remains development-only and not production critical.
  SEC EDGAR, VeritaWire/Truth Social, LMT RSS, PLTR IR, company earnings, and
  automatic AI-context classification remain disabled by default and require
  explicit research commands.
- `sec_edgar_tickers.yaml` is the reviewed, deterministic ticker/issuer/zero-padded-CIK map for the ten approved symbols. It is not inferred by an AI model.
- `risk_limits.yaml` defines placeholder paper-trading risk limits. These are not optimized live settings.
- `questdb.yaml` defines QuestDB connection/health defaults and the exact ledger
  table allow-list. PR34 adds `context_classification_attempts` and
  `shadow_context_policy_evaluations`; both store metadata only. QuestDB remains
  a bot ledger, not a historical market-data or raw-context warehouse.
- `model_config.yaml` defines placeholder feature, model, calibration, horizon, and label settings. It does not load or train a model.
- `calendar_events.yaml` defines reviewed scheduled event windows used as future risk flags, not trade signals.
- `execution.yaml` defines execution defaults. Alpaca may be enabled only in paper-only mode and cannot place live orders without explicit live-trading authorization.

## Local Validation

Run these commands from Windows PowerShell after pulling the repo:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_environment.py
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb.py
& ".\.venv\Scripts\python.exe" scripts/check_yfinance_proxy.py
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
& ".\.venv\Scripts\python.exe" -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

`check_external_event_sources.py` is offline by default. If it is absent from
the checked-out branch, the pilot has not landed there. `run_tests.ps1` invokes
the config-required QuestDB health check and therefore needs the local service.

The same commands should be run on the separate trading laptop after it pulls from GitHub.

## Safety Rules

- Online structured context/source connectivity is allowed when configuration is complete.
- Slow collectors must not run in the per-tick decision loop.
- AI context output has no direct trade authority.
- Live trading is disabled by default and Alpaca remains paper-only.
- QuestDB is a bot ledger only.
- Per-tick/per-signal decisions must read context from in-memory cache, not QuestDB.
- No V1 raw market-data table names belong in V2 config files.
- Phase 7 classifications and shadow actions remain research-only and cannot
  alter real risk, sizing, model, broker, or execution behavior.
- Full source documents, prompts/request excerpts, credentials, and provider
  exceptions must not appear in QuestDB configuration or schemas.
- External-source settings must keep `direct_trade_authority: false`; an
  enabled collector still receives no scheduling, policy, risk, or broker
  authority.
- Source archives, classification registries, coverage manifests, and mutable
  checkpoints belong under ignored `data_lake/`, not in Git or QuestDB.

## Gemini AI Context Filter

PR35 configures the source-neutral classifier under `ai_context_filter`:

```yaml
enabled: false
provider: gemini
model: gemini-3.5-flash
api_key_env: GEMINI_API_KEY
prompt_version: context_filter_v1
response_schema_version: context_classification_response_v1
timeout_seconds: 30.0
max_retries: 2
retry_base_delay_seconds: 0.5
retry_max_delay_seconds: 4.0
max_input_characters: 12000
max_prompt_characters: 30000
max_summary_characters: 500
max_output_tokens: 256
max_provider_calls_per_minute: 6
max_provider_calls_per_run: 20
dedup_cache_max_entries: 256
temperature: 0
direct_trade_authority: false
```

The model is configurable without a code change. The loader nevertheless
requires `provider: gemini`, zero temperature, positive bounds and budgets, no
more than two repository-owned retries, and `direct_trade_authority: false`.
The last setting is a hard safety assertion: `true` is invalid configuration,
not a supported mode.

The values above remain the exact backward-compatible v1 defaults. External
multi-scope profiles explicitly pin `context_filter_v2_scope`,
`context_classification_response_v2`, and
`context_filter_validator_v2_scope`. V2 accepts only canonical uppercase
ticker/sector allowlists supplied by trusted configuration and an explicit
boolean `global_relevance`; it does not relax v1 responses or silently select a
latest profile.

`GEMINI_API_KEY=` is the only Gemini placeholder in `.env.example`. Put the
real value in the ignored repository `.env`; the application and explicit live
checker load that file without displaying it. The key is never part of a
prompt, result, exception, or normal log field.

Each Interactions request uses JSON MIME output with the contract-derived JSON
Schema, `store=False`, and no tools, browsing, code execution, agents, previous
interaction ID, background execution, or conversation history. The SDK is
configured for one HTTP attempt; the classifier alone owns the bounded custom
retry loop for timeouts/network interruptions, 429/resource exhaustion, and
retryable 5xx failures. Two retries permit at most three actual provider calls
per logical classification. Authentication, permission, safety, validation,
and local-budget failures are not retried.

The source excerpt and final rendered prompt have separate 12,000- and
30,000-character limits, so oversized trusted metadata is also rejected before
network use. The local six-calls-per-minute and 20-calls-per-run guards are conservative
process limits, not replacements for Google project quotas or billing controls.
The 256-entry LRU deduplication cache stores only valid or abstained results and
uses a bounded fingerprint of trusted hashes/IDs, ticker hints, source type,
prompt, model, and schema versions. It is process-local and disappears on
restart. SEC and external-event archives own their separate durable
classification suppression and canonical-result state; they reuse this
classifier rather than adding another Gemini transport or retry loop.

Supported text inputs include SEC filing sections and exhibits, news excerpts,
social or political statements, contract descriptions,
government announcements, regulatory/policy and geopolitical developments,
company disclosures, and manual research documents. FRED observations, EIA
numbers, calendar timing, proxy bars, structured USAspending award values, and
deterministic Form 4 transaction facts bypass Gemini.

Offline/default and explicitly gated live checks are:

```powershell
python scripts/check_gemini_context.py
python scripts/check_gemini_context.py --live --required
```

The live checker makes one synthetic request. Free-tier 429 responses receive
only the configured bounded retries; exhaustion returns a safe structured
provider failure. Source archives own durable state and callers own optional
QuestDB publication. The classifier never writes QuestDB itself.

## SEC EDGAR Research Collector

PR36 reads public SEC EDGAR endpoints for `8-K`, `8-K/A`, `4`, and `4/A` only.
It requires contact identification, not an API key: set blank-placeholder
variables `SEC_ORGANIZATION` and `SEC_CONTACT_EMAIL` in the ignored `.env`.
The contact email is placed only in the compliant HTTP User-Agent; no email
account access, password, Gmail integration, or SEC API key is used.

The collector is source-specific and deliberately bounded: sequential requests
default to 2 requests per second and configuration above 8 per second fails.
They use monotonic pacing and a 10-second timeout, honor bounded `Retry-After`
on 429, use bounded exponential backoff for retryable 5xx/transport failures,
and stop on a potential fair-access 403. Downloads are never concurrent.

Immutable originals, complete normalized 8-K sections, deterministic Form 4
metadata, and the atomically replaced SEC manifest live under ignored
`data_lake/context/sec_edgar/`. Complete sections are hashed and archived before
a versioned `HEAD_V1` excerpt is bounded to PR35's Gemini input limit. The
manifest owns cross-restart successful-result suppression and ledger retry;
PR35's LRU remains process-local. QuestDB stores safe concise attempt metadata,
never raw filings or sections.

Form 4 facts are parsed from official XML. Both derivative and non-derivative
transactions are retained, while only non-derivative `P` and `S` become initial
research events. Unresolved Form 4/A records are excluded from default aggregate
counts. Gemini is used only when `--classify` is explicitly requested.

Offline/default validation makes no network call:

```powershell
python scripts/check_sec_edgar.py
```

The manually gated read-only SEC smoke check has a one-actionable-filing cap and
no broker, Gemini, or QuestDB action. In dry-run mode the same option caps raw
discoveries because no processing occurs:

```powershell
python scripts/check_sec_edgar.py --live --ticker LMT --form 8-K --max-filings 1
```

See `docs/sec_edgar.md` for bounded collection and optional `--classify` /
`--questdb` operation.

## External News, Social, and Earnings Sources

The pilot defines four disabled-by-default entries under
`unstructured_sources`:

```yaml
veritawire_truth_social:
  enabled: false
  direct_trade_authority: false

lockheed_martin_rss:
  enabled: false
  direct_trade_authority: false

palantir_ir:
  enabled: false
  direct_trade_authority: false

company_earnings:
  enabled: false
  direct_trade_authority: false
```

The complete committed entries also pin their official URLs, bounded network
and item limits, archive root, polling/reconnect behavior, parser/extraction
versions, fixed ticker ownership where applicable, and classification defaults.
These are operational settings only. Enabling a source does not schedule it,
classify records, write QuestDB, or grant policy/trading authority.

VeritaWire uses the environment-variable reference:

```yaml
api_key_env: VERITAWIRE_API_KEY
```

`VERITAWARE_API_KEY` is a spelling error. Keep only the correctly spelled blank
name in `.env.example` and the real value in the ignored `.env`. Never place the
value in YAML, URLs, logs, exceptions, fixtures, archives, or ledger rows.

Initial normal polling intervals are configurable and start at 30 seconds for
LMT RSS and PLTR IR. The earnings-window fast interval starts at 10 seconds and
is used only when polling is explicitly requested. Tests and one-shot checks do
not sleep for those durations.

Offline validation:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
```

Bounded live examples:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source veritawire --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source lmt-rss --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source pltr-ir --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source earnings --ticker PLTR --max-items 1 --timeout-seconds 20
```

Live collection does not imply classification or QuestDB access. `--classify`
and `--questdb` are separate explicit opt-ins. `--backfill` remains bounded and
separately enabled. Optional polling uses `--poll` and, for checks, a finite
`--max-polls`.

The ignored archive root is `data_lake/context/external_events/`. It owns raw
source truth, lifecycle revisions, durable canonical classification ownership,
conflict/resolution state, atomic checkpoints, and generation-pinned coverage.
QuestDB remains metadata-only. See `docs/external_event_ingestion.md` for exact
source URLs, availability modes, lifecycle, scope, duplicate/correlation,
coverage, restart, and safety behavior.

## YFinance Development Proxy

`structured_sources.yfinance_dev_only` is a PR25 development-only collector source. It may be enabled for source connectivity, but it is not production critical and is not used in the per-tick loop.

Required settings:

```yaml
enabled: true
development_only: true
production_critical: false
feeds_memory_cache: true
writes_questdb_ledger: true
used_in_per_tick_loop: false
required: false
period: "5d"
interval: "5m"
timeout_seconds: 10.0
bar_completion_grace_seconds: 30
max_staleness_seconds: 360
auto_adjust: false
actions: false
repair: false
keepna: true
prepost: false
threads: true
```

Validation enforces five-minute-only bars and:

```text
max_staleness_seconds >= 300 + bar_completion_grace_seconds
```

The collector stores `valid_until = source_event_time + max_staleness_seconds`. That keeps the previous completed bar usable while the newest five-minute bar is still inside the completion grace period.

Oil proxy ETFs XLE, XOP, and OIH are stored under `SECTOR/OIL`, matching the
configured `oil` sector used by `XOM`, `OXY`, `SLB`, `COP`, and `VLO` after
cache key normalization.

Offline smoke, no internet or QuestDB:

```powershell
python scripts/check_yfinance_proxy.py
```

Optional live smoke:

```powershell
python scripts/check_yfinance_proxy.py --live
python scripts/check_yfinance_proxy.py --live --require-fresh
python scripts/check_yfinance_proxy.py --live --write-questdb
```

`NO_FRESH_DATA` exits successfully by default in live mode because the source may be reachable while the market is closed or no fresh completed bars are available. `--require-fresh` makes that status fail. `--live --write-questdb` requires successful QuestDB writes for produced indicators and exits nonzero on ledger write failures or zero successful writes when valid indicators were produced.

Full behavior is documented in `docs/yfinance_proxy.md`.

## QuestDB Health Defaults

QuestDB health config is resolved in this order:

```text
explicit script/function overrides
-> environment variables / .env
-> config/questdb.yaml
-> hardcoded defaults
```

The default check uses `http://localhost:9000/exec?query=SELECT 1`. Current
repository configuration sets health, writer, and analysis
`required_by_default: true`; a plain health command therefore fails if the
local service is unavailable. The server laptop may make that explicit with:

```powershell
python scripts/check_questdb.py --required
```

The QuestDB writer uses the same HTTP host, port, scheme, and timeout defaults.
It also has `QUESTDB_MAX_SQL_LENGTH_CHARS`, which defaults to `7000`, so large
JSON payloads are rejected before the documented `/exec` GET path is used.
The QuestDB analysis reader is read-only and uses the same connection defaults.
It also has `QUESTDB_ANALYSIS_MAX_ENCODED_URL_LENGTH_CHARS`, which defaults to
`7000`, so oversized readback URLs are rejected before `/exec` is called.
