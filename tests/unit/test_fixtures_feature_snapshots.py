from market_relay_engine.common.serialization import from_json_string, to_json_dict, to_json_string
from market_relay_engine.contracts.features import FeatureSnapshot
from tests.fixtures.feature_snapshots import (
    build_feature_snapshot_examples,
    make_feature_snapshot,
)


def test_feature_snapshot_fixtures_instantiate_contracts() -> None:
    examples = build_feature_snapshot_examples()

    assert examples
    assert all(isinstance(example, FeatureSnapshot) for example in examples)


def test_feature_snapshot_contains_expected_json_safe_features() -> None:
    snapshot = make_feature_snapshot()

    assert {
        "midprice",
        "spread",
        "spread_bps",
        "return_1m",
        "volume_1m",
        "volatility_5m",
    }.issubset(snapshot.features)


def test_feature_snapshot_serializes_to_json_safe_values() -> None:
    snapshot = make_feature_snapshot()
    parsed = from_json_string(to_json_string(snapshot))

    assert parsed == to_json_dict(snapshot)
    assert parsed["feature_snapshot_id"] == "FIXTURE-FEATURE-SNAPSHOT-0001"
    assert parsed["snapshot_time"].endswith("Z")

