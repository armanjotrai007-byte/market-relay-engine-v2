"""Inspect official EIA WPSR API metadata and representative records safely."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.eia_wpsr import (  # noqa: E402
    API_ROOT,
    EIAWPSRClient,
    METRICS,
    STOCK_ROUTE,
    UTILIZATION_ROUTE,
)


def _safe_get(path: str, params: list[tuple[str, str]], *, timeout: float = 10.0) -> dict[str, Any]:
    for attempt in range(2):
        try:
            response = requests.get(f"{API_ROOT}/{path}/", params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == 0:
                continue
            raise RuntimeError(f"official EIA metadata request failed for {path}") from exc
        if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
            continue
        if response.status_code != 200:
            raise RuntimeError(f"official EIA metadata route {path} returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"official EIA metadata route {path} returned non-object JSON")
        return payload
    raise RuntimeError(f"official EIA metadata request failed for {path}")


def inspect_live() -> dict[str, object]:
    key = os.getenv("EIA_API_KEY")
    if not key:
        raise RuntimeError("EIA_API_KEY missing")
    client = EIAWPSRClient()
    result: dict[str, object] = {"source": "official EIA API v2", "routes": []}
    routes: list[dict[str, object]] = []
    for route in (STOCK_ROUTE, UTILIZATION_ROUTE):
        metadata = _safe_get(route, [("api_key", key)])
        body = metadata.get("response", {})
        if not isinstance(body, dict):
            raise RuntimeError(f"invalid metadata response for {route}")
        specs = [spec for spec in METRICS if spec.route == route]
        records = client.fetch_weekly_records(route, [spec.series_id for spec in specs], observations_per_series=2)
        routes.append(
            {
                "route": f"/v2/{route}/data/",
                "frequency": body.get("frequency"),
                "facets": body.get("facets"),
                "data_fields": sorted(records[0]) if records else [],
                "top_level_response_keys": sorted(body),
                "verified_series": [
                    {
                        "series_id": spec.series_id,
                        "indicator_name": spec.indicator_name,
                        "expected_units": spec.units,
                        "facets": dict(spec.facets),
                        "representative_records": [
                            {key: record.get(key) for key in ("period", "series", "value", "units", "duoarea", "product", "process")}
                            for record in records
                            if record.get("series") == spec.series_id
                        ][:2],
                    }
                    for spec in specs
                ],
                "publication_timestamp_present": any(
                    any(token in field.lower() for token in ("publish", "release", "update"))
                    for record in records
                    for field in record
                ),
                "revision_version_present": any(
                    any(token in field.lower() for token in ("revision", "version"))
                    for record in records
                    for field in record
                ),
            }
        )
    result["routes"] = routes
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the official EIA WPSR API safely")
    parser.add_argument("--live", action="store_true", help="perform read-only official EIA requests")
    parser.add_argument("--output", type=Path, help="optional sanitized JSON output path")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required")
    try:
        result = inspect_live()
        rendered = json.dumps(result, indent=2, sort_keys=True)
        if args.output is not None:
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(f"sanitized_output_written: {args.output}")
        else:
            print(rendered)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary; messages are sanitized above.
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
