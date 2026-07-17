"""Explicit, bounded research-only SEC EDGAR collector/checker.

Without ``--live`` this validates configuration and performs no network I/O.
``--live`` reads public SEC endpoints only; it never calls a broker.  Gemini is
also opt-in through ``--classify`` and QuestDB is opt-in through ``--questdb``.
"""

from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import sys

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
for path in (REPO_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from market_relay_engine.ai_context import GeminiContextClassifier, load_ai_context_filter_settings  # noqa: E402
from market_relay_engine.common.config import load_yaml_config  # noqa: E402
from market_relay_engine.context.sec_edgar import (  # noqa: E402
    SECEDGARCollector,
    SECEDGARHTTPClient,
    SECEDGARSettings,
    load_sec_issuers,
)
from market_relay_engine.context.sec_edgar_archive import SECEDGARArchive  # noqa: E402
from market_relay_engine.questdb.jsonl_fallback import EmergencyJSONLLedgerFallback  # noqa: E402
from market_relay_engine.questdb.writer import QuestDBLedgerWriter  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use public SEC endpoints; omitted means offline validation only.")
    parser.add_argument("--ticker", action="append", choices=("PLTR", "LMT", "RTX", "GD", "AVAV", "XOM", "OXY", "SLB", "COP", "VLO"), help="Approved ticker; repeat to select several.")
    parser.add_argument("--form", action="append", choices=("8-K", "8-K/A", "4", "4/A"), help="SEC form; repeat to select several.")
    parser.add_argument("--start-date", type=_date_argument, help="Inclusive filing date, YYYY-MM-DD.")
    parser.add_argument("--end-date", type=_date_argument, help="Inclusive filing date, YYYY-MM-DD.")
    parser.add_argument(
        "--max-filings",
        type=int,
        default=1,
        help=(
            "Maximum actionable filings per live run; dry-run limits raw "
            "discoveries (default: 1)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Discover only: no archive, Gemini, or QuestDB writes; "
            "--max-filings limits raw discoveries."
        ),
    )
    parser.add_argument("--classify", action="store_true", help="Explicitly enable existing Gemini classification for eligible bounded 8-K sections.")
    parser.add_argument("--questdb", action="store_true", help="Explicitly write classification-attempt metadata to QuestDB.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_filings <= 0:
        print("SEC EDGAR check FAIL: --max-filings must be positive")
        return 2
    if args.end_date is not None and args.start_date is not None and args.end_date < args.start_date:
        print("SEC EDGAR check FAIL: --end-date precedes --start-date")
        return 2
    if args.dry_run and (args.classify or args.questdb):
        print("SEC EDGAR check FAIL: --dry-run cannot be combined with --classify or --questdb")
        return 2
    try:
        context_config = load_yaml_config("context_sources", base_dir=REPO_ROOT)
        settings = SECEDGARSettings.from_repository_config(context_config, base_dir=REPO_ROOT)
        issuers = load_sec_issuers(base_dir=REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - script intentionally avoids verbose local config output.
        print(f"SEC EDGAR offline check FAIL: {type(exc).__name__}")
        return 1
    if not args.live:
        print("SEC EDGAR offline check PASS (no network request made)")
        print(f"approved_issuers={len(issuers)}")
        print(f"request_rate_per_second={settings.request_rate_per_second:g}")
        return 0

    load_dotenv(REPO_ROOT / ".env", override=False)
    try:
        user_agent = settings.user_agent(os.environ)
    except Exception as exc:  # noqa: BLE001 - redacted boundary.
        print(f"SEC EDGAR live check FAIL: {type(exc).__name__}")
        return 1
    classifier = None
    ai_settings = None
    if args.classify:
        try:
            ai_settings = load_ai_context_filter_settings(base_dir=REPO_ROOT, enabled_override=True)
            api_key = os.getenv(ai_settings.api_key_env)
            if not api_key:
                raise RuntimeError("Gemini key unavailable")
            classifier = GeminiContextClassifier(ai_settings, api_key=api_key)
        except Exception as exc:  # noqa: BLE001 - do not expose credentials or provider internals.
            print(f"SEC EDGAR live check FAIL: Gemini {type(exc).__name__}")
            return 1
    writer = QuestDBLedgerWriter() if args.questdb else None
    collector = SECEDGARCollector(
        settings=settings,
        issuers=issuers,
        client=SECEDGARHTTPClient(
            user_agent=user_agent,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            request_rate_per_second=settings.request_rate_per_second,
            retry_base_delay_seconds=settings.retry_base_delay_seconds,
            retry_max_delay_seconds=settings.retry_max_delay_seconds,
        ),
        archive=SECEDGARArchive(settings.archive_path),
        classifier=classifier,
        ai_settings=ai_settings,
        ledger_writer=writer,
        fallback=EmergencyJSONLLedgerFallback() if args.questdb else None,
    )
    try:
        result = collector.collect(tickers=args.ticker, forms=args.form or ("8-K", "8-K/A", "4", "4/A"), start_date=args.start_date, end_date=args.end_date, max_filings=args.max_filings, dry_run=args.dry_run, write_questdb=args.questdb)
    except Exception as exc:  # noqa: BLE001 - safe operational checker boundary.
        print(f"SEC EDGAR live check FAIL: {type(exc).__name__}")
        return 1
    finally:
        if classifier is not None:
            classifier.close()
    if result["mapping_drift"]:
        print("SEC EDGAR live check FAIL: SECMappingDriftError")
        print(f"mapping_drift={result['mapping_drift']}")
        return 1
    print("SEC EDGAR live check PASS")
    print("mode=research_only")
    print(f"discovered={result['discovered']}")
    print(f"actionable_filings={result['actionable_filings']}")
    print(f"archived={result['archived']}")
    print(f"classifications={result['classifications']}")
    print(f"persistent_suppressions={result['persistent_suppressions']}")
    print(f"ledger_retries={result['ledger_retries']}")
    print(f"form4_events={result['form4_events']}")
    print(f"mapping_drift={result['mapping_drift']}")
    return 0


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


if __name__ == "__main__":
    raise SystemExit(main())
