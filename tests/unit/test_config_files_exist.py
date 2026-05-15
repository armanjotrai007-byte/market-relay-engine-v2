from pathlib import Path

from market_relay_engine.common.config import load_yaml_config


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_required_config_files_exist() -> None:
    required = [
        "config/symbols.yaml",
        "config/context_sources.yaml",
        "config/risk_limits.yaml",
        "config/questdb.yaml",
        "config/model_config.yaml",
    ]

    missing = [path for path in required if not (REPO_ROOT / path).is_file()]
    assert missing == []


def test_yaml_loader_can_load_symbols_config() -> None:
    loaded = load_yaml_config("config/symbols.yaml", base_dir=REPO_ROOT)
    assert loaded["symbols"]["defense"] == ["LMT", "RTX", "NOC", "GD"]
    assert loaded["symbols"]["oil_energy"] == ["XOM", "CVX"]
