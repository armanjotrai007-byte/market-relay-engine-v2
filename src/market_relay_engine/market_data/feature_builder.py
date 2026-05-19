"""Canonical MarketRecord to FeatureSnapshot builder."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import timedelta
import math
from typing import Any, Iterable

from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.market import MarketRecord


V1_FEATURE_KEYS = {
    "ticker",
    "record_count_window",
    "trade_count_window",
    "quote_count_window",
    "last_price",
    "last_trade_price",
    "last_trade_size",
    "last_bid_price",
    "last_ask_price",
    "last_bid_size",
    "last_ask_size",
    "midprice",
    "spread",
    "spread_bps",
    "is_crossed_or_locked",
    "lookback_window_seconds",
    "volume_window",
    "price_return_window",
    "midprice_return_window",
    "midprice_change_from_previous",
    "simple_volatility_window",
}

_TRADE_RECORD_TYPES = {"trade", "trades", "tbbo_trade"}
_QUOTE_RECORD_TYPES = {"quote", "bbo", "mbp-1", "tbbo", "bbo-1s", "bbo-1m"}


class FeatureBuilderError(ValueError):
    """Raised when a feature snapshot cannot be built safely."""


@dataclass(frozen=True, kw_only=True)
class FeatureBuilderConfig:
    """Small configuration for the canonical V1 feature builder."""

    lookback_window_seconds: int = 60
    feature_version: str = "feature_v1"
    max_records_per_ticker: int = 50000

    def __post_init__(self) -> None:
        if isinstance(self.lookback_window_seconds, bool) or self.lookback_window_seconds <= 0:
            raise FeatureBuilderError("lookback_window_seconds must be a positive integer")
        if not self.feature_version:
            raise FeatureBuilderError("feature_version must be a non-empty string")
        if isinstance(self.max_records_per_ticker, bool) or self.max_records_per_ticker <= 0:
            raise FeatureBuilderError("max_records_per_ticker must be a positive integer")


class FeatureBuilder:
    """Stateful canonical feature builder for normalized market records."""

    def __init__(self, config: FeatureBuilderConfig | None = None) -> None:
        self.config = config or FeatureBuilderConfig()
        self._records_by_ticker: dict[str, deque[MarketRecord]] = {}
        self._max_event_time_seen: dict[str, Any] = {}

    def update(self, record: MarketRecord) -> FeatureSnapshot:
        """Process one record in caller order and return the current snapshot."""
        self._validate_record(record)
        records = self._records_by_ticker.setdefault(record.ticker, deque())
        records.append(record)

        previous_max = self._max_event_time_seen.get(record.ticker)
        if previous_max is None or record.event_time > previous_max:
            self._max_event_time_seen[record.ticker] = record.event_time

        self._prune_ticker(record.ticker)
        return self.build_snapshot(record.ticker, trace_id=record.trace_id)

    def build_snapshot(
        self,
        ticker: str,
        snapshot_time: Any | None = None,
        trace_id: str | None = None,
    ) -> FeatureSnapshot:
        """Build a snapshot from the current active window for one ticker."""
        records = self._records_by_ticker.get(ticker)
        if not records:
            raise FeatureBuilderError(f"No records available for ticker: {ticker}")

        resolved_snapshot_time = snapshot_time or self._max_event_time_seen.get(ticker)
        if resolved_snapshot_time is None:
            resolved_snapshot_time = records[-1].event_time

        features = _build_v1_features(
            records,
            ticker=ticker,
            lookback_window_seconds=self.config.lookback_window_seconds,
        )
        _validate_v1_features(features)

        return FeatureSnapshot(
            snapshot_time=resolved_snapshot_time,
            ticker=ticker,
            feature_version=self.config.feature_version,
            features=features,
            source_record_count=len(records),
            lookback_window_seconds=float(self.config.lookback_window_seconds),
            trace_id=trace_id,
        )

    def reset(self, ticker: str | None = None) -> None:
        """Clear all rolling state, or only the state for one ticker."""
        if ticker is None:
            self._records_by_ticker.clear()
            self._max_event_time_seen.clear()
            return
        self._records_by_ticker.pop(ticker, None)
        self._max_event_time_seen.pop(ticker, None)

    def _validate_record(self, record: MarketRecord) -> None:
        if not isinstance(record, MarketRecord):
            raise FeatureBuilderError("FeatureBuilder.update requires a MarketRecord")
        if not record.ticker:
            raise FeatureBuilderError("MarketRecord.ticker must be non-empty")

    def _prune_ticker(self, ticker: str) -> None:
        records = self._records_by_ticker.get(ticker)
        max_event_time = self._max_event_time_seen.get(ticker)
        if not records or max_event_time is None:
            return

        cutoff = max_event_time - timedelta(seconds=self.config.lookback_window_seconds)
        self._records_by_ticker[ticker] = deque(
            record for record in records if record.event_time >= cutoff
        )
        records = self._records_by_ticker[ticker]
        while len(records) > self.config.max_records_per_ticker:
            records.popleft()


def build_feature_snapshot(
    records: Iterable[MarketRecord],
    config: FeatureBuilderConfig | None = None,
    feature_version: str | None = None,
    trace_id: str | None = None,
) -> FeatureSnapshot:
    """Sort records by event time and return the final single-ticker snapshot."""
    record_list = list(records)
    if not record_list:
        raise FeatureBuilderError("Cannot build feature snapshot from empty records")
    for record in record_list:
        if not isinstance(record, MarketRecord):
            raise FeatureBuilderError("build_feature_snapshot requires MarketRecord inputs")

    tickers = {record.ticker for record in record_list}
    if len(tickers) != 1:
        raise FeatureBuilderError("build_feature_snapshot supports exactly one ticker")

    resolved_config = config or FeatureBuilderConfig()
    if feature_version is not None:
        resolved_config = replace(resolved_config, feature_version=feature_version)

    builder = FeatureBuilder(resolved_config)
    snapshot: FeatureSnapshot | None = None
    for record in sorted(record_list, key=lambda item: item.event_time):
        snapshot = builder.update(record)

    if snapshot is None:
        raise FeatureBuilderError("No feature snapshot was produced")
    if trace_id is not None:
        snapshot = builder.build_snapshot(snapshot.ticker, trace_id=trace_id)
    return snapshot


def _build_v1_features(
    records: deque[MarketRecord],
    *,
    ticker: str,
    lookback_window_seconds: int,
) -> dict[str, Any]:
    observations = [_record_observation(record) for record in records]
    trade_observations = [item for item in observations if item["is_trade_like"]]
    quote_observations = [item for item in observations if item["is_quote_like"]]

    trade_prices = [
        item["price"] for item in trade_observations if item["price"] is not None
    ]
    midprices = [item["midprice"] for item in observations if item["midprice"] is not None]
    latest_trade_price_observation = _latest_with_value(trade_observations, "price")
    latest_midprice = midprices[-1] if midprices else None
    latest_spread = _latest_value(observations, "spread")
    latest_bid_price = _latest_value(observations, "bid_price")
    latest_ask_price = _latest_value(observations, "ask_price")
    latest_complete_quote = _latest_complete_quote(observations)

    features: dict[str, Any] = {
        "ticker": ticker,
        "record_count_window": len(observations),
        "trade_count_window": len(trade_observations),
        "quote_count_window": len(quote_observations),
        "last_price": (
            latest_trade_price_observation["price"]
            if latest_trade_price_observation
            else latest_midprice
        ),
        "last_trade_price": (
            latest_trade_price_observation["price"]
            if latest_trade_price_observation
            else None
        ),
        "last_trade_size": _latest_value(trade_observations, "size"),
        "last_bid_price": latest_bid_price,
        "last_ask_price": latest_ask_price,
        "last_bid_size": _latest_value(observations, "bid_size"),
        "last_ask_size": _latest_value(observations, "ask_size"),
        "midprice": latest_midprice,
        "spread": latest_spread,
        "spread_bps": _compute_spread_bps(latest_spread, latest_midprice),
        "is_crossed_or_locked": _is_crossed_or_locked(latest_complete_quote),
        "lookback_window_seconds": lookback_window_seconds,
        "volume_window": sum(
            item["size"] for item in trade_observations if item["size"] is not None
        ),
        "price_return_window": _return_from_values(trade_prices),
        "midprice_return_window": _return_from_values(midprices),
        "midprice_change_from_previous": _change_from_previous(midprices),
        "simple_volatility_window": _simple_volatility(midprices),
    }
    return features


def _record_observation(record: MarketRecord) -> dict[str, Any]:
    price = _optional_finite_float(record.price, "price")
    size = _optional_finite_float(record.size, "size")
    bid_price = _optional_finite_float(record.bid_price, "bid_price")
    ask_price = _optional_finite_float(record.ask_price, "ask_price")
    bid_size = _optional_finite_float(record.bid_size, "bid_size")
    ask_size = _optional_finite_float(record.ask_size, "ask_size")
    midprice = _optional_finite_float(record.midprice, "midprice")
    spread = _optional_finite_float(record.spread, "spread")

    if midprice is None:
        midprice = _compute_midprice(bid_price, ask_price)
    if spread is None:
        spread = _compute_spread(bid_price, ask_price)

    record_type = record.record_type.strip().lower()
    has_quote_values = bid_price is not None or ask_price is not None
    has_complete_quote = bid_price is not None and ask_price is not None
    is_trade_like = record_type in _TRADE_RECORD_TYPES or (
        price is not None and size is not None and not has_quote_values
    )
    is_quote_like = record_type in _QUOTE_RECORD_TYPES or has_complete_quote

    return {
        "price": price,
        "size": size,
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "midprice": midprice,
        "spread": spread,
        "is_trade_like": is_trade_like,
        "is_quote_like": is_quote_like,
    }


def _optional_finite_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise FeatureBuilderError(f"{field_name} must be numeric, not bool")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise FeatureBuilderError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric_value):
        return None
    return numeric_value


def _compute_midprice(bid_price: float | None, ask_price: float | None) -> float | None:
    if bid_price is None or ask_price is None:
        return None
    return (bid_price + ask_price) / 2


def _compute_spread(bid_price: float | None, ask_price: float | None) -> float | None:
    if bid_price is None or ask_price is None:
        return None
    return ask_price - bid_price


def _compute_spread_bps(spread: float | None, midprice: float | None) -> float | None:
    if spread is None or midprice is None or midprice <= 0:
        return None
    return spread / midprice * 10000


def _latest_with_value(
    observations: list[dict[str, Any]],
    field_name: str,
) -> dict[str, Any] | None:
    for observation in reversed(observations):
        if observation[field_name] is not None:
            return observation
    return None


def _latest_value(observations: list[dict[str, Any]], field_name: str) -> float | None:
    latest = _latest_with_value(observations, field_name)
    return latest[field_name] if latest else None


def _latest_complete_quote(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    for observation in reversed(observations):
        if observation["bid_price"] is not None and observation["ask_price"] is not None:
            return observation
    return None


def _is_crossed_or_locked(observation: dict[str, Any] | None) -> bool:
    if observation is None:
        return False
    return observation["bid_price"] >= observation["ask_price"]


def _return_from_values(values: list[float]) -> float | None:
    if len(values) < 2 or values[0] <= 0:
        return None
    return values[-1] / values[0] - 1


def _change_from_previous(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return values[-1] - values[-2]


def _simple_volatility(midprices: list[float]) -> float | None:
    returns: list[float] = []
    for previous, current in zip(midprices, midprices[1:]):
        if previous > 0:
            returns.append(current / previous - 1)
    if len(returns) < 2:
        return None
    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / len(returns)
    return math.sqrt(variance)


def _validate_v1_features(features: dict[str, Any]) -> None:
    if set(features) != V1_FEATURE_KEYS:
        raise FeatureBuilderError("V1 feature keys do not match V1_FEATURE_KEYS")
    _validate_json_safe(features, "features")


def _validate_json_safe(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FeatureBuilderError(f"{path} must not contain NaN or Infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FeatureBuilderError(f"{path} contains a non-string key")
            _validate_json_safe(item, f"{path}.{key}")
        return
    raise FeatureBuilderError(f"{path} contains non-JSON-safe value: {type(value).__name__}")
