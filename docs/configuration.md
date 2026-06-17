# Configuration

PR 2 organizes the Trading System V2 configuration files under `config/`.

Each file is safe to validate locally without internet access, API keys, broker access, QuestDB, or live market data.

## Files

- `symbols.yaml` defines example tradable symbols and separate context symbols. Example tradable symbols are not approved for live trading. PR25 uses fixed context proxy groups for SPY, QQQ, IWM, GLD, `^VIX`, XLE, XOP, OIH, XLI, PPA, and ITA.
- `context_sources.yaml` defines structured and unstructured context source settings. All sources are disabled by default, and `yfinance_dev_only` is explicitly development-only.
- `risk_limits.yaml` defines placeholder paper-trading risk limits. These are not optimized live settings.
- `questdb.yaml` defines QuestDB connection and health-check defaults and confirms QuestDB is for the bot ledger only, not a historical market-data warehouse.
- `model_config.yaml` defines placeholder feature, model, calibration, horizon, and label settings. It does not load or train a model.
- `calendar_events.yaml` defines empty scheduled event windows used as future risk flags, not trade signals.
- `execution.yaml` defines future execution defaults. Alpaca is disabled by default, paper-only, and cannot place live orders without manual config changes in a future PR.

## Local Validation

Run these commands from Windows PowerShell after pulling the repo:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_questdb.py
python scripts/check_yfinance_proxy.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

The same commands should be run on the separate trading laptop after it pulls from GitHub.

## Safety Rules

- External sources are disabled by default.
- Slow collectors must not run in the per-tick decision loop.
- AI context output has no direct trade authority.
- Live trading is disabled by default.
- QuestDB is a bot ledger only.
- No V1 raw market-data table names belong in V2 config files.

## YFinance Development Proxy

`structured_sources.yfinance_dev_only` is a PR25 development-only collector source. It is disabled by default, not production critical, and not used in the per-tick loop.

Required settings:

```yaml
enabled: false
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

`NO_FRESH_DATA` exits successfully by default in live mode because the source may be reachable while the market is closed or no fresh completed bars are available. `--require-fresh` makes that status fail.

Full behavior is documented in `docs/yfinance_proxy.md`.

## QuestDB Health Defaults

QuestDB health config is resolved in this order:

```text
explicit script/function overrides
-> environment variables / .env
-> config/questdb.yaml
-> hardcoded defaults
```

The default check uses `http://localhost:9000/exec?query=SELECT 1`. Offline
validation uses optional mode, while the server laptop should run:

```powershell
python scripts/check_questdb.py --required
```

The QuestDB writer uses the same HTTP host, port, scheme, and timeout defaults.
It also has `QUESTDB_MAX_SQL_LENGTH_CHARS`, which defaults to `7000`, so large
JSON payloads are rejected before the documented `/exec` GET path is used.
The QuestDB analysis reader is read-only and uses the same connection defaults.
It also has `QUESTDB_ANALYSIS_MAX_ENCODED_URL_LENGTH_CHARS`, which defaults to
`7000`, so oversized readback URLs are rejected before `/exec` is called.
