# Server Runtime Validation

This note maps server-side validation coverage without replacing existing checker scripts or creating a broad runbook.

| Category | Existing or new validation |
| --- | --- |
| Python/package/config | Existing `scripts/check_environment.py` and `scripts/check_config.py` |
| Real context source APIs | Existing PR33 `scripts/smoke_context_sources.py --live --env-file` mode |
| QuestDB HTTP + generic writer/read-only components | Existing `scripts/check_questdb.py`, `scripts/check_questdb_writer.py --required`, and `scripts/check_questdb_analysis.py --required` |
| Context-source-specific QuestDB persistence | New PR33 `scripts/smoke_context_sources.py --live --questdb --env-file` mode |
| Meinberg NTP daemon | New `scripts/check_meinberg_ntp.ps1` |
| Alpaca account connectivity | Existing optional account-only `scripts/check_alpaca_paper.py --required` |
| Databento / live market feed | Not added in this PR because no current live server collector/service path exists to validate |

PR33 QuestDB mode is explicit. Ordinary source smoke remains no-write; `--questdb` adds the existing QuestDB health check, a permanent `system_health_events` validation marker, exact read-back through `QuestDBLedgerReader`, and source-specific collector ledger write/read-back checks where deployed configuration enables that ledger path.

Never run destructive schema apply against the active server ledger. PR33 validation rows are clearly tagged and intentionally preserved for auditability.

The Meinberg NTP check is Windows-only and non-invasive. It verifies the supplied service name, `ntpq.exe` path, config path, expected upstream token, selected peer, reach, and offset threshold without changing the service, daemon config, registry, network settings, or clock configuration.

No validation in this document modifies the active 24/7 service checkout, service process, service cache, service runtime state, or production USAspending checkpoint. The server operator runs validation from an isolated worktree and passes the active server `.env` by absolute path without copying it into the validation worktree.
