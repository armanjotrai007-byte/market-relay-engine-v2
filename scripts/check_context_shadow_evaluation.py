"""Offline PR37 research projection and shadow-evaluation check."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.decision_context import DecisionContextAssembler  # noqa: E402
from market_relay_engine.context.research_projection import (  # noqa: E402
    EvidenceCategory,
    ResearchClassificationProfile,
    ResearchRunDefinition,
    hydrate_sec_research_evidence,
)
from market_relay_engine.context.sec_edgar_archive import SECEDGARArchive  # noqa: E402
from market_relay_engine.context.shadow_evaluation import evaluate_shadow_context  # noqa: E402
from market_relay_engine.context.state_cache import (  # noqa: E402
    ContextStateCache,
    make_global_context_entry,
)
from market_relay_engine.contracts.context import ShadowContextAction  # noqa: E402
from market_relay_engine.contracts.model import ModelSignal, SignalSide  # noqa: E402
from market_relay_engine.questdb.writer import (  # noqa: E402
    QuestDBLedgerWriter,
    shadow_context_policy_evaluation_to_row,
)


CHECK_TIME = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
PROFILE_HASH = "a" * 64
DOCUMENT_HASH = "b" * 64
SECTION_HASH = "c" * 64
EXCERPT_HASH = "d" * 64
RAW_HASH = "e" * 64


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--questdb",
        action="store_true",
        help="Explicitly write the fixture shadow result through the existing writer.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with TemporaryDirectory(prefix="market-relay-pr37-") as directory:
            archive = SECEDGARArchive(Path(directory) / "sec")
            _write_fixture_archive(archive)
            definition = _run_definition()
            index = hydrate_sec_research_evidence(
                archive=archive,
                run_definition=definition,
            )
            context = _decision_context()
            selection = index.select(context)
            signal = _model_signal()
            evaluation = evaluate_shadow_context(
                model_signal=signal,
                decision_context=context,
                evidence_selection=selection,
            )
            row = shadow_context_policy_evaluation_to_row(
                evaluation,
                write_time=CHECK_TIME,
            )
            _validate_fixture_result(index, selection, evaluation, row)
            if args.questdb:
                result = QuestDBLedgerWriter().write_shadow_context_policy_evaluation(
                    evaluation
                )
                if not result.success:
                    raise RuntimeError("QuestDB shadow write was not accepted")
    except Exception as exc:  # noqa: BLE001 - checker reports a safe local boundary.
        print(f"PR37 context shadow check FAIL: {type(exc).__name__}")
        return 1

    print("PR37 context shadow check PASS")
    print("mode=research_only")
    print("structured_context_entries=1")
    print("selected_event_evidence=2")
    print("hypothetical_action=NO_CHANGE")
    print(f"questdb_write={'enabled' if args.questdb else 'disabled'}")
    return 0


def _run_definition() -> ResearchRunDefinition:
    return ResearchRunDefinition(
        ticker_universe=("XOM",),
        event_sources=("sec_edgar",),
        evidence_categories=(
            EvidenceCategory.AI_EVENT,
            EvidenceCategory.DETERMINISTIC_EVENT,
        ),
        hydration_start_time=CHECK_TIME - timedelta(days=1),
        hydration_end_time=CHECK_TIME + timedelta(days=1),
        capacity=10,
        classification_profile=ResearchClassificationProfile(
            extraction_version="sec_8k_items_v1",
            prompt_version="context_filter_v1",
            model_version="gemini-fixture",
            response_schema_version="context_classification_response_v1",
            classification_config_hash=PROFILE_HASH,
        ),
        max_age_without_valid_until=timedelta(hours=1),
        selection_policy_version="research_selection_fixture_v1",
    )


def _decision_context() -> object:
    cache = ContextStateCache()
    cache.update(
        make_global_context_entry(
            name="fixture_macro",
            value="VISIBLE",
            source="macro_calendar_v1",
            updated_at=CHECK_TIME - timedelta(minutes=1),
        )
    )
    return DecisionContextAssembler(cache=cache).build_for_decision(
        "XOM",
        CHECK_TIME,
        "trace_pr37_check",
        None,
        ticker_sector="ENERGY",
    )


def _model_signal() -> ModelSignal:
    return ModelSignal(
        signal_time=CHECK_TIME,
        ticker="XOM",
        signal=SignalSide.BUY,
        confidence=0.7,
        raw_score=0.2,
        model_version="fixture_model_v1",
        calibration_version="fixture_calibration_v1",
        feature_version="features_v1",
        feature_snapshot_id="feature_snapshot_pr37_check",
        signal_id="signal_pr37_check",
        trace_id="trace_pr37_check",
    )


def _write_fixture_archive(archive: SECEDGARArchive) -> None:
    row = {
        "classification_attempt_id": "classification_attempt_pr37_check",
        "classification_request_id": "classification_request_pr37_check",
        "raw_input_id": "raw_input_pr37_check",
        "source_document_id": "source_document_pr37_check",
        "source": "sec_edgar",
        "source_type": "sec_8k_item",
        "source_platform": "sec_edgar",
        "source_uri": "https://www.sec.gov/Archives/pr37-check",
        "source_locator": "0000000000-26-000001:2.02",
        "affected_tickers_json": '["XOM"]',
        "raw_input_hash": RAW_HASH,
        "document_hash": DOCUMENT_HASH,
        "source_published_at": CHECK_TIME.isoformat(),
        "source_updated_at": None,
        "collected_at": (CHECK_TIME + timedelta(minutes=1)).isoformat(),
        "normalized_at": (CHECK_TIME + timedelta(minutes=1)).isoformat(),
        "classified_at": (
            CHECK_TIME + timedelta(minutes=2)
        ).isoformat().replace("+00:00", "Z"),
        "provider": "gemini",
        "model_version": "gemini-fixture",
        "prompt_version": "context_filter_v1",
        "status": "VALID",
        "event_type": "SEC_8K_RESULTS",
        "risk_level": "MEDIUM",
        "urgency": "MEDIUM",
        "confidence": 0.7,
        "summary": "Safe fixture classification.",
        "validation_result_id": "validation_pr37_check",
        "validation_outcome": True,
        "validated_at": (CHECK_TIME + timedelta(minutes=2)).isoformat(),
    }
    saved = {
        "classification_complete": True,
        "classification_request_id": "classification_request_pr37_check",
        "classification_attempt_id": "classification_attempt_pr37_check",
        "status": "VALID",
        "event_type": "SEC_8K_RESULTS",
        "risk_level": "MEDIUM",
        "urgency": "MEDIUM",
        "confidence": 0.7,
        "summary": "Safe fixture classification.",
        "classified_at": (CHECK_TIME + timedelta(minutes=2)).isoformat(),
        "provider": "gemini",
        "model_version": "gemini-fixture",
        "prompt_version": "context_filter_v1",
        "response_schema_version": "context_classification_response_v1",
        "classification_config_hash": PROFILE_HASH,
        "accession_number": "0000000000-26-000001",
        "document_hash": DOCUMENT_HASH,
        "full_section_hash": SECTION_HASH,
        "excerpt_hash": EXCERPT_HASH,
        "extraction_version": "sec_8k_items_v1",
        "item_number": "2.02",
        "ledger_row": row,
    }
    archive.save_manifest(
        {
            "schema_version": 2,
            "filings": {
                "0000000000-26-000001": {
                    "form_type": "8-K",
                    "primary_document": "fixture-8k.htm",
                    "official_document_identity": "fixture-8k.htm",
                    "official_document_url": "https://www.sec.gov/Archives/fixture-8k.htm",
                    "document_hash": DOCUMENT_HASH,
                    "document_extension": ".htm",
                    "collected_at": (CHECK_TIME + timedelta(minutes=1)).isoformat(),
                    "classifications": {"fixture": saved},
                }
            },
        }
    )
    archive.write_filing_once(
        "0000000000-26-000001",
        {
            "ticker": "XOM",
            "issuer_cik": "0000000000",
            "accession_number": "0000000000-26-000001",
            "form_type": "8-K",
            "filing_date": CHECK_TIME.date().isoformat(),
            "acceptance_at": CHECK_TIME.isoformat(),
            "primary_document": "fixture-8k.htm",
            "filing_url": "https://www.sec.gov/Archives/fixture-8k.htm",
            "official_document_identity": "fixture-8k.htm",
            "official_document_url": "https://www.sec.gov/Archives/fixture-8k.htm",
            "amendment_of": None,
            "collected_at": (CHECK_TIME + timedelta(minutes=1)).isoformat(),
            "document_hash": DOCUMENT_HASH,
        },
    )
    archive.write_form4_once(
        "0000000000-26-000002",
        {
            "filing": {
                "ticker": "XOM",
                "issuer_cik": "0000000000",
                "accession_number": "0000000000-26-000002",
                "form_type": "4",
                "filing_date": CHECK_TIME.date().isoformat(),
                "acceptance_at": CHECK_TIME.isoformat(),
                "primary_document": "form4.xml",
                "filing_url": "https://www.sec.gov/Archives/form4.xml",
                "official_document_identity": "form4.xml",
                "official_document_url": "https://www.sec.gov/Archives/form4.xml",
                "amendment_of": None,
                "collected_at": (CHECK_TIME + timedelta(minutes=1)).isoformat(),
                "document_hash": DOCUMENT_HASH,
            },
            "issuer_ticker": "XOM",
            "issuer_cik": "0000000000",
            "filing_plan_10b5_1": False,
            "reporting_owners": [],
            "is_amendment": False,
            "amends_accession": None,
            "normalized_transactions": [],
            "research_events": [
                {
                    "event_type": "SEC_FORM4_PURCHASE",
                    "issuer_ticker": "XOM",
                    "issuer_cik": "0000000000",
                    "accession_number": "0000000000-26-000002",
                    "reporting_owners": [],
                    "transaction_date": CHECK_TIME.date().isoformat(),
                    "available_at": CHECK_TIME.isoformat(),
                    "transaction_code": "P",
                    "shares": 10.0,
                    "price_per_share": 100.0,
                    "approximate_value": 1000.0,
                    "direct_or_indirect": "D",
                    "shares_owned_following": 110.0,
                    "is_amendment": False,
                    "amends_accession": None,
                    "aggregate_eligibility": "ELIGIBLE",
                    "plan_10b5_1": False,
                }
            ],
        },
    )


def _validate_fixture_result(
    index: object,
    selection: object,
    evaluation: object,
    row: dict[str, object],
) -> None:
    if index.attempted_record_count != 2:
        raise AssertionError("fixture hydration count changed")
    if len(selection.selected_evidence) != 2:
        raise AssertionError("fixture evidence selection changed")
    if evaluation.hypothetical_action is not ShadowContextAction.NO_CHANGE:
        raise AssertionError("default shadow policy must return NO_CHANGE")
    if evaluation.matched_context_event_ids or evaluation.matched_context_flag_ids:
        raise AssertionError("default policy must not claim matched action evidence")
    if row["shadow_evaluation_id"] != evaluation.shadow_evaluation_id:
        raise AssertionError("existing QuestDB row conversion was not used")


if __name__ == "__main__":
    raise SystemExit(main())
