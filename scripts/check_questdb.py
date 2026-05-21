"""Validate optional or required QuestDB HTTP health."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.questdb.health import (  # noqa: E402
    QuestDBHealthError,
    check_questdb_http,
    format_questdb_health_result,
    load_questdb_health_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check local QuestDB HTTP /exec health for the bot ledger."
    )
    parser.add_argument(
        "--required",
        action="store_true",
        default=None,
        help="Fail if QuestDB is not reachable and healthy.",
    )
    parser.add_argument("--host", help="QuestDB HTTP host override.")
    parser.add_argument("--port", help="QuestDB HTTP port override.")
    parser.add_argument("--scheme", help="QuestDB HTTP scheme override.")
    parser.add_argument("--timeout", help="QuestDB health timeout seconds override.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_questdb_health_config(
            http_scheme=args.scheme,
            http_host=args.host,
            http_port=args.port,
            timeout_seconds=args.timeout,
            required=args.required,
        )
        result = check_questdb_http(config)
    except QuestDBHealthError as exc:
        if exc.result is not None:
            print(format_questdb_health_result(exc.result))
        else:
            print(f"[FAIL] QuestDB health check failed: {exc}")
        return 1

    print(format_questdb_health_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
