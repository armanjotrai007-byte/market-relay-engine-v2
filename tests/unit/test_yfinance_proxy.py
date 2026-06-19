from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math

import pandas as pd
import pytest

import scripts.check_yfinance_proxy as check_yfinance_proxy
from market_relay_engine.context.state_cache import (
    ContextScope,
    ContextStateCache,
    ContextStateCacheError,
    make_global_context_entry,
    make_sector_context_entry,
)
from market_relay_engine.context.yfinance_proxy import (
    DIGEST_PREFIX_HEX_LENGTH,
    SOURCE_NAME,
    ProxySymbolRegistration,
    YFinanceProxyCollectionResult,
    YFinanceProxyCollectionStatus,
    YFinanceProxyCollector,
    YFinanceProxyConfig,
    YFinanceProxyError,
    YFinanceProxyIssue,
    build_proxy_registry,
    cache_indicator_name,
    deterministic_context_indicator_id,
    get_proxy_indicator,
    get_sector_proxy_indicators,
)
from market_relay_engine.contracts.context import ContextIndicatorSnapshot
from market_relay_engine.questdb.writer import QuestDBLedgerWriter, context_indicator_snapshot_to_row

BASE_TIME = datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC)
START_TIME = datetime(2026, 1, 2, 14, 0, tzinfo=UTC)


def _frame(closes: list[float | int | None], *, start: datetime = START_TIME) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": closes},
        index=pd.date_range(start=start, periods=len(closes), freq="5min", tz="UTC"),
    )


def _frame_without_close(periods: int = 13, *, start: datetime = START_TIME) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [100.0 + index for index in range(periods)],
            "High": [101.0 + index for index in range(periods)],
            "Low": [99.0 + index for index in range(periods)],
            "Volume": [1000 + index for index in range(periods)],
        },
        index=pd.date_range(start=start, periods=periods, freq="5min", tz="UTC"),
    )


def _multi_frame(symbol_closes: dict[str, list[float | int | None]], *, start: datetime = START_TIME) -> pd.DataFrame:
    lengths = {len(values) for values in symbol_closes.values()}
    assert len(lengths) == 1
    index = pd.date_range(start=start, periods=lengths.pop(), freq="5min", tz="UTC")
    data = {
        ("Close", symbol): values
        for symbol, values in symbol_closes.items()
    }
    return pd.DataFrame(data, index=index)


def _multi_frame_missing_close(symbols: tuple[str, ...], *, periods: int = 13, start: datetime = START_TIME) -> pd.DataFrame:
    index = pd.date_range(start=start, periods=periods, freq="5min", tz="UTC")
    data = {
        ("Open", symbol): [100.0 + offset for offset in range(periods)]
        for symbol in symbols
    }
    return pd.DataFrame(data, index=index)


def _mixed_multi_frame_with_missing_close(*, periods: int = 13, start: datetime = START_TIME) -> pd.DataFrame:
    index = pd.date_range(start=start, periods=periods, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            ("Close", "XLE"): [100.0 for _ in range(periods)],
            ("Open", "XOP"): [101.0 for _ in range(periods)],
        },
        index=index,
    )


def _config(*symbols: str, enabled: bool = True, **overrides: object) -> YFinanceProxyConfig:
    registry = build_proxy_registry(None)
    requested = tuple(symbol.upper() for symbol in symbols) or ("XLE",)
    return YFinanceProxyConfig(
        enabled=enabled,
        requested_symbols=requested,
        registry=tuple(registry[symbol] for symbol in requested),
        **overrides,
    )


def _collector(frame: pd.DataFrame, *, config: YFinanceProxyConfig | None = None, now: datetime = BASE_TIME, writer: object | None = None) -> YFinanceProxyCollector:
    def download(**_: object) -> pd.DataFrame:
        return frame

    return YFinanceProxyCollector(
        cache=ContextStateCache(),
        config=config or _config("XLE"),
        download=download,
        clock=lambda: now,
        ledger_writer=writer,  # type: ignore[arg-type]
    )


def _snapshot(index: int = 0) -> ContextIndicatorSnapshot:
    return ContextIndicatorSnapshot(
        snapshot_time=BASE_TIME,
        source=SOURCE_NAME,
        ticker_or_sector="XLE",
        indicator_name=f"return_{index}",
        value=0.01,
        context_indicator_id=f"context_indicator_cli_{index}",
        window="5m",
        units="return",
        source_event_time=BASE_TIME - timedelta(minutes=5),
    )


def _cli_result(
    *,
    status: YFinanceProxyCollectionStatus = YFinanceProxyCollectionStatus.SUCCESS,
    indicator_count: int = 1,
    ledger_count: int = 1,
    issues: tuple[YFinanceProxyIssue, ...] = (),
) -> YFinanceProxyCollectionResult:
    snapshots = tuple(_snapshot(index) for index in range(indicator_count))
    return YFinanceProxyCollectionResult(
        status=status,
        started_at=BASE_TIME,
        completed_at=BASE_TIME,
        requested_symbols=("XLE",),
        successful_symbols=("XLE",) if indicator_count else (),
        failed_symbols=(),
        stale_symbols=(),
        issues=issues,
        indicator_snapshots=snapshots,
        cache_update_results=(),
        ledger_write_results=tuple({"ok": index} for index in range(ledger_count)),
    )


class _RecordingWriter:
    def __init__(self) -> None:
        self.snapshots: list[ContextIndicatorSnapshot] = []

    def write_context_indicator_snapshot(self, snapshot: ContextIndicatorSnapshot, **kwargs: object) -> object:
        self.snapshots.append(snapshot)
        return {"id": snapshot.context_indicator_id, "kwargs": kwargs}


def _missing_close_issues(result: YFinanceProxyCollectionResult) -> list[YFinanceProxyIssue]:
    return [issue for issue in result.issues if issue.issue_type == "MISSING_CLOSE_COLUMN"]


def test_grace_staleness_validation_and_no_boundary_blackout() -> None:
    assert YFinanceProxyConfig(bar_completion_grace_seconds=30, max_staleness_seconds=330)
    assert YFinanceProxyConfig(bar_completion_grace_seconds=30, max_staleness_seconds=360)
    with pytest.raises(YFinanceProxyError):
        YFinanceProxyConfig(bar_completion_grace_seconds=30, max_staleness_seconds=329)

    frame = _frame([100.0 + index for index in range(14)])
    collector = _collector(
        frame,
        config=_config("XLE", max_staleness_seconds=330),
        now=datetime(2026, 1, 2, 15, 10, 20, tzinfo=UTC),
    )

    result = collector.collect()

    assert result.status is YFinanceProxyCollectionStatus.SUCCESS
    latest = next(snapshot for snapshot in result.indicator_snapshots if snapshot.indicator_name == "latest_close")
    assert latest.source_event_time == datetime(2026, 1, 2, 15, 5, tzinfo=UTC)
    assert latest.value == 112.0


def test_scalar_cache_values_are_json_safe_without_truthiness_rejection() -> None:
    accepted = [0, 0.0, False, 0.015, -0.02, " risk_off "]
    for value in accepted:
        entry = make_global_context_entry(name=f"value_{len(str(value))}", value=value, updated_at=BASE_TIME)
        if isinstance(value, str):
            assert entry.value == value.strip()
        else:
            assert entry.value == value

    rejected = ["", "   ", None, float("nan"), float("inf"), [1], {"x": 1}]
    for value in rejected:
        with pytest.raises(ContextStateCacheError):
            make_global_context_entry(name="bad", value=value, updated_at=BASE_TIME)  # type: ignore[arg-type]


def test_numeric_retrieval_preserves_sector_proxy_identity_and_expiry() -> None:
    registry = build_proxy_registry(None)
    cache = ContextStateCache()
    source_event_time = BASE_TIME - timedelta(minutes=1)
    future = BASE_TIME + timedelta(minutes=5)
    for symbol, value in (("XLE", 0.01), ("XOP", -0.02), ("OIH", 0.0)):
        cache.update(
            make_sector_context_entry(
                sector="OIL",
                name=cache_indicator_name(symbol, "return_5m", "5m"),
                value=value,
                updated_at=BASE_TIME,
                source=SOURCE_NAME,
                source_event_time=source_event_time,
                valid_until=future,
                details={"context_indicator_id": f"context_indicator_{symbol.lower()}"},
            )
        )

    xle = get_proxy_indicator(cache, registry, symbol="XLE", indicator_name="return_5m", window="5m", now=BASE_TIME)
    assert xle is not None
    assert xle.symbol == "XLE"
    assert xle.sector == "OIL"
    assert xle.value == 0.01

    oil = get_sector_proxy_indicators(cache, registry, sector="oil", indicator_name="return_5m", window="5m", now=BASE_TIME)
    assert set(oil) == {"XLE", "XOP", "OIH"}
    assert get_sector_proxy_indicators(cache, registry, sector="OIL", indicator_name="return_5m", window="5m", now=BASE_TIME).keys() == oil.keys()
    assert get_sector_proxy_indicators(cache, registry, sector="ENERGY", indicator_name="return_5m", window="5m", now=BASE_TIME) == {}

    cache.update(
        make_sector_context_entry(
            sector="OIL",
            name=cache_indicator_name("XLE", "latest_close", "5m"),
            value="not numeric",
            updated_at=BASE_TIME,
            source_event_time=source_event_time,
            valid_until=future,
            details={"context_indicator_id": "context_indicator_bad"},
        )
    )
    assert get_proxy_indicator(cache, registry, symbol="XLE", indicator_name="latest_close", window="5m", now=BASE_TIME) is None

    cache.update(
        make_sector_context_entry(
            sector="OIL",
            name=cache_indicator_name("XLE", "return_15m", "15m"),
            value=0.1,
            updated_at=BASE_TIME,
            source_event_time=source_event_time,
            valid_until=BASE_TIME - timedelta(seconds=1),
            details={"context_indicator_id": "context_indicator_expired"},
        )
    )
    assert get_proxy_indicator(cache, registry, symbol="XLE", indicator_name="return_15m", window="15m", now=BASE_TIME) is None
    assert get_proxy_indicator(cache, registry, symbol="XLE", indicator_name="return_15m", window="15m", now=BASE_TIME, include_expired=True) is not None


def test_oil_proxy_registry_matches_configured_tradable_sector() -> None:
    registry = build_proxy_registry(None)
    assert {registry[symbol].sector for symbol in ("XLE", "XOP", "OIH")} == {"OIL"}
    assert registry["XLI"].sector == "INDUSTRIALS"
    assert {registry[symbol].sector for symbol in ("PPA", "ITA")} == {"DEFENSE"}

    collector = _collector(
        _multi_frame(
            {
                "XLE": [100.0 for _ in range(13)],
                "XOP": [101.0 for _ in range(13)],
                "OIH": [102.0 for _ in range(13)],
            }
        ),
        config=_config("XLE", "XOP", "OIH"),
    )
    result = collector.collect()
    assert result.status is YFinanceProxyCollectionStatus.SUCCESS

    for symbol in ("XLE", "XOP", "OIH"):
        name = cache_indicator_name(symbol, "return_5m", "5m")
        assert collector.cache.get_sector("OIL", name, now=BASE_TIME) is not None
        assert collector.cache.get_sector("ENERGY", name, now=BASE_TIME) is None

    configured_sector_from_tradable_symbol = "oil"
    readings = get_sector_proxy_indicators(
        collector.cache,
        registry,
        sector=configured_sector_from_tradable_symbol,
        indicator_name="return_5m",
        window="5m",
        now=BASE_TIME,
    )
    assert set(readings) == {"XLE", "XOP", "OIH"}


def test_return_guard_omits_only_invalid_target_close() -> None:
    closes = [100.0 for _ in range(13)]
    closes[-2] = 0.0
    closes[-1] = 101.0
    result = _collector(_frame(closes)).collect()

    names = {snapshot.indicator_name for snapshot in result.indicator_snapshots}
    issue_windows = {(issue.issue_type, issue.window) for issue in result.issues}
    assert "latest_close" in names
    assert "return_15m" in names
    assert "return_60m" in names
    assert "return_5m" not in names
    assert ("INVALID_TARGET_CLOSE", "5m") in issue_windows
    assert result.status is YFinanceProxyCollectionStatus.PARTIAL


@pytest.mark.parametrize("target_close", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_target_close_values_do_not_crash_collection(target_close: float) -> None:
    closes = [100.0 for _ in range(13)]
    closes[-2] = target_close
    closes[-1] = 101.0
    result = _collector(_frame(closes)).collect()

    assert result.status is YFinanceProxyCollectionStatus.PARTIAL
    assert any(issue.issue_type == "INVALID_TARGET_CLOSE" for issue in result.issues)
    assert any(snapshot.indicator_name == "latest_close" for snapshot in result.indicator_snapshots)


def test_valid_zero_return_is_cached_successfully() -> None:
    collector = _collector(_frame([100.0 for _ in range(13)]))
    result = collector.collect()

    return_snapshot = next(snapshot for snapshot in result.indicator_snapshots if snapshot.indicator_name == "return_5m")
    assert return_snapshot.value == 0.0
    cache_entry = collector.cache.get_sector("OIL", cache_indicator_name("XLE", "return_5m", "5m"), now=BASE_TIME)
    assert cache_entry is not None
    assert cache_entry.value == 0.0


def test_invalid_latest_completed_close_uses_previous_valid_bar_without_crashing() -> None:
    closes = [100.0 for _ in range(13)]
    closes[-1] = float("nan")
    result = _collector(
        _frame(closes),
        now=datetime(2026, 1, 2, 15, 5, 40, tzinfo=UTC),
    ).collect()

    assert result.status in {YFinanceProxyCollectionStatus.SUCCESS, YFinanceProxyCollectionStatus.PARTIAL}
    latest = next(snapshot for snapshot in result.indicator_snapshots if snapshot.indicator_name == "latest_close")
    assert latest.source_event_time == datetime(2026, 1, 2, 15, 0, tzinfo=UTC)


def test_status_taxonomy_failed_partial_and_no_fresh_data() -> None:
    stale_result = _collector(_frame([100.0 for _ in range(13)]), now=BASE_TIME + timedelta(hours=3)).collect()
    assert stale_result.status is YFinanceProxyCollectionStatus.NO_FRESH_DATA
    assert stale_result.stale_symbols == ("XLE",)

    def failing_download(**_: object) -> pd.DataFrame:
        raise RuntimeError("source unavailable")

    failed = YFinanceProxyCollector(
        cache=ContextStateCache(),
        config=_config("XLE"),
        download=failing_download,
        clock=lambda: BASE_TIME,
    ).collect()
    assert failed.status is YFinanceProxyCollectionStatus.FAILED

    mixed = _collector(
        _multi_frame(
            {
                "XLE": [100.0 for _ in range(13)],
                "XOP": [100.0] + [math.nan for _ in range(12)],
            }
        ),
        config=_config("XLE", "XOP"),
    ).collect()
    assert mixed.status is YFinanceProxyCollectionStatus.PARTIAL
    assert "XLE" in mixed.successful_symbols
    assert "XOP" in mixed.stale_symbols


def test_missing_close_one_level_single_symbol_fails_without_side_effects() -> None:
    cache = ContextStateCache()
    writer = _RecordingWriter()
    collector = YFinanceProxyCollector(
        cache=cache,
        config=_config("XLE"),
        download=lambda **_: _frame_without_close(),
        clock=lambda: BASE_TIME,
        ledger_writer=writer,
    )

    result = collector.collect(write_questdb=True)

    assert result.status is YFinanceProxyCollectionStatus.FAILED
    assert result.failed_symbols == ("XLE",)
    assert result.indicator_snapshots == ()
    assert result.cache_update_results == ()
    assert result.ledger_write_results == ()
    assert writer.snapshots == []
    assert cache.snapshot(now=BASE_TIME, include_expired=True)["entry_count"] == 0
    issues = _missing_close_issues(result)
    assert [(issue.symbol, issue.message) for issue in issues] == [
        ("XLE", "Source response for XLE does not contain a Close column")
    ]


def test_missing_close_partial_collection_keeps_valid_symbol_and_ledger_writes() -> None:
    cache = ContextStateCache()
    writer = _RecordingWriter()
    collector = YFinanceProxyCollector(
        cache=cache,
        config=_config("XLE", "XOP"),
        download=lambda **_: _mixed_multi_frame_with_missing_close(),
        clock=lambda: BASE_TIME,
        ledger_writer=writer,
    )

    result = collector.collect(write_questdb=True)

    assert result.status is YFinanceProxyCollectionStatus.PARTIAL
    assert result.successful_symbols == ("XLE",)
    assert result.failed_symbols == ("XOP",)
    assert [issue.symbol for issue in _missing_close_issues(result)] == ["XOP"]
    assert {snapshot.ticker_or_sector for snapshot in result.indicator_snapshots} == {"XLE"}
    assert len(writer.snapshots) == len(result.indicator_snapshots)
    assert cache.get_sector("OIL", cache_indicator_name("XLE", "return_5m", "5m"), now=BASE_TIME) is not None
    assert cache.get_sector("OIL", cache_indicator_name("XOP", "latest_close", "5m"), now=BASE_TIME) is None


def test_all_missing_close_multiindex_symbols_fail_without_raw_exception() -> None:
    result = _collector(
        _multi_frame_missing_close(("XLE", "XOP")),
        config=_config("XLE", "XOP"),
    ).collect()

    assert result.status is YFinanceProxyCollectionStatus.FAILED
    assert result.failed_symbols == ("XLE", "XOP")
    assert result.indicator_snapshots == ()
    assert result.cache_update_results == ()
    assert [issue.symbol for issue in _missing_close_issues(result)] == ["XLE", "XOP"]


def test_missing_close_individual_fallback_is_not_retried_again() -> None:
    calls: list[tuple[str, ...]] = []

    def download(**kwargs: object) -> pd.DataFrame:
        tickers = kwargs["tickers"]
        assert isinstance(tickers, tuple)
        calls.append(tickers)
        if len(calls) == 1:
            return pd.DataFrame()
        return _frame_without_close()

    result = YFinanceProxyCollector(
        cache=ContextStateCache(),
        config=_config("XLE"),
        download=download,
        clock=lambda: BASE_TIME,
    ).collect()

    assert calls == [("XLE",), ("XLE",)]
    assert result.status is YFinanceProxyCollectionStatus.FAILED
    assert result.failed_symbols == ("XLE",)
    assert [issue.symbol for issue in _missing_close_issues(result)] == ["XLE"]
    assert result.indicator_snapshots == ()


def test_missing_close_multiindex_extraction_uses_shared_structured_issue() -> None:
    result = _collector(
        _multi_frame_missing_close(("XLE",)),
        config=_config("XLE"),
    ).collect()

    assert result.status is YFinanceProxyCollectionStatus.FAILED
    assert result.failed_symbols == ("XLE",)
    assert [issue.issue_type for issue in result.issues] == ["MISSING_CLOSE_COLUMN"]
    assert result.indicator_snapshots == ()


def test_required_questdb_without_writer_raises_before_source_or_cache_work() -> None:
    download_called = False
    cache = ContextStateCache()

    def download(**_: object) -> pd.DataFrame:
        nonlocal download_called
        download_called = True
        return _frame([100.0 for _ in range(13)])

    collector = YFinanceProxyCollector(
        cache=cache,
        config=_config("XLE"),
        download=download,
        clock=lambda: BASE_TIME,
        ledger_writer=None,
    )

    with pytest.raises(YFinanceProxyError, match="required.*ledger writer|ledger writer.*required"):
        collector.collect(write_questdb=True, questdb_required=True)

    assert download_called is False
    assert cache.snapshot(now=BASE_TIME, include_expired=True)["entry_count"] == 0


def test_no_writer_without_questdb_write_still_collects_normally() -> None:
    result = _collector(_frame([100.0 for _ in range(13)]), writer=None).collect(write_questdb=False)

    assert result.status is YFinanceProxyCollectionStatus.SUCCESS
    assert result.indicator_snapshots
    assert result.ledger_write_results == ()


def test_writer_protocol_optional_and_required_failures() -> None:
    writer = _RecordingWriter()
    result = _collector(_frame([100.0 for _ in range(13)]), writer=writer).collect(write_questdb=True, questdb_required=True, run_id="run_test")
    assert result.status is YFinanceProxyCollectionStatus.SUCCESS
    assert len(writer.snapshots) == len(result.indicator_snapshots)
    assert result.ledger_write_results
    assert hasattr(QuestDBLedgerWriter, "write_context_indicator_snapshot")

    class FailingWriter:
        def write_context_indicator_snapshot(self, snapshot: ContextIndicatorSnapshot, **kwargs: object) -> object:
            raise RuntimeError("write failed")

    optional = _collector(_frame([100.0 for _ in range(13)]), writer=FailingWriter()).collect(write_questdb=True)
    assert optional.status is YFinanceProxyCollectionStatus.PARTIAL
    assert any(issue.issue_type == "LEDGER_WRITE_FAILED" for issue in optional.issues)

    with pytest.raises(YFinanceProxyError):
        _collector(_frame([100.0 for _ in range(13)]), writer=FailingWriter()).collect(write_questdb=True, questdb_required=True)


def test_deterministic_identity_and_snapshot_compatibility() -> None:
    source_event_time = datetime(2026, 1, 2, 15, 5, tzinfo=UTC)
    first = deterministic_context_indicator_id(SOURCE_NAME, "XLE", "return_5m", "5m", source_event_time)
    second = deterministic_context_indicator_id(SOURCE_NAME, "xle", "return_5m", "5m", source_event_time)
    different_bar = deterministic_context_indicator_id(SOURCE_NAME, "XLE", "return_5m", "5m", source_event_time + timedelta(minutes=5))

    assert first == second
    assert first != different_bar
    assert first.startswith("context_indicator_")
    assert len(first.removeprefix("context_indicator_")) == DIGEST_PREFIX_HEX_LENGTH

    legacy_snapshot = ContextIndicatorSnapshot(
        snapshot_time=BASE_TIME,
        source="fixture",
        ticker_or_sector="SPY",
        indicator_name="latest_close",
        value=100.0,
    )
    assert legacy_snapshot.context_indicator_id.startswith("context_indicator_")

    fixed_snapshot = ContextIndicatorSnapshot(
        snapshot_time=BASE_TIME,
        source="fixture",
        ticker_or_sector="SPY",
        indicator_name="latest_close",
        value=100.0,
        context_indicator_id="context_indicator_fixed",
    )
    row = context_indicator_snapshot_to_row(fixed_snapshot, write_time=BASE_TIME)
    assert row["context_indicator_id"] == "context_indicator_fixed"


def test_disabled_collector_makes_no_source_calls() -> None:
    calls = 0

    def download(**_: object) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return _frame([100.0])

    result = YFinanceProxyCollector(
        cache=ContextStateCache(),
        config=_config("XLE", enabled=False),
        download=download,
        clock=lambda: BASE_TIME,
    ).collect(write_questdb=True)

    assert result.status is YFinanceProxyCollectionStatus.DISABLED
    assert calls == 0


def test_registry_rejects_ticker_scope_sector_combo() -> None:
    with pytest.raises(YFinanceProxyError):
        ProxySymbolRegistration(symbol="XLE", scope=ContextScope.TICKER, sector="OIL")


def test_live_without_write_questdb_does_not_require_questdb(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def fake_run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
        calls.append(write_questdb)
        return _cli_result(indicator_count=1, ledger_count=0)

    monkeypatch.setattr(check_yfinance_proxy, "_run_live", fake_run_live)

    assert check_yfinance_proxy.main(["--live"]) == 0
    assert calls == [False]


def test_live_write_questdb_exits_nonzero_when_required_write_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
        assert write_questdb is True
        raise YFinanceProxyError("write failed")

    monkeypatch.setattr(check_yfinance_proxy, "_run_live", fake_run_live)

    assert check_yfinance_proxy.main(["--live", "--write-questdb"]) == 1


def test_live_write_questdb_exits_nonzero_when_writer_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
        assert write_questdb is True
        raise check_yfinance_proxy.QuestDBWriteError("bad writer config")

    monkeypatch.setattr(check_yfinance_proxy, "_run_live", fake_run_live)

    assert check_yfinance_proxy.main(["--live", "--write-questdb"]) == 1


def test_live_write_questdb_exits_nonzero_on_ledger_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    issue = YFinanceProxyIssue(issue_type="LEDGER_WRITE_FAILED", message="insert failed", symbol="XLE", window="5m")

    def fake_run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
        assert write_questdb is True
        return _cli_result(status=YFinanceProxyCollectionStatus.PARTIAL, indicator_count=1, ledger_count=0, issues=(issue,))

    monkeypatch.setattr(check_yfinance_proxy, "_run_live", fake_run_live)

    assert check_yfinance_proxy.main(["--live", "--write-questdb"]) == 1


def test_live_write_questdb_exits_nonzero_when_no_rows_written(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
        assert write_questdb is True
        return _cli_result(indicator_count=1, ledger_count=0)

    monkeypatch.setattr(check_yfinance_proxy, "_run_live", fake_run_live)

    assert check_yfinance_proxy.main(["--live", "--write-questdb"]) == 1


def test_live_write_questdb_exits_zero_when_writes_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_live(*, write_questdb: bool) -> YFinanceProxyCollectionResult:
        assert write_questdb is True
        return _cli_result(indicator_count=2, ledger_count=2)

    monkeypatch.setattr(check_yfinance_proxy, "_run_live", fake_run_live)

    assert check_yfinance_proxy.main(["--live", "--write-questdb"]) == 0
