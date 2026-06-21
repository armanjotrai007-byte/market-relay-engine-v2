from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path

import pytest
import yaml

from market_relay_engine.context.fred_collector import (
    EXPECTED_SERIES_IDS,
    FREDClient,
    FREDCollectionStatus,
    FREDCollector,
    FREDCollectorError,
    FREDConfig,
    FREDSeriesStatus,
)
from market_relay_engine.context.state_cache import (
    ContextStateCache,
    ContextStateUpdateStatus,
    make_global_context_entry,
)
from market_relay_engine.contracts.context import ContextFlag


REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED_AT = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
LATEST_DATE = "2026-06-19"
PRIOR_DATE = "2026-06-16"


def _rows(
    latest: str,
    prior: str,
    *,
    latest_date: str = LATEST_DATE,
    prior_date: str = PRIOR_DATE,
    realtime: str = "ignored-vintage-a",
) -> list[dict[str, object]]:
    return [
        {"date": latest_date, "value": latest, "realtime_start": realtime},
        {"date": prior_date, "value": prior, "realtime_end": realtime},
    ]


def _payloads() -> dict[str, list[dict[str, object]]]:
    return {
        "DGS3MO": _rows("4.20", "4.10"),
        "DGS2": _rows("4.00", "3.95"),
        "DGS10": _rows("4.35", "4.25"),
    }


class FakeClient:
    def __init__(
        self,
        payloads: dict[str, list[dict[str, object]]] | None = None,
        *,
        failures: set[str] | None = None,
    ) -> None:
        self.payloads = deepcopy(_payloads() if payloads is None else payloads)
        self.failures = set() if failures is None else set(failures)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch_observations(self, series_id: str, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append((series_id, dict(kwargs)))
        if series_id in self.failures:
            raise RuntimeError("source failure with hidden-secret-value")
        return deepcopy(self.payloads.get(series_id, []))


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.snapshots: list[object] = []

    def write_context_indicator_snapshot(self, snapshot: object, **kwargs: object) -> str:
        if self.fail:
            raise RuntimeError("writer failure with hidden-secret-value")
        self.snapshots.append(snapshot)
        return "written"


def _collector(
    *,
    cache: ContextStateCache | None = None,
    client: FakeClient | None = None,
    writer: FakeWriter | None = None,
    config: FREDConfig | None = None,
) -> FREDCollector:
    return FREDCollector(
        cache=ContextStateCache() if cache is None else cache,
        config=FREDConfig(enabled=True) if config is None else config,
        client=FakeClient() if client is None else client,
        ledger_writer=writer,
    )


def _values(result: object) -> dict[str, object]:
    return {item.indicator_name: item.value for item in result.indicator_snapshots}


def test_repository_configuration_is_exact_and_disabled_by_default() -> None:
    loaded = yaml.safe_load((REPO_ROOT / "config" / "context_sources.yaml").read_text(encoding="utf-8"))
    config = FREDConfig.from_repository_config(loaded)

    assert config.enabled is False
    assert config.api_key_env == "FRED_API_KEY"
    assert config.observation_fetch_limit == 20
    assert config.max_observation_age_calendar_days == 5
    assert config.series_ids == EXPECTED_SERIES_IDS


def test_disabled_collection_does_not_use_key_or_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    client = FakeClient(failures=set(EXPECTED_SERIES_IDS.values()))
    result = FREDCollector(
        cache=ContextStateCache(),
        config=FREDConfig(),
        client=client,
    ).collect(evaluation_time=CHECKED_AT)

    assert result.status is FREDCollectionStatus.DISABLED
    assert client.calls == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"enabled": "false"}, "enabled"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": float("nan")}, "timeout_seconds"),
        ({"max_observation_age_calendar_days": -1}, "max_observation"),
        ({"max_observation_age_calendar_days": True}, "max_observation"),
        ({"observation_fetch_limit": 3}, "observation_fetch_limit"),
        ({"observation_fetch_limit": 51}, "observation_fetch_limit"),
        ({"observation_fetch_limit": 20.0}, "observation_fetch_limit"),
        ({"series_ids": {"us_treasury_3m_yield": "DGS3MO"}}, "series_ids"),
        ({"used_in_per_tick_loop": True}, "used_in_per_tick_loop"),
    ],
)
def test_strict_config_validation(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(FREDCollectorError, match=match):
        FREDConfig(**kwargs)


def test_repository_config_rejects_missing_and_unexpected_fields() -> None:
    source = {
        "enabled": False,
        "api_key_env": "FRED_API_KEY",
        "purpose": "Slow daily Treasury-rate context.",
        "feeds_memory_cache": True,
        "writes_questdb_ledger": True,
        "used_in_per_tick_loop": False,
        "timeout_seconds": 10.0,
        "max_observation_age_calendar_days": 5,
        "observation_fetch_limit": 20,
        "series_ids": dict(EXPECTED_SERIES_IDS),
    }
    missing = deepcopy(source)
    del missing["timeout_seconds"]
    with pytest.raises(FREDCollectorError, match="timeout_seconds"):
        FREDConfig.from_repository_config({"structured_sources": {"fred": missing}})
    unexpected = deepcopy(source)
    unexpected["extra"] = True
    with pytest.raises(FREDCollectorError, match="unexpected"):
        FREDConfig.from_repository_config({"structured_sources": {"fred": unexpected}})


def test_every_request_is_explicit_bounded_and_uses_exact_series() -> None:
    client = FakeClient()
    result = _collector(client=client).collect(evaluation_time=CHECKED_AT)

    assert result.status is FREDCollectionStatus.SUCCESS
    assert [series_id for series_id, _ in client.calls] == ["DGS3MO", "DGS2", "DGS10"]
    assert all(
        params == {
            "file_type": "json",
            "sort_order": "desc",
            "order_by": "observation_date",
            "limit": 20,
        }
        for _, params in client.calls
    )


def test_selection_noise_is_silent_when_two_valid_dates_remain() -> None:
    payloads = _payloads()
    for series_id, rows in payloads.items():
        payloads[series_id] = [
            {"date": "2026-06-22", "value": "9.9"},
            {"date": "bad-date", "value": "4.9"},
            {"date": "2026-06-20", "value": "."},
            {"date": "2026-06-20", "value": ""},
            {"date": "2026-06-20", "value": "NaN"},
            {"date": "2026-06-20", "value": "Infinity"},
            rows[0],
            {"date": LATEST_DATE, "value": "999"},
            rows[1],
        ]
    result = _collector(client=FakeClient(payloads)).collect(evaluation_time=CHECKED_AT)

    assert result.status is FREDCollectionStatus.SUCCESS
    assert result.issues == ()
    assert len(result.indicator_snapshots) == 10


def test_happy_path_values_units_scope_and_regime() -> None:
    result = _collector().collect(evaluation_time=CHECKED_AT)
    values = _values(result)

    assert result.status is FREDCollectionStatus.SUCCESS
    assert len(result.indicator_snapshots) == 10
    assert values["us_treasury_3m_yield"] == 4.2
    assert values["us_treasury_2y_yield"] == 4.0
    assert values["us_treasury_10y_yield"] == 4.35
    assert values["us_treasury_2y_minus_3m"] == -0.2
    assert values["us_treasury_10y_minus_2y"] == 0.35
    assert values["us_treasury_10y_minus_3m"] == 0.15
    assert values["us_treasury_3m_yield_change_prev_valid_obs"] == 0.1
    assert values["us_treasury_2y_yield_change_prev_valid_obs"] == 0.05
    assert values["us_treasury_10y_yield_change_prev_valid_obs"] == 0.1
    assert values["rate_curve_regime_v1"] == "FRONT_INVERTED__LONG_POSITIVE"
    assert all(item.ticker_or_sector == "GLOBAL" for item in result.indicator_snapshots)
    assert {item.units for item in result.indicator_snapshots if "yield" in item.indicator_name and "change" not in item.indicator_name} == {"percent"}
    assert next(item for item in result.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1").units == "category"


def test_zero_spreads_are_positive_in_regime() -> None:
    payloads = {
        series_id: _rows("4.00", "3.90") for series_id in EXPECTED_SERIES_IDS.values()
    }
    result = _collector(client=FakeClient(payloads)).collect(evaluation_time=CHECKED_AT)
    assert _values(result)["rate_curve_regime_v1"] == "FRONT_POSITIVE__LONG_POSITIVE"


def test_provenance_uses_midnight_convention_without_realtime_fields() -> None:
    result = _collector().collect(evaluation_time=CHECKED_AT)
    expected_source_time = datetime(2026, 6, 19, tzinfo=UTC)

    for snapshot in result.indicator_snapshots:
        assert snapshot.source_event_time == expected_source_time
        assert snapshot.details["source"] == "fred_rates_v1"
        assert snapshot.details["source_event_time_basis"] == "observation_date_utc_midnight_convention"
        assert snapshot.details["availability_basis"] == "collector_observed"
        assert snapshot.details["research_asof_eligible"] is False
        assert snapshot.details["vintage_tracking_mode"] == "current_fred_unpinned_v1"
        encoded = json.dumps(snapshot.details)
        assert "realtime_start" not in encoded
        assert "realtime_end" not in encoded


def test_change_preserves_weekend_gap_and_is_not_time_normalized() -> None:
    result = _collector().collect(evaluation_time=CHECKED_AT)
    snapshot = next(
        item
        for item in result.indicator_snapshots
        if item.indicator_name == "us_treasury_3m_yield_change_prev_valid_obs"
    )

    assert snapshot.value == pytest.approx(0.1)
    assert snapshot.details["observation_date"] == "2026-06-19"
    assert snapshot.details["previous_observation_date"] == "2026-06-16"
    assert snapshot.details["observation_interval_calendar_days"] == 3
    assert snapshot.details["observation_interval_kind"] == "previous_valid_observation"


def test_new_york_freshness_and_valid_until_boundary() -> None:
    config = FREDConfig(enabled=True, max_observation_age_calendar_days=5)
    final_instant = datetime(2026, 6, 25, 3, 59, 59, 999999, tzinfo=UTC)
    current = _collector(config=config).collect(evaluation_time=final_instant)
    stale = _collector(config=config).collect(
        evaluation_time=final_instant + timedelta(microseconds=1)
    )

    assert current.status is FREDCollectionStatus.SUCCESS
    assert {item.details["valid_until"] for item in current.indicator_snapshots} == {
        "2026-06-25T03:59:59.999999Z"
    }
    assert stale.status is FREDCollectionStatus.STALE
    assert stale.indicator_snapshots == ()


def test_all_future_rows_are_no_valid_not_current() -> None:
    payloads = {
        series_id: _rows("4.0", "3.9", latest_date="2026-06-22", prior_date="2026-06-21")
        for series_id in EXPECTED_SERIES_IDS.values()
    }
    result = _collector(client=FakeClient(payloads)).collect(evaluation_time=CHECKED_AT)

    assert result.status is FREDCollectionStatus.FAILED
    assert {item.status for item in result.series_results} == {
        FREDSeriesStatus.NO_VALID_OBSERVATION
    }


def test_no_valid_and_missing_prior_statuses() -> None:
    no_valid = _payloads()
    no_valid["DGS10"] = [{"date": LATEST_DATE, "value": "."}]
    partial_no_valid = _collector(client=FakeClient(no_valid)).collect(evaluation_time=CHECKED_AT)
    assert partial_no_valid.status is FREDCollectionStatus.PARTIAL
    assert next(item for item in partial_no_valid.series_results if item.series_id == "DGS10").status is FREDSeriesStatus.NO_VALID_OBSERVATION

    no_prior = _payloads()
    no_prior["DGS10"] = [{"date": LATEST_DATE, "value": "4.35"}]
    partial_no_prior = _collector(client=FakeClient(no_prior)).collect(evaluation_time=CHECKED_AT)
    values = _values(partial_no_prior)
    assert partial_no_prior.status is FREDCollectionStatus.PARTIAL
    assert "us_treasury_10y_yield" in values
    assert "us_treasury_10y_yield_change_prev_valid_obs" not in values
    assert len(partial_no_prior.indicator_snapshots) == 9


def test_date_alignment_suppresses_only_affected_derived_facts() -> None:
    payloads = _payloads()
    payloads["DGS3MO"] = _rows("4.20", "4.10", latest_date="2026-06-18", prior_date="2026-06-17")
    result = _collector(client=FakeClient(payloads)).collect(evaluation_time=CHECKED_AT)
    values = _values(result)

    assert result.status is FREDCollectionStatus.PARTIAL
    assert "us_treasury_2y_minus_3m" not in values
    assert "us_treasury_10y_minus_3m" not in values
    assert "us_treasury_10y_minus_2y" in values
    assert "rate_curve_regime_v1" not in values
    assert all(name in values for name in EXPECTED_SERIES_IDS)
    assert {issue.issue_type for issue in result.issues} == {"DATE_MISALIGNED"}


def test_all_request_failures_and_all_reachable_unusable_are_failed() -> None:
    failed = _collector(
        client=FakeClient(failures={"DGS3MO", "DGS2", "DGS10"})
    ).collect(evaluation_time=CHECKED_AT)
    unusable = _collector(
        client=FakeClient(
            {series_id: [{"date": LATEST_DATE, "value": "."}] for series_id in EXPECTED_SERIES_IDS.values()}
        )
    ).collect(evaluation_time=CHECKED_AT)

    assert failed.status is FREDCollectionStatus.FAILED
    assert unusable.status is FREDCollectionStatus.FAILED
    assert failed.indicator_snapshots == unusable.indicator_snapshots == ()


def test_mixed_request_failure_with_no_usable_reachable_data_is_failed() -> None:
    payloads = _payloads()
    unusable_rows = [
        {"date": LATEST_DATE, "value": "."},
        {"date": LATEST_DATE, "value": ""},
        {"date": "bad-date", "value": "4.0"},
        {"date": PRIOR_DATE, "value": "NaN"},
        {"date": "2026-06-15", "value": "Infinity"},
    ]
    payloads["DGS3MO"] = deepcopy(unusable_rows)
    payloads["DGS2"] = deepcopy(unusable_rows)
    writer = FakeWriter()
    result = _collector(
        client=FakeClient(payloads, failures={"DGS10"}),
        writer=writer,
    ).collect(evaluation_time=CHECKED_AT, write_questdb=True)

    assert result.status is FREDCollectionStatus.FAILED
    assert {
        (issue.issue_type, issue.series_id) for issue in result.issues
    } >= {
        ("SOURCE_REQUEST_FAILED", "DGS10"),
        ("NO_VALID_OBSERVATION", "DGS3MO"),
        ("NO_VALID_OBSERVATION", "DGS2"),
    }
    assert result.indicator_snapshots == ()
    assert result.cache_update_results == ()
    assert writer.snapshots == []


def test_one_usable_series_with_failure_and_no_valid_series_is_partial() -> None:
    payloads = _payloads()
    payloads["DGS2"] = [{"date": LATEST_DATE, "value": "."}]
    result = _collector(
        client=FakeClient(payloads, failures={"DGS10"}),
    ).collect(evaluation_time=CHECKED_AT)
    names = {item.indicator_name for item in result.indicator_snapshots}

    assert result.status is FREDCollectionStatus.PARTIAL
    assert "us_treasury_3m_yield" in names
    assert "us_treasury_3m_yield_change_prev_valid_obs" in names
    assert not any("minus" in name for name in names)
    assert "rate_curve_regime_v1" not in names


def test_mixed_request_failure_and_current_or_stale_are_partial() -> None:
    partial_failure = _collector(client=FakeClient(failures={"DGS10"})).collect(
        evaluation_time=CHECKED_AT
    )
    mixed_payloads = _payloads()
    mixed_payloads["DGS10"] = _rows(
        "4.35", "4.25", latest_date="2026-06-10", prior_date="2026-06-09"
    )
    mixed_stale = _collector(client=FakeClient(mixed_payloads)).collect(
        evaluation_time=CHECKED_AT
    )

    assert partial_failure.status is FREDCollectionStatus.PARTIAL
    assert mixed_stale.status is FREDCollectionStatus.PARTIAL
    assert "hidden-secret-value" not in json.dumps([issue.__dict__ for issue in partial_failure.issues])


def test_request_failure_with_all_reachable_usable_stale_is_partial() -> None:
    payloads = {
        series_id: _rows("4.0", "3.9", latest_date="2026-06-10", prior_date="2026-06-09")
        for series_id in EXPECTED_SERIES_IDS.values()
    }
    result = _collector(
        client=FakeClient(payloads, failures={"DGS10"}),
    ).collect(evaluation_time=CHECKED_AT)

    assert result.status is FREDCollectionStatus.PARTIAL
    assert result.status is not FREDCollectionStatus.STALE


def test_all_reachable_valid_stale_is_stale_and_does_not_update_cache() -> None:
    payloads = {
        series_id: _rows("4.0", "3.9", latest_date="2026-06-10", prior_date="2026-06-09")
        for series_id in EXPECTED_SERIES_IDS.values()
    }
    cache = ContextStateCache()
    result = _collector(cache=cache, client=FakeClient(payloads)).collect(
        evaluation_time=CHECKED_AT
    )

    assert result.status is FREDCollectionStatus.STALE
    assert result.indicator_snapshots == ()
    assert cache.snapshot(now=CHECKED_AT)["entry_count"] == 0


def test_cache_and_ledger_are_idempotent_with_stable_first_collection() -> None:
    cache = ContextStateCache()
    client = FakeClient()
    writer = FakeWriter()
    collector = _collector(cache=cache, client=client, writer=writer)
    first = collector.collect(evaluation_time=CHECKED_AT, write_questdb=True)
    second_time = CHECKED_AT + timedelta(days=1)
    second = collector.collect(evaluation_time=second_time, write_questdb=True)

    assert len(writer.snapshots) == 10
    assert all(item.status is ContextStateUpdateStatus.WRITTEN for item in first.cache_update_results)
    assert all(item.status is ContextStateUpdateStatus.IGNORED_DUPLICATE for item in second.cache_update_results)
    assert second.checked_at == second_time
    assert {
        item.indicator_name: item.details["first_collected_at"]
        for item in second.indicator_snapshots
    } == {
        item.indicator_name: item.details["first_collected_at"]
        for item in first.indicator_snapshots
    }
    assert {
        cache.get_global(f"fred:{item.indicator_name}", now=second_time, include_expired=True).updated_at
        for item in second.indicator_snapshots
    } == {datetime(2026, 6, 19, tzinfo=UTC)}
    assert not any(
        issue.issue_type == "CACHE_UPDATE_IGNORED_STALE" for issue in second.issues
    )
    first_regime = next(
        item for item in first.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1"
    )
    second_regime = next(
        item for item in second.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1"
    )
    second_regime_update = next(
        item
        for item in second.cache_update_results
        if item.key.name == "fred:rate_curve_regime_v1"
    )
    assert second_regime.context_indicator_id == first_regime.context_indicator_id
    assert second_regime_update.status is ContextStateUpdateStatus.IGNORED_DUPLICATE
    assert len([item for item in writer.snapshots if item.indicator_name == "rate_curve_regime_v1"]) == 1


def test_cache_rejected_raw_is_not_returned_and_suppresses_derivations() -> None:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="fred:us_treasury_3m_yield",
            value=4.5,
            updated_at=datetime(2026, 6, 20, tzinfo=UTC),
            source="fred_rates_v1",
            source_event_time=datetime(2026, 6, 20, tzinfo=UTC),
            valid_until=datetime(2026, 6, 26, tzinfo=UTC),
            details={"preseeded": True},
        )
    )
    writer = FakeWriter()
    result = _collector(cache=cache, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    returned_names = {item.indicator_name for item in result.indicator_snapshots}
    update_by_name = {
        update.key.name: update.status for update in result.cache_update_results
    }
    ledger_names = {item.indicator_name for item in writer.snapshots}

    assert result.status is FREDCollectionStatus.PARTIAL
    assert update_by_name["fred:us_treasury_3m_yield"] is ContextStateUpdateStatus.IGNORED_STALE
    assert "us_treasury_3m_yield" not in returned_names
    assert "us_treasury_3m_yield" not in ledger_names
    assert "us_treasury_3m_yield_change_prev_valid_obs" not in returned_names
    assert "us_treasury_2y_minus_3m" not in returned_names
    assert "us_treasury_10y_minus_3m" not in returned_names
    assert "rate_curve_regime_v1" not in returned_names
    assert "us_treasury_3m_yield_change_prev_valid_obs" not in ledger_names
    assert "fred:us_treasury_3m_yield_change_prev_valid_obs" not in update_by_name
    assert "fred:us_treasury_2y_minus_3m" not in update_by_name
    assert "fred:us_treasury_10y_minus_3m" not in update_by_name
    assert "fred:rate_curve_regime_v1" not in update_by_name
    assert any(
        issue.issue_type == "CACHE_UPDATE_IGNORED_STALE"
        and issue.indicator_name == "us_treasury_3m_yield"
        for issue in result.issues
    )
    suppressed = {
        issue.indicator_name
        for issue in result.issues
        if issue.issue_type == "DERIVATION_SUPPRESSED_STALE_COMPONENT"
    }
    assert suppressed == {
        "us_treasury_3m_yield_change_prev_valid_obs",
        "us_treasury_2y_minus_3m",
        "us_treasury_10y_minus_3m",
        "rate_curve_regime_v1",
    }


def test_unpinned_realtime_changes_do_not_change_identity_or_write() -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    first_client = FakeClient()
    first = _collector(cache=cache, client=first_client, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    changed = {
        series_id: _rows(
            str(rows[0]["value"]),
            str(rows[1]["value"]),
            realtime="different-unpinned-request-period",
        )
        for series_id, rows in _payloads().items()
    }
    second = _collector(cache=cache, client=FakeClient(changed), writer=writer).collect(
        evaluation_time=CHECKED_AT + timedelta(days=1),
        write_questdb=True,
    )

    assert all(item.status is ContextStateUpdateStatus.IGNORED_DUPLICATE for item in second.cache_update_results)
    assert len(writer.snapshots) == 10
    first_regime = next(item for item in first.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")
    second_regime = next(item for item in second.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")
    regime_update = next(
        item
        for item in second.cache_update_results
        if item.key.name == "fred:rate_curve_regime_v1"
    )
    assert second_regime.context_indicator_id == first_regime.context_indicator_id
    assert regime_update.status is ContextStateUpdateStatus.IGNORED_DUPLICATE
    assert len([item for item in writer.snapshots if item.indicator_name == "rate_curve_regime_v1"]) == 1


def test_same_date_value_revision_replaces_only_affected_facts() -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    first = _collector(cache=cache, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    revised = _payloads()
    revised["DGS2"][0]["value"] = "4.05"
    result = _collector(cache=cache, client=FakeClient(revised), writer=writer).collect(
        evaluation_time=CHECKED_AT + timedelta(hours=1),
        write_questdb=True,
    )
    statuses = {
        update.key.name: update.status for update in result.cache_update_results
    }
    original_ids = {
        item.indicator_name: item.context_indicator_id for item in writer.snapshots[:10]
    }
    revised_ids = {
        item.indicator_name: item.context_indicator_id for item in result.indicator_snapshots
    }

    assert statuses["fred:us_treasury_2y_yield"] is ContextStateUpdateStatus.REPLACED
    assert statuses["fred:us_treasury_2y_yield_change_prev_valid_obs"] is ContextStateUpdateStatus.REPLACED
    assert statuses["fred:us_treasury_2y_minus_3m"] is ContextStateUpdateStatus.REPLACED
    assert statuses["fred:us_treasury_10y_minus_2y"] is ContextStateUpdateStatus.REPLACED
    assert statuses["fred:us_treasury_3m_yield"] is ContextStateUpdateStatus.IGNORED_DUPLICATE
    assert statuses["fred:us_treasury_10y_yield"] is ContextStateUpdateStatus.IGNORED_DUPLICATE
    assert statuses["fred:us_treasury_10y_minus_3m"] is ContextStateUpdateStatus.IGNORED_DUPLICATE
    assert statuses["fred:rate_curve_regime_v1"] is ContextStateUpdateStatus.REPLACED
    assert revised_ids["us_treasury_2y_yield"] != original_ids["us_treasury_2y_yield"]
    first_regime = next(item for item in first.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")
    revised_regime = next(item for item in result.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")
    first_component_ids = first_regime.details["component_indicator_ids"]
    revised_component_ids = revised_regime.details["component_indicator_ids"]
    assert revised_regime.value == first_regime.value
    assert revised_regime.context_indicator_id != first_regime.context_indicator_id
    assert revised_component_ids["us_treasury_2y_yield"] != first_component_ids["us_treasury_2y_yield"]
    assert revised_component_ids["us_treasury_3m_yield"] == first_component_ids["us_treasury_3m_yield"]
    assert revised_component_ids["us_treasury_10y_yield"] == first_component_ids["us_treasury_10y_yield"]
    assert len({item.context_indicator_id for item in writer.snapshots if item.indicator_name == "rate_curve_regime_v1"}) == 2
    assert len(writer.snapshots) > 10


def test_all_same_date_revised_yields_change_regime_identity_without_changing_label() -> None:
    first_payloads = {
        "DGS3MO": _rows("3.00", "2.90"),
        "DGS2": _rows("4.00", "3.90"),
        "DGS10": _rows("5.00", "4.90"),
    }
    revised_payloads = {
        "DGS3MO": _rows("3.10", "2.90"),
        "DGS2": _rows("4.10", "3.90"),
        "DGS10": _rows("5.10", "4.90"),
    }
    cache = ContextStateCache()
    writer = FakeWriter()
    first = _collector(cache=cache, client=FakeClient(first_payloads), writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    revised = _collector(cache=cache, client=FakeClient(revised_payloads), writer=writer).collect(
        evaluation_time=CHECKED_AT + timedelta(hours=1),
        write_questdb=True,
    )
    first_regime = next(item for item in first.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")
    revised_regime = next(item for item in revised.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")
    regime_update = next(
        item
        for item in revised.cache_update_results
        if item.key.name == "fred:rate_curve_regime_v1"
    )

    assert first_regime.value == revised_regime.value == "FRONT_POSITIVE__LONG_POSITIVE"
    assert revised_regime.context_indicator_id != first_regime.context_indicator_id
    assert revised_regime.details["component_indicator_ids"] != first_regime.details["component_indicator_ids"]
    assert regime_update.status is ContextStateUpdateStatus.REPLACED
    assert revised_regime.details["component_yields"] == {
        "us_treasury_3m_yield": 3.1,
        "us_treasury_2y_yield": 4.1,
        "us_treasury_10y_yield": 5.1,
    }
    assert revised_regime.details["front_spread"] == pytest.approx(1.0)
    assert revised_regime.details["long_spread"] == pytest.approx(1.0)
    assert revised_regime.details["regime_value"] == revised_regime.value
    assert len([item for item in writer.snapshots if item.indicator_name == "rate_curve_regime_v1"]) == 2


def test_stale_refresh_preserves_existing_numeric_cache_deadline() -> None:
    cache = ContextStateCache()
    collector = _collector(cache=cache)
    collector.collect(evaluation_time=CHECKED_AT)
    name = "fred:us_treasury_3m_yield"
    before = cache.get_global(name, now=CHECKED_AT, include_expired=True)
    stale_payloads = {
        series_id: _rows("3.8", "3.7", latest_date="2026-06-10", prior_date="2026-06-09")
        for series_id in EXPECTED_SERIES_IDS.values()
    }
    stale = _collector(cache=cache, client=FakeClient(stale_payloads)).collect(
        evaluation_time=CHECKED_AT
    )
    after = cache.get_global(name, now=CHECKED_AT, include_expired=True)

    assert stale.status is FREDCollectionStatus.STALE
    assert before is not None and after is not None
    assert after.value == before.value
    assert after.updated_at == before.updated_at
    assert after.valid_until == before.valid_until


def test_newer_observation_replaces_and_older_observation_is_ignored_stale() -> None:
    cache = ContextStateCache()
    collector = _collector(cache=cache)
    collector.collect(evaluation_time=CHECKED_AT)
    newer_payloads = {
        series_id: _rows("4.1", "4.0", latest_date="2026-06-20", prior_date="2026-06-19")
        for series_id in EXPECTED_SERIES_IDS.values()
    }
    newer = _collector(cache=cache, client=FakeClient(newer_payloads)).collect(
        evaluation_time=CHECKED_AT + timedelta(days=1)
    )
    older_payloads = {
        series_id: _rows("3.9", "3.8", latest_date="2026-06-18", prior_date="2026-06-17")
        for series_id in EXPECTED_SERIES_IDS.values()
    }
    older = _collector(cache=cache, client=FakeClient(older_payloads)).collect(
        evaluation_time=CHECKED_AT + timedelta(days=1)
    )

    assert all(item.status is ContextStateUpdateStatus.REPLACED for item in newer.cache_update_results)
    assert all(item.status is ContextStateUpdateStatus.IGNORED_STALE for item in older.cache_update_results)


def test_regime_is_cached_and_written_but_absent_when_misaligned() -> None:
    cache = ContextStateCache()
    writer = FakeWriter()
    success = _collector(cache=cache, writer=writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    regime = next(item for item in success.indicator_snapshots if item.indicator_name == "rate_curve_regime_v1")

    assert cache.get_global("fred:rate_curve_regime_v1", now=CHECKED_AT) is not None
    assert regime in writer.snapshots
    assert not isinstance(regime, ContextFlag)
    assert regime.details["value_kind"] == "categorical_regime"
    assert regime.details["derivation_version"] == "fred_rate_curve_regime_v1"

    payloads = _payloads()
    payloads["DGS10"] = _rows("4.35", "4.25", latest_date="2026-06-18", prior_date="2026-06-17")
    misaligned_writer = FakeWriter()
    misaligned = _collector(client=FakeClient(payloads), writer=misaligned_writer).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    assert "rate_curve_regime_v1" not in _values(misaligned)
    assert all(item.indicator_name != "rate_curve_regime_v1" for item in misaligned_writer.snapshots)

    missing = _payloads()
    missing["DGS10"] = [{"date": LATEST_DATE, "value": "."}]
    missing_result = _collector(client=FakeClient(missing)).collect(evaluation_time=CHECKED_AT)
    assert "rate_curve_regime_v1" not in _values(missing_result)


def test_writer_failure_required_and_optional_behavior_is_sanitized() -> None:
    optional = _collector(writer=FakeWriter(fail=True)).collect(
        evaluation_time=CHECKED_AT,
        write_questdb=True,
    )
    assert optional.status is FREDCollectionStatus.PARTIAL
    assert "hidden-secret-value" not in json.dumps([issue.__dict__ for issue in optional.issues])

    with pytest.raises(FREDCollectorError, match="QuestDB") as exc_info:
        _collector(writer=FakeWriter(fail=True)).collect(
            evaluation_time=CHECKED_AT,
            write_questdb=True,
            questdb_required=True,
        )
    assert "hidden-secret-value" not in str(exc_info.value)

    cache = ContextStateCache()
    with pytest.raises(FREDCollectorError, match="no writer"):
        _collector(cache=cache).collect(
            evaluation_time=CHECKED_AT,
            write_questdb=True,
            questdb_required=True,
        )
    assert cache.snapshot(now=CHECKED_AT)["entry_count"] == 0


class _Response:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"observations": _rows("4.2", "4.1")}


def test_concrete_client_uses_exact_params_and_never_exposes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "fred-test-secret-value"
    monkeypatch.setenv("CUSTOM_FRED_KEY", secret)
    captured: dict[str, object] = {}

    def request_get(url: str, **kwargs: object) -> _Response:
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    rows = FREDClient(
        api_key_env="CUSTOM_FRED_KEY",
        timeout_seconds=7.5,
        request_get=request_get,
    ).fetch_observations(
        "DGS2",
        file_type="json",
        sort_order="desc",
        order_by="observation_date",
        limit=20,
    )
    assert len(rows) == 2
    assert captured["timeout"] == 7.5
    assert captured["params"] == {
        "series_id": "DGS2",
        "api_key": secret,
        "file_type": "json",
        "sort_order": "desc",
        "order_by": "observation_date",
        "limit": 20,
    }

    monkeypatch.delenv("CUSTOM_FRED_KEY")
    with pytest.raises(FREDCollectorError) as exc_info:
        FREDClient(api_key_env="CUSTOM_FRED_KEY", request_get=request_get).fetch_observations(
            "DGS2",
            file_type="json",
            sort_order="desc",
            order_by="observation_date",
            limit=20,
        )
    assert secret not in str(exc_info.value)


def test_naive_evaluation_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _collector().collect(evaluation_time=datetime(2026, 6, 20, 12, 0))


def test_fred_module_has_no_trading_or_scheduler_dependencies() -> None:
    text = (REPO_ROOT / "src" / "market_relay_engine" / "context" / "fred_collector.py").read_text(encoding="utf-8")
    forbidden = (
        "market_relay_engine.risk",
        "market_relay_engine.execution",
        "market_relay_engine.model",
        "databento",
        "alpaca",
        "sleep(",
        "Thread(",
    )
    assert not any(term in text for term in forbidden)
