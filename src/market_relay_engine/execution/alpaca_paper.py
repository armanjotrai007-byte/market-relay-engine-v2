"""Paper-only Alpaca Trading API wrapper."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math
import os
from pathlib import Path
import re

from dotenv import load_dotenv
import requests

from market_relay_engine.execution.order_manager import OrderIntentSide


PAPER_BASE_URL = "https://paper-api.alpaca.markets"
MAX_CLIENT_ORDER_ID_LENGTH = 48
_QUANTITY_QUANTIZER = Decimal("0.000000001")
_CLIENT_ORDER_ID_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


class AlpacaPaperError(RuntimeError):
    """Raised for local Alpaca paper safety or configuration failures."""


@dataclass(frozen=True, kw_only=True)
class AlpacaPaperConfig:
    """Configuration for the paper-only Alpaca client."""

    api_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    base_url: str = PAPER_BASE_URL
    enabled: bool = False
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        api_key = _clean_optional_secret(self.api_key)
        secret_key = _clean_optional_secret(self.secret_key)
        timeout_seconds = _positive_finite_float(
            self.timeout_seconds,
            "timeout_seconds",
        )
        base_url = normalize_paper_base_url(self.base_url)

        if self.enabled and not api_key:
            raise AlpacaPaperError("ALPACA_API_KEY is required when Alpaca paper is enabled")
        if self.enabled and not secret_key:
            raise AlpacaPaperError("ALPACA_SECRET_KEY is required when Alpaca paper is enabled")

        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "secret_key", secret_key)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)

    @classmethod
    def from_env(
        cls,
        *,
        enabled: bool = False,
        base_dir: str | Path | None = None,
        timeout_seconds: float = 5.0,
    ) -> "AlpacaPaperConfig":
        """Load Alpaca paper config from a local ``.env`` file and environment."""
        env_path = (Path(base_dir) if base_dir is not None else Path.cwd()) / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)

        return cls(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            base_url=os.getenv("ALPACA_BASE_URL") or PAPER_BASE_URL,
            enabled=enabled,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True, kw_only=True)
class AlpacaPaperResponse:
    """JSON-safe result from an Alpaca paper API request."""

    success: bool
    status_code: int | None
    broker_order_id: str | None
    raw_response: dict[str, object] | None
    error_message: str | None


class AlpacaPaperClient:
    """Small paper-only Alpaca Trading API client."""

    def __init__(
        self,
        config: AlpacaPaperConfig,
        http_client: object | None = None,
    ) -> None:
        self.config = config
        self._http_client = http_client or requests.Session()

    def get_account(self) -> AlpacaPaperResponse:
        """Fetch the Alpaca paper account record."""
        self._require_enabled_credentials()
        return self._request("GET", "/v2/account")

    def submit_order(self, intent: object) -> AlpacaPaperResponse:
        """Submit one resolved BUY/SELL intent to Alpaca paper trading."""
        self._require_enabled_credentials()
        payload = self._build_order_payload(intent)
        return self._request("POST", "/v2/orders", payload=payload)

    def _build_order_payload(self, intent: object) -> dict[str, str]:
        side = _intent_side(intent)
        if side is OrderIntentSide.CLOSE_POSITION:
            raise AlpacaPaperError(
                "CLOSE_POSITION must be resolved with resolve_close_position_intent "
                "before submit_order"
            )
        if side not in {OrderIntentSide.BUY, OrderIntentSide.SELL}:
            raise AlpacaPaperError("submit_order accepts only resolved BUY/SELL intents")

        _require_market_only(intent)
        ticker = _intent_ticker(intent)
        quantity = format_alpaca_quantity(getattr(intent, "quantity", None))
        client_order_id = client_order_id_for_intent(intent)

        return {
            "client_order_id": client_order_id,
            "symbol": ticker,
            "qty": quantity,
            "side": side.value.lower(),
            "type": "market",
            "time_in_force": "day",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | None = None,
    ) -> AlpacaPaperResponse:
        url = f"{self.config.base_url}{path}"
        headers = self._headers()
        try:
            if method == "GET":
                response = self._http_client.get(  # type: ignore[attr-defined]
                    url,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                )
            elif method == "POST":
                response = self._http_client.post(  # type: ignore[attr-defined]
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
            else:
                raise AlpacaPaperError(f"Unsupported Alpaca paper HTTP method: {method}")
        except AlpacaPaperError:
            raise
        except Exception as exc:  # noqa: BLE001 - broker/network failures return failures.
            return AlpacaPaperResponse(
                success=False,
                status_code=None,
                broker_order_id=None,
                raw_response=None,
                error_message=self._redact(f"Alpaca paper request failed: {exc}"),
            )

        return self._response_from_http_response(response)

    def _response_from_http_response(self, response: object) -> AlpacaPaperResponse:
        status_code = getattr(response, "status_code", None)
        raw_response = _json_response(response)
        if raw_response is None:
            return AlpacaPaperResponse(
                success=False,
                status_code=status_code,
                broker_order_id=None,
                raw_response=None,
                error_message=self._redact(
                    _text_response(response) or "Alpaca paper response was not valid JSON"
                ),
            )

        redacted_response = _redact_payload_dict(raw_response, self._secrets())
        success = isinstance(status_code, int) and 200 <= status_code < 300
        broker_order_id = _optional_string(redacted_response.get("id")) if success else None
        error_message = None if success else self._redact(_broker_error_message(raw_response))
        return AlpacaPaperResponse(
            success=success,
            status_code=status_code,
            broker_order_id=broker_order_id,
            raw_response=redacted_response,
            error_message=error_message,
        )

    def _headers(self) -> dict[str, str]:
        self._require_enabled_credentials()
        assert self.config.api_key is not None
        assert self.config.secret_key is not None
        return {
            "APCA-API-KEY-ID": self.config.api_key,
            "APCA-API-SECRET-KEY": self.config.secret_key,
            "Content-Type": "application/json",
        }

    def _require_enabled_credentials(self) -> None:
        if not self.config.enabled:
            raise AlpacaPaperError("Alpaca paper trading is disabled")
        if not self.config.api_key:
            raise AlpacaPaperError("ALPACA_API_KEY is required for Alpaca paper requests")
        if not self.config.secret_key:
            raise AlpacaPaperError("ALPACA_SECRET_KEY is required for Alpaca paper requests")

    def _secrets(self) -> tuple[str, ...]:
        return tuple(
            secret
            for secret in (self.config.api_key, self.config.secret_key)
            if secret
        )

    def _redact(self, message: str) -> str:
        return str(_redact_payload(message, self._secrets()))


def normalize_paper_base_url(base_url: str | None) -> str:
    """Return the exact allowed paper base URL or raise."""
    candidate = PAPER_BASE_URL if base_url is None else str(base_url).strip()
    if not candidate:
        candidate = PAPER_BASE_URL
    normalized = candidate.rstrip("/")
    if normalized != PAPER_BASE_URL:
        raise AlpacaPaperError(
            "ALPACA_BASE_URL must be exactly the Alpaca paper Trading API base URL"
        )
    return normalized


def format_alpaca_quantity(quantity: float) -> str:
    """Format an Alpaca quantity string without binary-float artifacts."""
    if isinstance(quantity, bool):
        raise AlpacaPaperError("quantity must be numeric")
    try:
        numeric_quantity = float(quantity)
    except (TypeError, ValueError) as exc:
        raise AlpacaPaperError("quantity must be numeric") from exc
    if not math.isfinite(numeric_quantity):
        raise AlpacaPaperError("quantity must be finite")
    if numeric_quantity <= 0:
        raise AlpacaPaperError("quantity must be positive")

    try:
        decimal_quantity = Decimal(str(quantity))
    except (InvalidOperation, ValueError) as exc:
        raise AlpacaPaperError("quantity must be Decimal-compatible") from exc
    if not decimal_quantity.is_finite():
        raise AlpacaPaperError("quantity must be finite")
    if decimal_quantity <= 0:
        raise AlpacaPaperError("quantity must be positive")

    if decimal_quantity.as_tuple().exponent < -9:
        decimal_quantity = decimal_quantity.quantize(
            _QUANTITY_QUANTIZER,
            rounding=ROUND_DOWN,
        )
    if decimal_quantity <= 0:
        raise AlpacaPaperError("quantity is too small for Alpaca paper quantity precision")

    text = format(decimal_quantity.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def client_order_id_for_intent(intent: object) -> str:
    """Build a deterministic Alpaca client_order_id from a local intent."""
    raw_id = getattr(intent, "order_id", None) or getattr(intent, "source_signal_id", None)
    if raw_id is None or not str(raw_id).strip():
        raise AlpacaPaperError("Resolved order intent must include order_id or source_signal_id")

    ascii_id = str(raw_id).strip().encode("ascii", "ignore").decode("ascii")
    safe_id = _CLIENT_ORDER_ID_SAFE_PATTERN.sub("_", ascii_id)
    safe_id = re.sub(r"_+", "_", safe_id).strip("_-")
    if not safe_id:
        raise AlpacaPaperError("Resolved order intent ID is not usable as client_order_id")
    return safe_id[:MAX_CLIENT_ORDER_ID_LENGTH]


def _redact_payload(value: object, secrets: Sequence[str]) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        return redacted
    if isinstance(value, dict):
        return {
            _redact_payload(key, secrets): _redact_payload(child, secrets)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(child, secrets) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_payload(child, secrets) for child in value)
    return value


def _redact_payload_dict(
    value: dict[str, object],
    secrets: Sequence[str],
) -> dict[str, object]:
    redacted = _redact_payload(value, secrets)
    if not isinstance(redacted, dict):
        return {}
    return {
        str(key): child
        for key, child in redacted.items()
    }


def _clean_optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_finite_float(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise AlpacaPaperError(f"{field_name} must be numeric")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise AlpacaPaperError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric_value):
        raise AlpacaPaperError(f"{field_name} must be finite")
    if numeric_value <= 0:
        raise AlpacaPaperError(f"{field_name} must be positive")
    return numeric_value


def _intent_side(intent: object) -> OrderIntentSide:
    raw_side = getattr(intent, "side", None)
    side_value = raw_side.value if hasattr(raw_side, "value") else raw_side
    if side_value is None:
        raise AlpacaPaperError("Resolved order intent must include a side")
    try:
        return OrderIntentSide(str(side_value).upper())
    except ValueError as exc:
        raise AlpacaPaperError("Resolved order intent side must be BUY or SELL") from exc


def _intent_ticker(intent: object) -> str:
    ticker = getattr(intent, "ticker", None)
    if ticker is None or not str(ticker).strip():
        raise AlpacaPaperError("Resolved order intent must include a ticker")
    return str(ticker).strip().upper()


def _require_market_only(intent: object) -> None:
    for field_name in ("order_type", "order_style"):
        raw_value = getattr(intent, field_name, None)
        if raw_value is None:
            continue
        order_value = raw_value.value if hasattr(raw_value, "value") else raw_value
        if str(order_value).strip().lower() != "market":
            raise AlpacaPaperError("PR20 Alpaca paper wrapper supports MARKET orders only")


def _json_response(response: object) -> dict[str, object] | None:
    try:
        parsed = response.json()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - malformed broker JSON should be reported safely.
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _text_response(response: object) -> str | None:
    text = getattr(response, "text", None)
    if text is None:
        return None
    return str(text) or None


def _broker_error_message(raw_response: dict[str, object]) -> str:
    for key in ("message", "error"):
        value = raw_response.get(key)
        if value:
            return str(value)
    return "Alpaca paper request failed"


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
