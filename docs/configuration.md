# Configuration

PR 2 organizes the Trading System V2 configuration files under `config/`.

Each file is safe to validate locally without internet access, API keys, broker access, QuestDB, or live market data.

## Files

- `symbols.yaml` defines example tradable symbols and separate context symbols. Example tradable symbols are not approved for live trading.
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
