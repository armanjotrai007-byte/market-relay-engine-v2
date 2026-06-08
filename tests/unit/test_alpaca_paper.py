from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from market_relay_engine.execution.alpaca_paper import (
    PAPER_BASE_URL,
    AlpacaPaperClient,
    AlpacaPaperConfig,
    AlpacaPaperError,
    client_order_id_for_intent,
    format_alpaca_quantity,
)
from market_relay_engine.execution.order_manager import OrderIntentSide
from market_relay_engine.execution.position_state import ResolvedOrderIntent


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, object] | Exception | None,
        *,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, object]:
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTPClient:
    def __init__(
        self,
        *,
        get_response: FakeResponse | None = None,
        post_response: FakeResponse | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.get_response = get_response or FakeResponse(200, {"id": "paper_account"})
        self.post_response = post_response or FakeResponse(
            200,
            {"id": "paper_order_1", "status": "accepted"},
        )
        self.exception = exception
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> FakeResponse:
        if self.exception is not None:
            raise self.exception
        self.calls.append(
            {"method": "GET", "url": url, "headers": headers, "timeout": timeout}
        )
        return self.get_response

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        if self.exception is not None:
            raise self.exception
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return self.post_response


@dataclass(frozen=True, kw_only=True)
class FlexibleIntent:
    ticker: str = "AAPL"
    side: OrderIntentSide | str = OrderIntentSide.BUY
    quantity: float = 1
    source_signal_id: str | None = "signal_1"
    risk_decision_id: str | None = "risk_decision_1"
    reason: str = "test"
    order_id: str | None = None
    order_type: str | None = None
    order_style: str | None = None


def test_config_defaults_to_paper_url_and_disabled_needs_no_keys() -> None:
    config = AlpacaPaperConfig()

    assert config.base_url == PAPER_BASE_URL
    assert config.enabled is False


def test_enabled_config_requires_key_and_secret() -> None:
    with pytest.raises(AlpacaPaperError, match="ALPACA_API_KEY"):
        AlpacaPaperConfig(secret_key="secret", enabled=True)

    with pytest.raises(AlpacaPaperError, match="ALPACA_SECRET_KEY"):
        AlpacaPaperConfig(api_key="key", enabled=True)


def test_base_url_trailing_slashes_are_normalized() -> None:
    config = AlpacaPaperConfig(base_url=f" {PAPER_BASE_URL}/// ")

    assert config.base_url == PAPER_BASE_URL


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.com",
        "https://paper-api.alpaca.markets/v2",
        "http://paper-api.alpaca.markets",
    ],
)
def test_base_url_rejects_live_and_lookalike_urls(base_url: str) -> None:
    with pytest.raises(AlpacaPaperError, match="ALPACA_BASE_URL"):
        AlpacaPaperConfig(base_url=base_url)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_positive_finite(timeout: float) -> None:
    with pytest.raises(AlpacaPaperError, match="timeout_seconds"):
        AlpacaPaperConfig(timeout_seconds=timeout)


def test_config_repr_does_not_expose_secrets() -> None:
    config = _config(api_key="key_123", secret_key="secret_123")

    assert "key_123" not in repr(config)
    assert "secret_123" not in repr(config)


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        (1, "1"),
        (1.5, "1.5"),
        (0.75, "0.75"),
        (1.1000000000000001, "1.1"),
        (1e-7, "0.0000001"),
    ],
)
def test_format_alpaca_quantity_outputs_plain_safe_strings(
    quantity: float,
    expected: str,
) -> None:
    formatted = format_alpaca_quantity(quantity)

    assert formatted == expected
    assert "e" not in formatted.lower()


@pytest.mark.parametrize("quantity", [0, -1, float("nan"), float("inf")])
def test_format_alpaca_quantity_rejects_invalid_values(quantity: float) -> None:
    with pytest.raises(AlpacaPaperError):
        format_alpaca_quantity(quantity)


def test_get_account_uses_account_endpoint_and_apca_headers() -> None:
    fake_http = FakeHTTPClient()
    client = AlpacaPaperClient(config=_config(), http_client=fake_http)

    response = client.get_account()

    assert response.success is True
    call = fake_http.calls[-1]
    assert call["method"] == "GET"
    assert call["url"] == f"{PAPER_BASE_URL}/v2/account"
    assert call["headers"]["APCA-API-KEY-ID"] == "key_123"
    assert call["headers"]["APCA-API-SECRET-KEY"] == "secret_123"
    assert call["headers"]["Content-Type"] == "application/json"
    assert "Authorization" not in call["headers"]


def test_submit_order_uses_order_endpoint_and_expected_payload() -> None:
    fake_http = FakeHTTPClient()
    client = AlpacaPaperClient(config=_config(), http_client=fake_http)

    response = client.submit_order(_intent(quantity=1.1000000000000001))

    assert response.success is True
    assert response.broker_order_id == "paper_order_1"
    assert response.raw_response == {"id": "paper_order_1", "status": "accepted"}
    call = fake_http.calls[-1]
    assert call["url"] == f"{PAPER_BASE_URL}/v2/orders"
    assert call["json"] == {
        "client_order_id": "signal_1",
        "symbol": "AAPL",
        "qty": "1.1",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }


def test_payload_includes_deterministic_client_order_id() -> None:
    fake_http = FakeHTTPClient()
    client = AlpacaPaperClient(config=_config(), http_client=fake_http)
    intent = _intent(source_signal_id="signal_same_id")

    client.submit_order(intent)
    client.submit_order(intent)

    first_payload = fake_http.calls[-2]["json"]
    second_payload = fake_http.calls[-1]["json"]
    assert first_payload["client_order_id"] == "signal_same_id"
    assert first_payload["client_order_id"] == second_payload["client_order_id"]


def test_client_order_id_prefers_intent_order_id() -> None:
    fake_http = FakeHTTPClient()
    client = AlpacaPaperClient(config=_config(), http_client=fake_http)

    client.submit_order(
        FlexibleIntent(order_id="order_123", source_signal_id="signal_456")
    )

    assert fake_http.calls[-1]["json"]["client_order_id"] == "order_123"


def test_long_client_order_id_is_safely_truncated() -> None:
    long_id = "signal_" + ("x" * 100)
    safe_id = client_order_id_for_intent(_intent(source_signal_id=long_id))

    assert safe_id.startswith("signal_")
    assert len(safe_id) == 48


def test_missing_usable_client_order_id_raises() -> None:
    client = AlpacaPaperClient(config=_config(), http_client=FakeHTTPClient())

    with pytest.raises(AlpacaPaperError, match="order_id or source_signal_id"):
        client.submit_order(FlexibleIntent(order_id=None, source_signal_id=""))


def test_client_order_id_sanitizes_to_alpaca_safe_ascii() -> None:
    safe_id = client_order_id_for_intent(
        FlexibleIntent(source_signal_id=" signal @ 123 / weird ")
    )

    assert safe_id == "signal_123_weird"


def test_disabled_config_blocks_requests() -> None:
    client = AlpacaPaperClient(config=AlpacaPaperConfig(), http_client=FakeHTTPClient())

    with pytest.raises(AlpacaPaperError, match="disabled"):
        client.submit_order(_intent())


def test_submit_order_rejects_close_position_with_resolution_guidance() -> None:
    client = AlpacaPaperClient(config=_config(), http_client=FakeHTTPClient())

    with pytest.raises(AlpacaPaperError, match="resolve_close_position_intent"):
        client.submit_order(_intent(side=OrderIntentSide.CLOSE_POSITION))


def test_market_or_default_order_works() -> None:
    client = AlpacaPaperClient(config=_config(), http_client=FakeHTTPClient())

    default_response = client.submit_order(_intent())
    market_response = client.submit_order(FlexibleIntent(order_type="MARKET"))

    assert default_response.success is True
    assert market_response.success is True


@pytest.mark.parametrize(
    "intent",
    [
        FlexibleIntent(order_type="limit"),
        FlexibleIntent(order_style="LIMIT_AT_MID"),
    ],
)
def test_non_market_order_type_is_rejected(intent: FlexibleIntent) -> None:
    client = AlpacaPaperClient(config=_config(), http_client=FakeHTTPClient())

    with pytest.raises(AlpacaPaperError, match="MARKET"):
        client.submit_order(intent)


def test_sell_intent_builds_sell_side() -> None:
    fake_http = FakeHTTPClient()
    client = AlpacaPaperClient(config=_config(), http_client=fake_http)

    client.submit_order(_intent(side=OrderIntentSide.SELL, quantity=0.75))

    payload = fake_http.calls[-1]["json"]
    assert payload["side"] == "sell"
    assert payload["qty"] == "0.75"


def test_403_response_returns_failure() -> None:
    client = AlpacaPaperClient(
        config=_config(),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(403, {"message": "forbidden"})
        ),
    )

    response = client.submit_order(_intent())

    assert response.success is False
    assert response.status_code == 403
    assert response.error_message == "forbidden"


def test_422_response_returns_failure_and_broker_error() -> None:
    client = AlpacaPaperClient(
        config=_config(),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(422, {"error": "qty is invalid"})
        ),
    )

    response = client.submit_order(_intent())

    assert response.success is False
    assert response.status_code == 422
    assert response.error_message == "qty is invalid"


def test_non_json_broker_response_returns_failure() -> None:
    client = AlpacaPaperClient(
        config=_config(),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(500, ValueError("not json"), text="not json")
        ),
    )

    response = client.submit_order(_intent())

    assert response.success is False
    assert response.status_code == 500
    assert response.error_message == "not json"
    assert response.raw_response is None


def test_network_exception_returns_failure_without_crash() -> None:
    client = AlpacaPaperClient(
        config=_config(),
        http_client=FakeHTTPClient(exception=requests.exceptions.Timeout("timed out")),
    )

    response = client.submit_order(_intent())

    assert response.success is False
    assert response.status_code is None
    assert "timed out" in (response.error_message or "")


def test_failed_raw_response_redacts_api_key() -> None:
    client = AlpacaPaperClient(
        config=_config(api_key="key_123", secret_key="secret_123"),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(
                422,
                {
                    "message": "broker echoed key_123",
                    "headers": {"APCA-API-KEY-ID": "key_123"},
                },
            )
        ),
    )

    response = client.submit_order(_intent())

    raw_text = repr(response.raw_response)
    assert "key_123" not in raw_text
    assert "<redacted>" in raw_text


def test_failed_raw_response_redacts_secret_key() -> None:
    client = AlpacaPaperClient(
        config=_config(api_key="key_123", secret_key="secret_123"),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(
                403,
                {
                    "message": "broker echoed secret_123",
                    "headers": {"APCA-API-SECRET-KEY": "secret_123"},
                },
            )
        ),
    )

    response = client.submit_order(_intent())

    raw_text = repr(response.raw_response)
    assert "secret_123" not in raw_text
    assert "<redacted>" in raw_text


def test_nested_raw_response_secrets_are_redacted() -> None:
    client = AlpacaPaperClient(
        config=_config(api_key="key_123", secret_key="secret_123"),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(
                422,
                {
                    "message": "bad key_123 secret_123",
                    "details": {
                        "headers": [
                            "key_123",
                            {"secret": "secret_123"},
                            ("keep", "secret_123"),
                        ],
                        "normal": "preserve-me",
                    },
                },
            )
        ),
    )

    response = client.submit_order(_intent())

    raw_text = repr(response.raw_response)
    assert "key_123" not in raw_text
    assert "secret_123" not in raw_text
    assert "preserve-me" in raw_text
    assert raw_text.count("<redacted>") >= 4


def test_error_messages_redact_secrets() -> None:
    client = AlpacaPaperClient(
        config=_config(api_key="key_123", secret_key="secret_123"),
        http_client=FakeHTTPClient(
            post_response=FakeResponse(
                422,
                {"message": "bad key_123 and secret_123"},
            )
        ),
    )

    response = client.submit_order(_intent())

    assert response.success is False
    assert response.error_message is not None
    assert "key_123" not in response.error_message
    assert "secret_123" not in response.error_message
    assert "<redacted>" in response.error_message


def test_network_exception_redacts_secrets() -> None:
    client = AlpacaPaperClient(
        config=_config(api_key="key_123", secret_key="secret_123"),
        http_client=FakeHTTPClient(
            exception=requests.exceptions.ConnectionError("failed with key_123 secret_123")
        ),
    )

    response = client.submit_order(_intent())

    assert response.error_message is not None
    assert "key_123" not in response.error_message
    assert "secret_123" not in response.error_message


def test_no_real_network_client_is_used_in_unit_tests() -> None:
    fake_http = FakeHTTPClient()
    client = AlpacaPaperClient(config=_config(), http_client=fake_http)

    client.get_account()
    client.submit_order(_intent())

    assert [call["method"] for call in fake_http.calls] == ["GET", "POST"]


def test_alpaca_paper_source_keeps_pr20_scope_small() -> None:
    source = Path("src/market_relay_engine/execution/alpaca_paper.py").read_text(
        encoding="utf-8"
    )

    assert "Authorization" not in source
    assert "market_relay_engine.questdb" not in source
    assert "market_relay_engine.model" not in source
    assert "market_relay_engine.ai_context" not in source
    assert "market_relay_engine.context" not in source
    assert "async def" not in source
    assert "bracket" not in source.lower()
    assert "take_profit" not in source.lower()
    assert "stop_loss" not in source.lower()


def _config(
    *,
    api_key: str = "key_123",
    secret_key: str = "secret_123",
) -> AlpacaPaperConfig:
    return AlpacaPaperConfig(
        api_key=api_key,
        secret_key=secret_key,
        enabled=True,
    )


def _intent(
    *,
    side: OrderIntentSide = OrderIntentSide.BUY,
    quantity: float = 1,
    source_signal_id: str = "signal_1",
) -> ResolvedOrderIntent:
    return ResolvedOrderIntent(
        ticker="AAPL",
        side=side,
        quantity=quantity,
        source_signal_id=source_signal_id,
        risk_decision_id="risk_decision_1",
        reason="test",
    )
