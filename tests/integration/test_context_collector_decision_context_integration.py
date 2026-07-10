from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd

from market_relay_engine.context.decision_context import DecisionContextAssembler
from market_relay_engine.context.eia_wpsr import (
    EIARelease,
    EIAWPSRCollectionStatus,
    EIAWPSRCollector,
    EIAWPSRConfig,
    STOCK_ROUTE,
    UTILIZATION_ROUTE,
)
from market_relay_engine.context.fred_collector import FREDCollectionStatus, FREDCollector, FREDConfig
from market_relay_engine.context.macro_calendar import (
    MacroCalendarCollectionStatus,
    MacroCalendarCollector,
    MacroCalendarConfig,
    load_macro_calendar,
)
from market_relay_engine.context.state_cache import ContextStateCache
from market_relay_engine.context.usaspending_collector import (
    USAspendingCollectionStatus,
    USAspendingCollector,
    USAspendingConfig,
    USAspendingRecipientMapping,
)
from market_relay_engine.context.yfinance_proxy import (
    YFinanceProxyCollectionStatus,
    YFinanceProxyCollector,
    YFinanceProxyConfig,
    build_proxy_registry,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
EIA_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "eia_wpsr"
TRACE_ID = "trace_pr33_collector_assembler"
SMOKE_TICKER = "XOM"


def test_real_collectors_materialize_cache_entries_consumed_by_decision_context() -> None:
    scenarios = (
        _run_macro_calendar(),
        _run_eia_wpsr(),
        _run_fred(),
        _run_usaspending(),
        _run_yfinance(),
    )

    for scenario in scenarios:
        assert scenario["entry_count"] > 0, scenario["name"]
        for raw_entry in scenario["entries"]:
            context = _assemble_for_entry(scenario["cache"], raw_entry, scenario["evaluation_time"])
            selected = [
                entry
                for entry in context.all_structured_context
                if entry.cache_scope == raw_entry["scope"]
                and entry.cache_name == raw_entry["name"]
                and entry.scope_target == _scope_target(raw_entry)
                and entry.source == raw_entry["source"]
            ]

            assert len(selected) == 1, (scenario["name"], raw_entry)
            json.dumps(context.to_audit_payload().to_json_dict(), allow_nan=False, sort_keys=True)


def test_usaspending_uses_temporary_checkpoint_path_with_real_store() -> None:
    with TemporaryDirectory(prefix=".tmp-usaspending-seam-", dir=REPO_ROOT) as temp_dir:
        temp_root = Path(temp_dir)
        checkpoint_path = temp_root / "award_checkpoint.json"
        config = USAspendingConfig(
            enabled=True,
            checkpoint_path=_repo_relative(checkpoint_path),
        )
        cache = ContextStateCache()
        collector = USAspendingCollector(
            cache=cache,
            config=config,
            client=_USAspendingFakeClient(),
            recipient_mappings=(_usaspending_mapping(),),
        )

        result = collector.collect(
            evaluation_time=datetime(2026, 6, 20, 16, 0, tzinfo=UTC),
            write_questdb=False,
            questdb_required=False,
            run_id=None,
            session_id=None,
        )

        assert result.status is USAspendingCollectionStatus.SUCCESS
        assert checkpoint_path.is_file()
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["checkpoint_schema_version"] == "usaspending_award_checkpoint_v1"
        assert checkpoint["last_successful_collection_at"] == "2026-06-20T16:00:00Z"
        assert checkpoint["source_last_updated_date"] == "2026-06-20"
        assert isinstance(checkpoint["seen_event_fingerprints"], dict)
        assert isinstance(checkpoint["award_registry"], dict)


def _run_macro_calendar() -> dict[str, object]:
    evaluation_time = datetime(2026, 7, 1, 13, 55, tzinfo=UTC)
    cache = ContextStateCache()
    collector = MacroCalendarCollector(
        cache=cache,
        config=MacroCalendarConfig(enabled=True),
        calendar=load_macro_calendar(REPO_ROOT / "config" / "macro_calendar.yaml"),
        base_dir=REPO_ROOT,
    )

    result = collector.collect_once(
        evaluation_time,
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )

    assert result.status is MacroCalendarCollectionStatus.SUCCESS
    return _scenario("macro_calendar", cache, evaluation_time)


def _run_eia_wpsr() -> dict[str, object]:
    release_at = datetime(2026, 6, 17, 14, 30, tzinfo=UTC)
    evaluation_time = release_at + timedelta(seconds=30)
    cache = ContextStateCache()
    collector = EIAWPSRCollector(
        cache=cache,
        config=EIAWPSRConfig(
            event_windows_enabled=True,
            numeric_source_enabled=True,
            releases=(
                EIARelease(
                    release_id="eia_wpsr_2026_06_17",
                    release_at=release_at,
                    report_period=date(2026, 6, 12),
                ),
                EIARelease(
                    release_id="eia_wpsr_2026_06_24",
                    release_at=datetime(2026, 6, 24, 14, 30, tzinfo=UTC),
                    report_period=date(2026, 6, 19),
                ),
            ),
            oil_tickers=("XOM", "CVX"),
        ),
        client=_EIAFakeClient(),
    )

    result = collector.collect(
        evaluation_time=evaluation_time,
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )

    assert result.status is EIAWPSRCollectionStatus.SUCCESS
    return _scenario("eia_wpsr", cache, evaluation_time)


def _run_fred() -> dict[str, object]:
    evaluation_time = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
    cache = ContextStateCache()
    collector = FREDCollector(
        cache=cache,
        config=FREDConfig(enabled=True),
        client=_FREDFakeClient(),
    )

    result = collector.collect(
        evaluation_time=evaluation_time,
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )

    assert result.status is FREDCollectionStatus.SUCCESS
    return _scenario("fred", cache, evaluation_time)


def _run_usaspending() -> dict[str, object]:
    evaluation_time = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
    with TemporaryDirectory(prefix=".tmp-usaspending-materialize-", dir=REPO_ROOT) as temp_dir:
        temp_root = Path(temp_dir)
        cache = ContextStateCache()
        collector = USAspendingCollector(
            cache=cache,
            config=USAspendingConfig(
                enabled=True,
                checkpoint_path=_repo_relative(temp_root / "award_checkpoint.json"),
            ),
            client=_USAspendingFakeClient(with_award=True),
            recipient_mappings=(_usaspending_mapping(ticker="XOM"),),
        )

        result = collector.collect(
            evaluation_time=evaluation_time,
            write_questdb=False,
            questdb_required=False,
            run_id=None,
            session_id=None,
        )

        assert result.status is USAspendingCollectionStatus.SUCCESS
        return _scenario("usaspending", cache, evaluation_time)


def _run_yfinance() -> dict[str, object]:
    evaluation_time = datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC)
    cache = ContextStateCache()
    registry = build_proxy_registry(None)
    config = YFinanceProxyConfig(
        enabled=True,
        requested_symbols=("SPY", "XLE"),
        registry=(registry["SPY"], registry["XLE"]),
    )
    collector = YFinanceProxyCollector(
        cache=cache,
        config=config,
        download=lambda **_: _yfinance_frame(),
        clock=lambda: evaluation_time,
    )

    result = collector.collect(
        evaluation_time=evaluation_time,
        write_questdb=False,
        questdb_required=False,
        run_id=None,
        session_id=None,
    )

    assert result.status is YFinanceProxyCollectionStatus.SUCCESS
    return _scenario("yfinance_dev_only", cache, evaluation_time)


def _scenario(name: str, cache: ContextStateCache, evaluation_time: datetime) -> dict[str, object]:
    entries = _snapshot_entries(cache, evaluation_time)
    return {
        "name": name,
        "cache": cache,
        "evaluation_time": evaluation_time,
        "entries": entries,
        "entry_count": len(entries),
    }


def _assemble_for_entry(cache: ContextStateCache, raw_entry: dict[str, object], evaluation_time: datetime):
    scope = raw_entry["scope"]
    if scope == "GLOBAL":
        ticker = SMOKE_TICKER
        sector = None
    elif scope == "TICKER":
        ticker = str(raw_entry["ticker"])
        sector = None
    else:
        ticker = SMOKE_TICKER
        sector = str(raw_entry["sector"])
    return DecisionContextAssembler(cache=cache).build_for_decision(
        ticker,
        evaluation_time,
        TRACE_ID,
        None,
        ticker_sector=sector,
    )


def _snapshot_entries(cache: ContextStateCache, evaluation_time: datetime) -> list[dict[str, object]]:
    snapshot = cache.snapshot(now=evaluation_time)
    entries: list[dict[str, object]] = []
    entries.extend(dict(entry) for entry in snapshot["global"].values())  # type: ignore[union-attr]
    for by_name in snapshot["tickers"].values():  # type: ignore[union-attr]
        entries.extend(dict(entry) for entry in by_name.values())
    for by_name in snapshot["sectors"].values():  # type: ignore[union-attr]
        entries.extend(dict(entry) for entry in by_name.values())
    return entries


def _scope_target(raw_entry: dict[str, object]) -> str | None:
    if raw_entry["scope"] == "GLOBAL":
        return None
    if raw_entry["scope"] == "TICKER":
        return str(raw_entry["ticker"])
    return str(raw_entry["sector"])


def _repo_relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _eia_records(name: str) -> list[dict[str, object]]:
    payload = json.loads((EIA_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return payload["response"]["data"]


class _EIAFakeClient:
    def __init__(self) -> None:
        self.stocks = deepcopy(_eia_records("weekly_stocks.json"))
        self.utilization = deepcopy(_eia_records("refinery_utilization.json"))

    def fetch_weekly_records(
        self,
        route: str,
        series_ids: object,
        *,
        observations_per_series: int = 3,
    ) -> list[dict[str, object]]:
        del series_ids, observations_per_series
        return deepcopy(self.stocks if route == STOCK_ROUTE else self.utilization)


def _fred_rows(latest: str, prior: str) -> list[dict[str, object]]:
    return [
        {"date": "2026-06-19", "value": latest},
        {"date": "2026-06-16", "value": prior},
    ]


class _FREDFakeClient:
    payloads = {
        "DGS3MO": _fred_rows("4.20", "4.10"),
        "DGS2": _fred_rows("4.00", "3.95"),
        "DGS10": _fred_rows("4.35", "4.25"),
    }

    def fetch_observations(self, series_id: str, **kwargs: object) -> list[dict[str, object]]:
        assert kwargs == {
            "file_type": "json",
            "sort_order": "desc",
            "order_by": "observation_date",
            "limit": 20,
        }
        return deepcopy(self.payloads[series_id])


UEI = "EXACTUEI1234"


def _usaspending_mapping(*, ticker: str = "TST") -> USAspendingRecipientMapping:
    return USAspendingRecipientMapping(
        recipient_uei=UEI,
        recipient_name="EXACT LEGAL NAME",
        ticker=ticker,
        issuer_name="Test Issuer Inc.",
        mapping_confidence="confirmed",
        economic_beneficiary="prime_recipient",
        active=True,
        mapping_version="usaspending_recipient_map_v1",
    )


class _USAspendingFakeClient:
    def __init__(self, *, with_award: bool = False) -> None:
        self.with_award = with_award

    def fetch_last_updated(self) -> dict[str, object]:
        return {"last_updated": "2026-06-20"}

    def search_spending_by_award(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["recipient_uei"] == UEI
        if not self.with_award:
            return {"results": [], "page_metadata": {"hasNext": False}}
        return {
            "results": [
                {
                    "generated_internal_id": "lookup-1",
                    "Award ID": "AWARD-lookup-1",
                    "Recipient UEI": UEI,
                    "Recipient Name": "EXACT LEGAL NAME",
                    "Last Modified Date": "2026-06-20",
                    "Base Obligation Date": "2026-06-20",
                    "Award Amount": 100.0,
                    "Contract Award Type": "A",
                    "NAICS": "541330",
                    "PSC": "R425",
                }
            ],
            "page_metadata": {"hasNext": False},
        }

    def fetch_award_detail(self, award_id: str) -> dict[str, object]:
        assert award_id == "lookup-1"
        return {
            "generated_unique_award_id": "CONT_AWD_1",
            "recipient": {"recipient_uei": UEI, "recipient_name": "EXACT LEGAL NAME"},
            "type": "A",
            "type_description": "Definitive Contract",
            "category": "contract",
            "description": "Official factual award description.",
            "date_signed": "2026-06-20",
            "action_date": "2026-06-20",
            "last_modified_date": "2026-06-20",
            "period_of_performance_start": "2026-06-20",
            "period_of_performance_current_end_date": "2026-12-31",
            "total_obligation": 100.0,
            "base_exercised_options": 100.0,
            "base_and_all_options": 150.0,
            "awarding_agency": {"name": "Agency A", "code": "AA"},
            "funding_agency": {"name": "Agency F", "code": "FF"},
            "latest_transaction_contract_data": {
                "naics": "541330",
                "naics_description": "Engineering Services",
                "product_or_service_code": "R425",
                "product_or_service_description": "Engineering services",
            },
        }

    def fetch_award_funding(self, award_id: str, *, limit: int) -> dict[str, object]:
        assert award_id == "CONT_AWD_1"
        assert limit == 100
        return {
            "results": [
                {
                    "transaction_obligated_amount": 100.0,
                    "reporting_fiscal_year": 2026,
                    "reporting_fiscal_quarter": 3,
                    "reporting_fiscal_month": 6,
                    "awarding_agency_name": "Agency A",
                    "funding_agency_name": "Agency F",
                    "federal_account": "000-0000",
                    "account_title": "Operations",
                    "program_activity_code": "0001",
                    "program_activity_name": "Program",
                    "object_class": "25.2",
                    "object_class_name": "Services",
                }
            ],
            "page_metadata": {"hasNext": False},
        }


def _yfinance_frame() -> pd.DataFrame:
    index = pd.date_range(
        start=datetime(2026, 1, 2, 14, 0, tzinfo=UTC),
        periods=14,
        freq="5min",
        tz="UTC",
    )
    data: dict[tuple[str, str], list[float]] = {}
    for symbol, start in (("SPY", 500.0), ("XLE", 100.0)):
        data[("Close", symbol)] = [start + index for index in range(14)]
    return pd.DataFrame(data, index=index)
