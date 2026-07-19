from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

import scripts.check_external_event_sources as checker
from market_relay_engine.ai_context import load_ai_context_filter_settings
from market_relay_engine.common.config import load_yaml_config
from market_relay_engine.context.external_event_archive import (
    ExternalEventArchive,
    ExternalSourceRevision,
    LifecycleState,
    source_revision_id,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _archive_pltr_revision(
    archive: ExternalEventArchive,
    *,
    fact_id: str,
    observed_at: datetime,
) -> ExternalSourceRevision:
    text = f"Palantir official release {fact_id}."
    raw_hash = archive.archive_object(
        text.encode("utf-8"),
        extension="html",
        content_type="text/html",
    )
    normalized_hash = archive.archive_normalized_text(text)
    revision = ExternalSourceRevision(
        source="palantir_ir",
        source_fact_id=fact_id,
        source_revision_id=source_revision_id(
            source="palantir_ir",
            source_fact_id=fact_id,
            canonical_content_hash=normalized_hash,
            lifecycle_state=LifecycleState.ACTIVE,
            adapter_version="palantir_ir_adapter_v1",
        ),
        revision_sequence=1,
        supersedes_revision_id=None,
        lifecycle_state=LifecycleState.ACTIVE,
        lifecycle_effective_at=observed_at,
        system_observed_at=observed_at,
        source_available_at=observed_at,
        archived_at=observed_at,
        raw_object_hash=raw_hash,
        document_hash=normalized_hash,
        normalized_text_hash=normalized_hash,
        canonical_content_hash=normalized_hash,
        source_type="official_company_news",
        source_platform="palantir_investor_relations",
        affected_tickers=("PLTR",),
        adapter_version="palantir_ir_adapter_v1",
        extractor_version="palantir_ir_json_v1",
        normalizer_version="article_html_v1",
    )
    archive.publish_revision(revision)
    return revision


def _arguments(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "live": False,
        "source": None,
        "ticker": None,
        "max_items": 1,
        "timeout_seconds": 20.0,
        "poll": False,
        "max_polls": 1,
        "establish_checkpoint": False,
        "backfill": False,
        "start_time": None,
        "end_time": None,
        "classify": False,
        "questdb": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_offline_checker_uses_no_network_or_provider_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline checker attempted an external side effect")

    monkeypatch.setattr("requests.sessions.Session.get", forbidden)
    monkeypatch.setattr(checker, "_run_live", forbidden)
    monkeypatch.setattr(checker, "_build_metadata_writer", forbidden)

    results = checker.run_offline_checks(base_dir=REPO_ROOT)

    assert results
    assert all(result.ok for result in results)
    assert {result.label for result in results} == {
        "configuration",
        "normalization-and-scope",
        "scope-aware-excerpt",
        "archive-and-suppression",
        "connector-fixtures",
        "source-collector-fixtures",
        "classification-projection-shadow",
    }


def test_offline_main_reports_every_disabled_side_effect(capsys: pytest.CaptureFixture[str]) -> None:
    assert checker.main([]) == 0

    output = capsys.readouterr().out
    assert "offline check PASSED" in output
    assert "network=false" in output
    assert "gemini=false" in output
    assert "questdb=false" in output
    assert "alpaca=false" in output
    assert "risk_changes=false" in output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (_arguments(live=True), "--live requires --source"),
        (
            _arguments(source="lmt-rss"),
            "live source/action flags require --live",
        ),
        (
            _arguments(live=True, source="earnings"),
            "--source earnings requires --ticker",
        ),
        (
            _arguments(live=True, source="lmt-rss", ticker="LMT"),
            "--ticker is valid only",
        ),
        (
            _arguments(live=True, source="veritawire", poll=True),
            "--poll is HTTP-only",
        ),
        (
            _arguments(
                live=True,
                source="lmt-rss",
                backfill=True,
                establish_checkpoint=True,
            ),
            "cannot be combined",
        ),
        (
            _arguments(live=True, source="pltr-ir", backfill=True),
            "requires --start-time and --end-time",
        ),
        (
            _arguments(live=True, source="lmt-rss", max_items=0),
            "--max-items",
        ),
        (
            _arguments(live=True, source="lmt-rss", timeout_seconds=121),
            "--timeout-seconds",
        ),
        (
            _arguments(live=True, source="lmt-rss", max_polls=21),
            "--max-polls",
        ),
    ],
)
def test_live_argument_gates_fail_closed(args: Namespace, message: str) -> None:
    assert message in (checker.validate_arguments(args) or "")


def test_backfill_requires_ordered_aware_bounds() -> None:
    parser = checker.build_parser()
    args = parser.parse_args(
        [
            "--live",
            "--source",
            "pltr-ir",
            "--backfill",
            "--start-time",
            "2026-07-01T00:00:00Z",
            "--end-time",
            "2026-07-18T00:00:00+00:00",
            "--max-items",
            "3",
        ]
    )

    assert checker.validate_arguments(args) is None
    assert args.start_time.tzinfo is not None
    assert args.end_time.tzinfo is not None
    assert args.max_items == 3


def test_timestamp_without_offset_is_rejected() -> None:
    parser = checker.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--live",
                "--source",
                "pltr-ir",
                "--backfill",
                "--start-time",
                "2026-07-01T00:00:00",
                "--end-time",
                "2026-07-18T00:00:00Z",
            ]
        )


def test_safe_live_result_filters_secrets_bodies_and_urls() -> None:
    for name in (
        "api_key",
        "authorization",
        "source_body",
        "payload",
        "prompt",
        "source_url",
        "traceback",
    ):
        assert checker._safe_live_result_value(name, "sensitive") is False
    assert checker._safe_live_result_value("new_count", 2) is True


def test_poller_coverage_source_is_ticker_partitioned_for_earnings() -> None:
    assert checker._poll_coverage_source("lmt-rss", None) == "lockheed_martin_rss"
    assert checker._poll_coverage_source("pltr-ir", None) == "palantir_ir"
    assert (
        checker._poll_coverage_source("earnings", "PLTR")
        == "company_earnings:PLTR"
    )
    with pytest.raises(RuntimeError, match="reviewed ticker"):
        checker._poll_coverage_source("earnings", None)


def test_empty_pending_classification_does_not_load_key_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = checker._classify_pending_revisions(
        archive=ExternalEventArchive(tmp_path),
        source="palantir_ir",
        max_items=1,
        write_questdb=True,
    )

    assert result == {
        "classification_candidates": 0,
        "classification_completed": 0,
        "classification_pending": 0,
        "provider_calls": 0,
        "canonical_reuses": 0,
        "questdb_records": 0,
    }


def test_pending_selection_filters_newest_exact_profile_completion_before_limit(
    tmp_path: Path,
) -> None:
    archive = ExternalEventArchive(tmp_path)
    observed_at = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    older = _archive_pltr_revision(
        archive,
        fact_id="older-pending",
        observed_at=observed_at,
    )
    newer = _archive_pltr_revision(
        archive,
        fact_id="newer-complete",
        observed_at=observed_at + timedelta(seconds=1),
    )
    ai_settings = replace(
        load_ai_context_filter_settings(
            base_dir=REPO_ROOT,
            enabled_override=True,
        ),
        prompt_version="context_filter_v2_scope",
        response_schema_version="context_classification_response_v2",
    )
    hints = checker._ticker_sector_hints(
        load_yaml_config("symbols", base_dir=REPO_ROOT)
    )
    initial = checker._pending_classification_candidates(
        archive=archive,
        revisions=checker._current_classifiable_revisions(
            archive=archive,
            source="palantir_ir",
        ),
        max_items=2,
        ai_settings=ai_settings,
        ticker_sector_hints=hints,
    )
    completed = next(value for value in initial if value.revision == newer)
    profile = completed.profile
    output_hash = sha256(b"completed-output").hexdigest()
    archive.publish_readiness(
        source_revision_id=newer.source_revision_id,
        classification_input_fingerprint=(
            completed.classification_input_fingerprint
        ),
        canonical_classification_attempt_id="canonical-newer",
        complete_output_fingerprint=output_hash,
        policy_output_fingerprint=output_hash,
        profile_hash=profile.profile_hash,
        classification_profile=profile.to_fingerprint_payload(),
        classification_status="VALID",
        policy_eligible=False,
        context_event=None,
    )

    selected = checker._pending_classification_candidates(
        archive=archive,
        revisions=checker._current_classifiable_revisions(
            archive=archive,
            source="palantir_ir",
        ),
        max_items=1,
        ai_settings=ai_settings,
        ticker_sector_hints=hints,
    )

    assert [value.revision for value in selected] == [older]


def test_main_never_dispatches_live_without_explicit_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checker,
        "_run_live",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected live dispatch")
        ),
    )

    assert checker.main([]) == 0
