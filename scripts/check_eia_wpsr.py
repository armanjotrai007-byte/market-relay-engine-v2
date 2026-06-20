"""Offline and optional read-only live validation for PR26 EIA WPSR."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.common.config import load_all_configs  # noqa: E402
from market_relay_engine.context.eia_wpsr import (  # noqa: E402
    EIARelease,
    EIAWPSRCollectionStatus,
    EIAWPSRCollector,
    EIAWPSRConfig,
    STOCK_ROUTE,
)
from market_relay_engine.context.state_cache import ContextStateCache  # noqa: E402
from scripts.refresh_eia_wpsr_schedule import EASTERN, refresh_live  # noqa: E402


FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "eia_wpsr"
OFFLINE_RELEASE = datetime(2026, 6, 17, 14, 30, tzinfo=UTC)


class FixtureClient:
    def __init__(self) -> None:
        self.stocks = json.loads((FIXTURE_DIR / "weekly_stocks.json").read_text(encoding="utf-8"))["response"]["data"]
        self.utilization = json.loads((FIXTURE_DIR / "refinery_utilization.json").read_text(encoding="utf-8"))["response"]["data"]

    def fetch_weekly_records(self, route: str, series_ids: object, *, observations_per_series: int = 3) -> list[dict[str, object]]:
        return list(self.stocks if route == STOCK_ROUTE else self.utilization)


def _offline_config() -> EIAWPSRConfig:
    return EIAWPSRConfig(
        event_windows_enabled=True,
        numeric_source_enabled=True,
        releases=(
            EIARelease(release_id="eia_wpsr_2026_06_17", release_at=OFFLINE_RELEASE, report_period=date(2026, 6, 12)),
            EIARelease(release_id="eia_wpsr_2026_06_24", release_at=OFFLINE_RELEASE + timedelta(days=7), report_period=date(2026, 6, 19)),
        ),
        oil_tickers=("XOM", "CVX"),
    )


def run_offline() -> object:
    result = EIAWPSRCollector(cache=ContextStateCache(), config=_offline_config(), client=FixtureClient()).collect(evaluation_time=OFFLINE_RELEASE + timedelta(seconds=30))
    if result.status is not EIAWPSRCollectionStatus.SUCCESS or len(result.indicator_snapshots) != 10:
        raise RuntimeError(f"offline EIA validation failed: {result.status.value}")
    return result


def run_live() -> object:
    now = datetime.now(UTC)
    schedule = refresh_live(start_date=date(now.year, 1, 1), end_date=date(now.year, 12, 31))
    releases = tuple(
        EIARelease(
            release_id=item["release_id"],
            release_at=datetime.fromisoformat(item["release_at"]),
            report_period=date.fromisoformat(item["report_period"]),
        )
        for item in schedule["releases"]
    )
    configs = load_all_configs(base_dir=REPO_ROOT)
    base = EIAWPSRConfig.from_repository_configs(
        configs["calendar_events"],
        configs["context_sources"],
        configs["symbols"],
        event_windows_enabled=True,
        numeric_source_enabled=True,
        releases=releases,
    )
    return EIAWPSRCollector(cache=ContextStateCache(), config=base).collect(evaluation_time=now)


def _print_result(result: object) -> None:
    print(f"status: {result.status.value}")
    print(f"data_status: {result.data_status.value}")
    print(f"expected_report_period: {result.expected_report_period or '-'}")
    print(f"last_seen_report_period: {result.last_seen_report_period or '-'}")
    print(f"next_retry_at: {result.next_retry_at.isoformat() if result.next_retry_at else '-'}")
    print(f"flag_count: {len(result.context_flags)}")
    print(f"indicator_count: {len(result.indicator_snapshots)}")
    for snapshot in result.indicator_snapshots:
        print(f"indicator={snapshot.indicator_name} value={snapshot.value:g} units={snapshot.units} series={snapshot.details.get('series_id')} period={snapshot.details.get('report_period')}")
    for issue in result.issues:
        print(f"issue={issue.issue_type} metric={issue.metric or '-'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check EIA WPSR collection")
    parser.add_argument("--live", action="store_true", help="perform read-only official EIA checks")
    parser.add_argument("--write-questdb", action="store_true", help="reserved server-only mode")
    args = parser.parse_args(argv)
    if args.write_questdb:
        parser.error("server-only QuestDB write mode is not enabled in this work-laptop check")
    try:
        result = run_live() if args.live else run_offline()
        _print_result(result)
        return 0 if result.status not in {EIAWPSRCollectionStatus.FAILED, EIAWPSRCollectionStatus.SUPERSEDED} else 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary.
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
