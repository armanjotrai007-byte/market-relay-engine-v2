"""Build reviewed WPSR release candidates from the official EIA schedule page."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import requests
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.eia_wpsr import SCHEDULE_URL  # noqa: E402


EASTERN = ZoneInfo("America/New_York")


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def parse_schedule_candidates(html: str, *, start_date: date, end_date: date) -> list[dict[str, str]]:
    parser = _TableParser()
    parser.feed(html)
    table = next((item for item in parser.tables if item and "Alternate release date" in item[0]), None)
    if table is None:
        raise RuntimeError("official EIA holiday schedule table was not found")
    exceptions: dict[date, datetime] = {}
    represented_years: set[int] = set()
    for row in table[1:]:
        if len(row) < 4:
            continue
        report_period = datetime.strptime(row[0], "%B %d, %Y").date()
        release_date = datetime.strptime(row[1], "%B %d, %Y").date()
        release_time = datetime.strptime(row[3].replace("a.m.", "AM").replace("p.m.", "PM"), "%I:%M %p").time()
        exceptions[report_period] = datetime.combine(release_date, release_time, EASTERN)
        represented_years.add(release_date.year)
    requested_years = set(range(start_date.year, end_date.year + 1))
    if not requested_years.issubset(represented_years):
        raise RuntimeError("official schedule does not enumerate holiday exceptions for the requested year")

    first_friday = start_date - timedelta(days=14)
    first_friday += timedelta(days=(4 - first_friday.weekday()) % 7)
    candidates: list[dict[str, str]] = []
    report_period = first_friday
    while report_period <= end_date:
        release_at = exceptions.get(
            report_period,
            datetime.combine(report_period + timedelta(days=5), time(10, 30), EASTERN),
        )
        if start_date <= release_at.date() <= end_date:
            candidates.append(
                {
                    "release_id": f"eia_wpsr_{release_at:%Y_%m_%d}",
                    "release_at": release_at.isoformat(),
                    "report_period": report_period.isoformat(),
                }
            )
        report_period += timedelta(days=7)
    if not candidates:
        raise RuntimeError("official schedule produced no release candidates")
    return candidates


def refresh_live(*, start_date: date, end_date: date) -> dict[str, object]:
    response = requests.get(SCHEDULE_URL, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"official EIA schedule returned HTTP {response.status_code}")
    return {
        "metadata": {
            "source": SCHEDULE_URL,
            "generated_at": datetime.now(EASTERN).isoformat(),
            "runtime_authority": False,
            "review_required": True,
        },
        "releases": parse_schedule_candidates(response.text, start_date=start_date, end_date=end_date),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh official EIA WPSR schedule candidates")
    parser.add_argument("--live", action="store_true", help="read the official EIA schedule")
    parser.add_argument("--output", type=Path, help="optional candidate YAML output path")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required")
    today = datetime.now(EASTERN).date()
    start_date = args.start_date or today
    end_date = args.end_date or date(today.year, 12, 31)
    if end_date < start_date:
        parser.error("--end-date must not precede --start-date")
    try:
        result = refresh_live(start_date=start_date, end_date=end_date)
        rendered = yaml.safe_dump(result, sort_keys=False)
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
            print(f"candidate_output_written: {args.output}")
        else:
            print(rendered, end="")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary.
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
