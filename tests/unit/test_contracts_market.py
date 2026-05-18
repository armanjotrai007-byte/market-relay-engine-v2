from datetime import datetime

import pytest

from market_relay_engine.contracts.market import MarketRecord
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_market_record_serializes() -> None:
    parsed = assert_contract_serializes(example_for(MarketRecord))

    assert parsed["ticker"] == "XOM"
    assert parsed["event_time"].endswith("Z")
    assert parsed["source_event_time"].endswith("Z")
    assert parsed["local_receive_time"].endswith("Z")


def test_market_record_rejects_naive_event_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MarketRecord(
            event_time=datetime(2026, 5, 18, 14, 30),
            ticker="XOM",
            source="example",
            record_type="trade",
        )
