"""Offline and optional live smoke check for the PR25 yfinance proxy collector."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import sys

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.config import load_all_configs  # noqa: E402
from market_relay_engine.context.state_cache import ContextStateCache  # noqa: E402
from market_relay_engine.context.yfinance_proxy import (  # noqa: E402
    YFinanceProxyCollectionResult,
    YFinanceProxyCollectionStatus,
    YFinanceProxyCollector,
    YFinanceProxyConfig,
    YFinanceProxyError,
    cache_indicator_name,
)
from market_relay_engine.questdb.writer import (  # noqa: E402
    QuestDBLedgerWriter,
    load_questdb_write_config,
)

OFFLINE_COLLECTION_TIME = datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC)


def _offline_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": [100.0 + index for index in range(14)]},
        index=pd.date_range(datetime(2026, 1, 2, 14, 0, tzinfo=UTC), periods=14, freq="5min", tz="UTC"),
    )


def _run_offline() -> YFinanceProxyCollectionResult:
    def download(**_: object) -> pd.DataFrame:
        return _offline_frame()

    config = YFinanceProxyConfig(enabled=True, requested_symbols=("XLE",))
    cache = ContextStateCache()
    collector = YFinanceProxyCollector(
        cache=cache,
        config=config,
        download=download,
        clock=lambda: OFFLINE_COLLECTION_TIME,
    )
    result = collector.collect()
    if result.status is not YFinanceProxyCollectionStatus.SUCCESS:
        raise RuntimeError(f"offline collector expected SUCCESS, got {result.status}: {result.issues}")
    latest = next(snapshot for snapshot in result.indicator_snapshots if snapshot.indicator_name == "latest_close")
    if latest.value != 112.0:
        raise RuntimeError("offline incomplete-bar filtering did not use the previous completed bar")
    expected_returns = {"return_5m", "return_15m", "return_60m"}
    actual_returns = {snapshot.indicator_name for snapshot in result.indicator_snapshots}
    if not expected_returns.issubset(actual_returns):
        raise RuntimeError(f"offline exact returns missing: {sorted(expected_returns - actual_returns)}")
    cached = cache.get_sector("OIL", cache_indicator_name("XLE", "return_5m", "5m"), now=OFFLINE_COLLECTION_TIME)
    if cached is None:
        raise RuntimeError("offline collector did not publish the sector cache entry")
    return result


def _run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
    configs = load_all_configs(base_dir=REPO_ROOT)
    config = YFinanceProxyConfig.from_repository_configs(
        configs["context_sources"],
        configs["symbols"],
        enabled=True,
    )
    writer = None
    if write_questdb:
        writer = QuestDBLedgerWriter(load_questdb_write_config(required=True))
    collector = YFinanceProxyCollector(
        cache=ContextStateCache(),
        config=config,
        ledger_writer=writer,
    )
    return collector.collect(write_questdb=write_questdb, questdb_required=write_questdb)


def _print_result(result: YFinanceProxyCollectionResult) -> None:
    print(f"status: {result.status.value}")
    print(f"requested_symbols: {', '.join(result.requested_symbols)}")
    print(f"successful_symbols: {', '.join(result.successful_symbols) or '-'}")
    print(f"failed_symbols: {', '.join(result.failed_symbols) or '-'}")
    print(f"stale_symbols: {', '.join(result.stale_symbols) or '-'}")
    print(f"indicator_count: {len(result.indicator_snapshots)}")
    print(f"cache_update_count: {len(result.cache_update_results)}")
    print(f"ledger_write_count: {len(result.ledger_write_results)}")
    if result.issues:
        print("issues:")
        for issue in result.issues:
            symbol = f" symbol={issue.symbol}" if issue.symbol else ""
            window = f" window={issue.window}" if issue.window else ""
            print(f"- {issue.issue_type}{symbol}{window}: {issue.message}")


def _print_write_smoke_failure(*, attempted: int, successful: int, failed: int, first_failure: str) -> None:
    print("ERROR: QuestDB write smoke failed.")
    print(f"attempted_writes: {attempted}")
    print(f"successful_writes: {successful}")
    print(f"failed_writes: {failed}")
    print(f"first_failure: {first_failure}")


def _validate_write_smoke_result(result: YFinanceProxyCollectionResult) -> int:
    attempted = len(result.indicator_snapshots)
    successful = len(result.ledger_write_results)
    ledger_issues = [issue for issue in result.issues if issue.issue_type == "LEDGER_WRITE_FAILED"]
    failed = len(ledger_issues)
    if ledger_issues:
        _print_write_smoke_failure(
            attempted=attempted,
            successful=successful,
            failed=failed,
            first_failure=ledger_issues[0].message,
        )
        return 1
    if attempted > 0 and successful == 0:
        _print_write_smoke_failure(
            attempted=attempted,
            successful=successful,
            failed=attempted,
            first_failure="valid indicators were produced but no QuestDB rows were written",
        )
        return 1
    if successful < attempted:
        _print_write_smoke_failure(
            attempted=attempted,
            successful=successful,
            failed=attempted - successful,
            first_failure="not every produced indicator had a successful QuestDB write",
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the PR25 yfinance development proxy collector")
    parser.add_argument("--live", action="store_true", help="perform one real yfinance download")
    parser.add_argument("--require-fresh", action="store_true", help="treat NO_FRESH_DATA as a failure")
    parser.add_argument("--write-questdb", action="store_true", help="write successful live indicators through the configured QuestDB writer")
    args = parser.parse_args(argv)

    if args.write_questdb and not args.live:
        parser.error("--write-questdb requires --live")

    try:
        result = _run_live(write_questdb=args.write_questdb) if args.live else _run_offline()
    except YFinanceProxyError as exc:
        if args.write_questdb:
            _print_write_smoke_failure(
                attempted=1,
                successful=0,
                failed=1,
                first_failure=str(exc),
            )
            return 1
        raise
    _print_result(result)

    if result.status is YFinanceProxyCollectionStatus.NO_FRESH_DATA:
        print("WARNING: yfinance was reachable, but no fresh completed bars were available; the market may be closed.")
        return 1 if args.require_fresh else 0
    if result.status is YFinanceProxyCollectionStatus.FAILED:
        return 1
    if result.status is YFinanceProxyCollectionStatus.DISABLED:
        return 1
    if args.write_questdb:
        return _validate_write_smoke_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
