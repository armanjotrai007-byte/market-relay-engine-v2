# Live Runbook

PR 1 has no live trading runbook because it does not connect to live data, brokers, or external APIs.

For now, the operating runbook is local validation:

```powershell
python scripts/check_environment.py
pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

The separate trading laptop must pull from GitHub and run the same commands locally before any future paper-trading workflow is used.
