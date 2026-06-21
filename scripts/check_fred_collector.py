"""Deterministic offline validation for the PR27 FRED collector."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.fred_collector import (  # noqa: E402
    FREDCollectionStatus,
    FREDCollector,
    FREDConfig,
)
from market_relay_engine.context.state_cache import (  # noqa: E402
    ContextStateCache,
    ContextStateUpdateStatus,
)


CHECKED_AT = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)


def _records(latest: str, previous: str) -> list[dict[str, object]]:
    return [
        {"date": "2026-06-20", "value": "."},
        {"date": "malformed", "value": "9.9"},
        {"date": "2026-06-19", "value": latest, "realtime_start": "ignored-a"},
        {"date": "2026-06-19", "value": "999", "realtime_start": "duplicate"},
        {"date": "2026-06-16", "value": previous, "realtime_end": "ignored-b"},
    ]


class OfflineClient:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = set() if fail is None else set(fail)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.payloads = {
            "DGS3MO": _records("4.20", "4.10"),
            "DGS2": _records("4.00", "3.95"),
            "DGS10": _records("4.35", "4.25"),
        }

    def fetch_observations(self, series_id: str, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append((series_id, dict(kwargs)))
        if series_id in self.fail:
            raise RuntimeError("offline source failure")
        return deepcopy(self.payloads[series_id])


class OfflineWriter:
    def __init__(self) -> None:
        self.snapshots: list[object] = []

    def write_context_indicator_snapshot(self, snapshot: object, **kwargs: object) -> str:
        self.snapshots.append(snapshot)
        return "written"


def run_checks() -> None:
    disabled_client = OfflineClient()
    disabled = FREDCollector(
        cache=ContextStateCache(),
        config=FREDConfig(),
        client=disabled_client,
    ).collect(evaluation_time=CHECKED_AT)
    assert disabled.status is FREDCollectionStatus.DISABLED
    assert disabled_client.calls == []

    client = OfflineClient()
    cache = ContextStateCache()
    writer = OfflineWriter()
    collector = FREDCollector(
        cache=cache,
        config=FREDConfig(enabled=True),
        client=client,
        ledger_writer=writer,
    )
    first = collector.collect(evaluation_time=CHECKED_AT, write_questdb=True)
    assert first.status is FREDCollectionStatus.SUCCESS
    assert len(first.indicator_snapshots) == len(writer.snapshots) == 10
    assert {series_id for series_id, _ in client.calls} == {"DGS3MO", "DGS2", "DGS10"}
    assert all(
        params == {
            "file_type": "json",
            "sort_order": "desc",
            "order_by": "observation_date",
            "limit": 20,
        }
        for _, params in client.calls
    )
    values = {item.indicator_name: item.value for item in first.indicator_snapshots}
    assert values["us_treasury_2y_minus_3m"] == -0.2
    assert values["us_treasury_10y_minus_2y"] == 0.35
    assert values["us_treasury_10y_minus_3m"] == 0.15
    assert values["us_treasury_3m_yield_change_prev_valid_obs"] == 0.1
    assert values["rate_curve_regime_v1"] == "FRONT_INVERTED__LONG_POSITIVE"
    assert all(item.ticker_or_sector == "GLOBAL" for item in first.indicator_snapshots)
    assert all(item.details["research_asof_eligible"] is False for item in first.indicator_snapshots)
    assert all("realtime_start" not in item.details for item in first.indicator_snapshots)

    first_collected = {
        item.indicator_name: item.details["first_collected_at"]
        for item in first.indicator_snapshots
    }
    second = collector.collect(
        evaluation_time=datetime(2026, 6, 21, 16, 0, tzinfo=UTC),
        write_questdb=True,
    )
    assert second.status is FREDCollectionStatus.SUCCESS
    assert all(
        item.status is ContextStateUpdateStatus.IGNORED_DUPLICATE
        for item in second.cache_update_results
    )
    assert len(writer.snapshots) == 10
    assert {
        item.indicator_name: item.details["first_collected_at"]
        for item in second.indicator_snapshots
    } == first_collected

    failed = FREDCollector(
        cache=ContextStateCache(),
        config=FREDConfig(enabled=True),
        client=OfflineClient(fail={"DGS3MO", "DGS2", "DGS10"}),
    ).collect(evaluation_time=CHECKED_AT)
    assert failed.status is FREDCollectionStatus.FAILED

    partial = FREDCollector(
        cache=ContextStateCache(),
        config=FREDConfig(enabled=True),
        client=OfflineClient(fail={"DGS10"}),
    ).collect(evaluation_time=CHECKED_AT)
    assert partial.status is FREDCollectionStatus.PARTIAL


def main() -> int:
    try:
        run_checks()
    except Exception as exc:  # noqa: BLE001 - CLI validation boundary.
        print(f"FRED collector validation FAILED: {exc}")
        return 1
    print("FRED collector validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
