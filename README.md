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

The full local validation runner executes the health check, config validation, and then pytest:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

These same commands must be run on the separate trading laptop after it pulls from GitHub.

## Configuration

Trading System V2 config files live under `config/`. They separate tradable symbols from context symbols, keep all external sources disabled by default, mark yfinance as development-only, keep QuestDB ledger-only, and keep live trading disabled by default.

Validate configuration locally with:

```powershell
python scripts/check_config.py
```

See `docs/configuration.md` for the config file map and safety rules.

## Safety Defaults

The repository defaults to local development and future paper trading. `.env.example` contains placeholder variable names only. Do not commit real secrets, live credentials, logs, Databento DBN files, Parquet data, QuestDB data folders, or generated API exports.

QuestDB is reserved for the bot ledger and black-box recorder: signals, risk decisions, context flags, orders, fills, slippage, latency, PnL, outcomes, and system health. It must not be used as a historical market-data warehouse.
