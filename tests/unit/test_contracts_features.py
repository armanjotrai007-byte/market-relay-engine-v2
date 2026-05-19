from market_relay_engine.contracts.features import FeatureSnapshot
from tests.unit.contract_test_utils import assert_contract_serializes, example_for


def test_feature_snapshot_serializes_with_stable_id() -> None:
    parsed = assert_contract_serializes(example_for(FeatureSnapshot))

    assert parsed["feature_snapshot_id"]
    assert parsed["feature_version"] == "feature_v0_placeholder"
    assert parsed["features"]["midprice"] == 100.25
    assert parsed["snapshot_time"].endswith("Z")
