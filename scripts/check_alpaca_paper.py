"""Validate Alpaca paper wrapper behavior without submitting orders."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.execution.alpaca_paper import (  # noqa: E402
    AlpacaPaperClient,
    AlpacaPaperConfig,
    AlpacaPaperError,
)
from market_relay_engine.execution.order_manager import OrderIntentSide  # noqa: E402
from market_relay_engine.execution.position_state import ResolvedOrderIntent  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeHTTPClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return _FakeResponse(200, {"id": "paper_account"})

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return _FakeResponse(200, {"id": "paper_order_1", "status": "accepted"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--required",
        action="store_true",
        help="Require real local Alpaca paper credentials and check account only.",
    )
    args = parser.parse_args()

    if args.required:
        return _run_required_account_check()
    return _run_offline_check()


def _run_offline_check() -> int:
    fake_http = _FakeHTTPClient()
    config = AlpacaPaperConfig(
        api_key="fake_api_key",
        secret_key="fake_secret_key",
        enabled=True,
    )
    client = AlpacaPaperClient(config=config, http_client=fake_http)

    account_response = client.get_account()
    assert account_response.success is True
    assert fake_http.calls[-1]["url"].endswith("/v2/account")

    order_response = client.submit_order(_resolved_intent())
    assert order_response.success is True
    order_call = fake_http.calls[-1]
    assert order_call["url"].endswith("/v2/orders")
    assert order_call["json"] == {
        "client_order_id": "signal_check_alpaca_paper",
        "symbol": "AAPL",
        "qty": "1.5",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }

    print("Alpaca paper check PASS")
    return 0


def _run_required_account_check() -> int:
    try:
        config = AlpacaPaperConfig.from_env(enabled=True, base_dir=REPO_ROOT)
        client = AlpacaPaperClient(config=config)
        response = client.get_account()
    except AlpacaPaperError as exc:
        print(f"Alpaca paper required check FAILED: {exc}")
        return 1

    if not response.success:
        print(
            "Alpaca paper required check FAILED: "
            f"{response.error_message or 'account request failed'}"
        )
        return 1

    print("Alpaca paper account check PASS")
    return 0


def _resolved_intent() -> ResolvedOrderIntent:
    return ResolvedOrderIntent(
        ticker="AAPL",
        side=OrderIntentSide.BUY,
        quantity=1.5,
        source_signal_id="signal_check_alpaca_paper",
        risk_decision_id="risk_decision_check_alpaca_paper",
        reason="offline_check",
    )


if __name__ == "__main__":
    raise SystemExit(main())
