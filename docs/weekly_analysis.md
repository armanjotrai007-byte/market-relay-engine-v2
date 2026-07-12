# Weekly Analysis

Weekly analysis is a future workflow. It will use QuestDB bot-ledger exports
joined with official Databento historical Parquet/DBN-derived data where
appropriate. QuestDB is not the source of historical market truth.

Reports should evaluate approved trades, blocked signals, slippage, latency, event windows, context flags, risk rules, and execution quality.

PR34 adds audit schemas for `context_classification_attempts` and
`shadow_context_policy_evaluations`. Future analysis can join their IDs, hashes,
validation metadata, matched event/flag IDs, and hypothetical actions to model
signals and real risk decisions. A shadow action is observational and must not
be reported as the real decision.

The ledger intentionally excludes full filings, articles, posts, normalized
documents, prompt/request excerpts, credentials, and full provider exceptions.
Research that needs source bodies must use the future immutable local archive
from PR38/PR39, never QuestDB raw-text columns.

PR 1 does not generate reports and does not commit generated data.
