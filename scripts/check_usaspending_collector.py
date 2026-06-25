"""Deterministic offline validation for the PR28 USAspending collector."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.context.state_cache import ContextStateCache  # noqa: E402
from market_relay_engine.context.usaspending_collector import (  # noqa: E402
    USAspendingCollectionStatus,
    USAspendingCollector,
    USAspendingConfig,
    USAspendingRecipientMapping,
    JSONUSAspendingCheckpointStore,
    cache_entry_name,
)


CHECKED_AT = datetime(2026, 6, 20, 16, 0, tzinfo=UTC)
UEI = "EXACTUEI123"


def _mapping() -> USAspendingRecipientMapping:
    return USAspendingRecipientMapping(
        recipient_uei=UEI,
        recipient_name="EXACT LEGAL NAME",
        ticker="TST",
        issuer_name="Test Issuer Inc.",
        mapping_confidence="confirmed",
        economic_beneficiary="prime_recipient",
        active=True,
        mapping_version="usaspending_recipient_map_v1",
    )


def _search_row(lookup: str) -> dict[str, object]:
    return {
        "generated_internal_id": lookup,
        "Award ID": f"AWARD-{lookup}",
        "Recipient UEI": UEI,
        "Recipient Name": "EXACT LEGAL NAME",
        "Last Modified Date": "2026-06-20",
        "Base Obligation Date": "2026-06-20",
        "Award Amount": 100.0,
        "Contract Award Type": "A",
        "NAICS": "541330",
        "PSC": "R425",
    }


def _detail(canonical_id: str, *, amount: float = 100.0) -> dict[str, object]:
    return {
        "generated_unique_award_id": canonical_id,
        "recipient": {"recipient_uei": UEI, "recipient_name": "EXACT LEGAL NAME"},
        "type": "A",
        "type_description": "Definitive Contract",
        "category": "contract",
        "description": "Official factual award description.",
        "date_signed": "2026-06-20",
        "action_date": "2026-06-20",
        "last_modified_date": "2026-06-20",
        "total_obligation": amount,
        "base_exercised_options": amount,
        "base_and_all_options": amount + 50.0,
        "awarding_agency": {"name": "Agency A", "code": "AA"},
        "funding_agency": {"name": "Agency F", "code": "FF"},
        "latest_transaction_contract_data": {
            "naics": "541330",
            "naics_description": "Engineering Services",
            "product_or_service_code": "R425",
            "product_or_service_description": "Engineering services",
        },
    }


def _funding(*, has_next: bool = False) -> dict[str, object]:
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
        "page_metadata": {"hasNext": has_next},
    }


class OfflineClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.search_payload = {
            "results": [_search_row("lookup-1")],
            "page_metadata": {"hasNext": False},
        }
        self.details = {
            "lookup-1": _detail("CONT_AWD_1"),
            "CONT_AWD_1": _detail("CONT_AWD_1"),
        }
        self.funding = {"CONT_AWD_1": _funding()}

    def fetch_last_updated(self) -> dict[str, object]:
        self.calls.append(("last_updated", None))
        return {"last_updated": "2026-06-20"}

    def search_spending_by_award(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("search", dict(kwargs)))
        return deepcopy(self.search_payload)

    def fetch_award_detail(self, award_id: str) -> dict[str, object]:
        self.calls.append(("detail", award_id))
        return deepcopy(self.details[award_id])

    def fetch_award_funding(self, award_id: str, *, limit: int) -> dict[str, object]:
        self.calls.append(("funding", {"award_id": award_id, "limit": limit}))
        return deepcopy(self.funding[award_id])


class OfflineWriter:
    def __init__(self) -> None:
        self.snapshots: list[object] = []

    def write_context_indicator_snapshot(self, snapshot: object, **kwargs: object) -> str:
        self.snapshots.append(snapshot)
        return "written"


def run_checks() -> None:
    disabled_client = OfflineClient()
    disabled = USAspendingCollector(
        cache=ContextStateCache(),
        config=USAspendingConfig(),
        client=disabled_client,
        recipient_mappings=(_mapping(),),
    ).collect(evaluation_time=CHECKED_AT)
    assert disabled.status is USAspendingCollectionStatus.DISABLED
    assert disabled_client.calls == []

    with tempfile.TemporaryDirectory() as temp_dir:
        checkpoint = Path(temp_dir) / "award_checkpoint.json"
        client = OfflineClient()
        cache = ContextStateCache()
        writer = OfflineWriter()
        collector = USAspendingCollector(
            cache=cache,
            config=USAspendingConfig(enabled=True),
            client=client,
            ledger_writer=writer,
            checkpoint_store=JSONUSAspendingCheckpointStore(checkpoint),
            recipient_mappings=(_mapping(),),
        )
        first = collector.collect(evaluation_time=CHECKED_AT, write_questdb=True)
        assert first.status is USAspendingCollectionStatus.SUCCESS
        assert len(first.indicator_snapshots) == len(writer.snapshots) == 1
        assert cache.get_ticker("TST", cache_entry_name("TST", "CONT_AWD_1"), now=CHECKED_AT)

        fresh_cache = ContextStateCache()
        second = USAspendingCollector(
            cache=fresh_cache,
            config=USAspendingConfig(enabled=True),
            client=OfflineClient(),
            ledger_writer=writer,
            checkpoint_store=JSONUSAspendingCheckpointStore(checkpoint),
            recipient_mappings=(_mapping(),),
        ).collect(evaluation_time=CHECKED_AT, write_questdb=True)
        assert second.status is USAspendingCollectionStatus.SUCCESS
        assert second.indicator_snapshots == ()
        assert len(writer.snapshots) == 1
        assert fresh_cache.get_ticker(
            "TST",
            cache_entry_name("TST", "CONT_AWD_1"),
            now=CHECKED_AT,
        )

        truncated_client = OfflineClient()
        truncated_client.funding["CONT_AWD_1"] = _funding(has_next=True)
        partial = USAspendingCollector(
            cache=ContextStateCache(),
            config=USAspendingConfig(enabled=True),
            client=truncated_client,
            checkpoint_store=JSONUSAspendingCheckpointStore(Path(temp_dir) / "partial.json"),
            recipient_mappings=(_mapping(),),
        ).collect(evaluation_time=CHECKED_AT)
        assert partial.status is USAspendingCollectionStatus.PARTIAL
        assert partial.indicator_snapshots[0].details["funding_page_complete"] is False


def main() -> int:
    try:
        run_checks()
    except Exception as exc:  # noqa: BLE001 - CLI validation boundary.
        print(f"USAspending collector validation FAILED: {exc}")
        return 1
    print("USAspending collector validation PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
