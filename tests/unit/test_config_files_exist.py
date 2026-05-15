from pathlib import Path

from market_relay_engine.common.config import EXPECTED_CONFIG_FILES


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_expected_config_files_exist() -> None:
    missing = [
        f"config/{file_name}"
        for file_name in EXPECTED_CONFIG_FILES
        if not (REPO_ROOT / "config" / file_name).is_file()
    ]
    assert missing == []
