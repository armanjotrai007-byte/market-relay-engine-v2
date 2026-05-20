"""Pure trading cost estimates for Market Relay Engine V2."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
import math
from typing import Any

from market_relay_engine.contracts.model import SignalSide


BPS_MULTIPLIER = 10000.0
SUPPORTED_TRADE_HORIZONS = ("1m", "5m", "15m")
TRADE_ENTRY_SIDES = {SignalSide.BUY, SignalSide.SELL}


class CostModelError(ValueError):
    """Raised when a cost estimate cannot be calculated safely."""


class OrderStyle(str, Enum):
    """Supported V1 order styles."""

    MARKET = "MARKET"
    LIMIT_AT_MID = "LIMIT_AT_MID"


class TradeHorizon(str, Enum):
    """Supported V1 trade horizons."""

    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"


@dataclass(frozen=True, kw_only=True)
class CostModelConfig:
    """Configuration for deterministic V1 trading cost estimates."""

    min_edge_bps: float = 1.0
    round_trip_slippage_per_share: float = 0.02
    market_order_spread_multiplier: float = 1.0
    limit_order_spread_multiplier: float = 0.0
    limit_order_missed_fill_probability_by_horizon: dict[str, float] = field(
        default_factory=lambda: {"1m": 0.30, "5m": 0.20, "15m": 0.10}
    )
    size_penalty_bps_per_1000_shares: float = 0.25
    size_penalty_free_quantity: float = 100.0
    fallback_minimum_spread_bps: float = 1.0
    assumptions_version: str = "cost_model_v1"

    def __post_init__(self) -> None:
        _finite_non_negative(self.min_edge_bps, "min_edge_bps")
        _finite_non_negative(
            self.round_trip_slippage_per_share,
            "round_trip_slippage_per_share",
        )
        _finite_non_negative(
            self.market_order_spread_multiplier,
            "market_order_spread_multiplier",
        )
        _finite_non_negative(
            self.limit_order_spread_multiplier,
            "limit_order_spread_multiplier",
        )
        _finite_non_negative(
            self.size_penalty_bps_per_1000_shares,
            "size_penalty_bps_per_1000_shares",
        )
        _finite_non_negative(
            self.size_penalty_free_quantity,
            "size_penalty_free_quantity",
        )
        _finite_non_negative(
            self.fallback_minimum_spread_bps,
            "fallback_minimum_spread_bps",
        )
        if not isinstance(self.assumptions_version, str) or not self.assumptions_version.strip():
            raise CostModelError("assumptions_version must be a non-empty string")

        probabilities = self.limit_order_missed_fill_probability_by_horizon
        if not isinstance(probabilities, dict):
            raise CostModelError(
                "limit_order_missed_fill_probability_by_horizon must be a dictionary"
            )
        if set(probabilities) != set(SUPPORTED_TRADE_HORIZONS):
            raise CostModelError(
                "limit_order_missed_fill_probability_by_horizon must define "
                "1m, 5m, and 15m"
            )
        for horizon, probability in probabilities.items():
            _validate_horizon(horizon)
            probability_value = _finite_float(
                probability,
                f"limit_order_missed_fill_probability_by_horizon.{horizon}",
            )
            if probability_value < 0 or probability_value > 1:
                raise CostModelError(
                    "limit_order_missed_fill_probability_by_horizon "
                    f"{horizon} must be between 0 and 1"
                )


@dataclass(frozen=True, kw_only=True)
class CostEstimate:
    """JSON-safe output from the V1 cost model."""

    ticker: str
    side: SignalSide
    horizon: str
    order_style: str
    quantity: float
    midprice: float
    spread_bps: float
    expected_gross_move_bps: float
    spread_cost_bps: float
    estimated_slippage_bps: float
    size_penalty_bps: float
    base_cost_bps: float
    missed_fill_probability: float
    pre_missed_fill_net_edge_bps: float
    missed_fill_penalty_bps: float
    total_cost_bps: float
    min_edge_bps: float
    net_expected_edge_bps: float
    exceeds_min_edge_threshold: bool
    profitable_after_costs: bool
    assumptions_version: str
    fallback_spread_applied: bool
    reason: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise CostModelError("ticker must be a non-empty string")
        object.__setattr__(self, "ticker", self.ticker.strip())
        object.__setattr__(self, "side", _validate_trade_side(self.side))
        object.__setattr__(self, "horizon", _validate_horizon(self.horizon).value)
        object.__setattr__(
            self,
            "order_style",
            _validate_order_style(self.order_style).value,
        )
        if not isinstance(self.assumptions_version, str) or not self.assumptions_version.strip():
            raise CostModelError("assumptions_version must be a non-empty string")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise CostModelError("reason must be a non-empty string when provided")
        if self.trace_id is not None and (
            not isinstance(self.trace_id, str) or not self.trace_id.strip()
        ):
            raise CostModelError("trace_id must be a non-empty string when provided")

        for field_info in fields(self):
            value = getattr(self, field_info.name)
            if isinstance(value, bool):
                continue
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise CostModelError(f"{field_info.name} must be finite")
                continue
            if field_info.name in {
                "ticker",
                "side",
                "horizon",
                "order_style",
                "assumptions_version",
                "reason",
                "trace_id",
            }:
                continue
            if isinstance(value, int):
                continue
            raise CostModelError(
                f"{field_info.name} contains non-JSON-safe value: {type(value).__name__}"
            )


def estimate_cost_from_expected_move(
    *,
    ticker: str,
    side: SignalSide,
    expected_gross_move_bps: float,
    horizon: str,
    midprice: float,
    spread: float | None = None,
    spread_bps: float | None = None,
    order_style: str = OrderStyle.MARKET.value,
    quantity: float = 1.0,
    is_crossed_or_locked: bool = False,
    config: CostModelConfig | None = None,
    trace_id: str | None = None,
) -> CostEstimate:
    """Estimate costs from an already mid-to-mid expected move in basis points."""
    resolved_config = config or CostModelConfig()
    resolved_side = _validate_trade_side(side)
    resolved_horizon = _validate_horizon(horizon)
    resolved_order_style = _validate_order_style(order_style)
    expected_move = _finite_float(
        expected_gross_move_bps,
        "expected_gross_move_bps",
    )
    resolved_midprice = _positive_finite_float(midprice, "midprice")
    resolved_quantity = _positive_finite_float(quantity, "quantity")

    if not isinstance(ticker, str) or not ticker.strip():
        raise CostModelError("ticker must be a non-empty string")
    if is_crossed_or_locked:
        raise CostModelError("crossed or locked books cannot be costed safely")

    resolved_spread_bps, fallback_applied, reason = _resolve_spread_bps(
        midprice=resolved_midprice,
        spread=spread,
        spread_bps=spread_bps,
        config=resolved_config,
    )

    if (
        resolved_order_style is OrderStyle.MARKET
        and resolved_spread_bps <= 0
    ):
        raise CostModelError("MARKET order requires positive spread_bps after fallback")

    spread_multiplier = (
        resolved_config.market_order_spread_multiplier
        if resolved_order_style is OrderStyle.MARKET
        else resolved_config.limit_order_spread_multiplier
    )
    spread_cost_bps = resolved_spread_bps * spread_multiplier
    estimated_slippage_bps = (
        resolved_config.round_trip_slippage_per_share
        / resolved_midprice
        * BPS_MULTIPLIER
    )
    size_penalty_bps = _size_penalty_bps(resolved_quantity, resolved_config)
    base_cost_bps = spread_cost_bps + estimated_slippage_bps + size_penalty_bps

    missed_fill_probability = 0.0
    if resolved_order_style is OrderStyle.LIMIT_AT_MID:
        missed_fill_probability = resolved_config.limit_order_missed_fill_probability_by_horizon[
            resolved_horizon.value
        ]

    pre_missed_fill_net_edge_bps = expected_move - base_cost_bps
    missed_fill_penalty_bps = missed_fill_probability * max(
        pre_missed_fill_net_edge_bps,
        0.0,
    )
    total_cost_bps = base_cost_bps + missed_fill_penalty_bps
    net_expected_edge_bps = expected_move - total_cost_bps
    exceeds_threshold = net_expected_edge_bps > resolved_config.min_edge_bps

    return CostEstimate(
        ticker=ticker.strip(),
        side=resolved_side,
        horizon=resolved_horizon.value,
        order_style=resolved_order_style.value,
        quantity=resolved_quantity,
        midprice=resolved_midprice,
        spread_bps=resolved_spread_bps,
        expected_gross_move_bps=expected_move,
        spread_cost_bps=spread_cost_bps,
        estimated_slippage_bps=estimated_slippage_bps,
        size_penalty_bps=size_penalty_bps,
        base_cost_bps=base_cost_bps,
        missed_fill_probability=missed_fill_probability,
        pre_missed_fill_net_edge_bps=pre_missed_fill_net_edge_bps,
        missed_fill_penalty_bps=missed_fill_penalty_bps,
        total_cost_bps=total_cost_bps,
        min_edge_bps=resolved_config.min_edge_bps,
        net_expected_edge_bps=net_expected_edge_bps,
        exceeds_min_edge_threshold=exceeds_threshold,
        profitable_after_costs=exceeds_threshold,
        assumptions_version=resolved_config.assumptions_version,
        fallback_spread_applied=fallback_applied,
        reason=reason,
        trace_id=trace_id,
    )


def estimate_cost_from_mid_prices(
    *,
    ticker: str,
    side: SignalSide,
    entry_midprice: float,
    exit_midprice: float,
    horizon: str,
    bid_price: float | None = None,
    ask_price: float | None = None,
    spread: float | None = None,
    spread_bps: float | None = None,
    order_style: str = OrderStyle.MARKET.value,
    quantity: float = 1.0,
    is_crossed_or_locked: bool = False,
    config: CostModelConfig | None = None,
    trace_id: str | None = None,
) -> CostEstimate:
    """Estimate costs from entry/exit midprices, not fill prices."""
    resolved_side = _validate_trade_side(side)
    entry = _positive_finite_float(entry_midprice, "entry_midprice")
    exit_ = _positive_finite_float(exit_midprice, "exit_midprice")
    derived_spread = spread
    if derived_spread is None and (bid_price is not None or ask_price is not None):
        bid = _positive_finite_float(bid_price, "bid_price")
        ask = _positive_finite_float(ask_price, "ask_price")
        derived_spread = ask - bid

    if resolved_side is SignalSide.BUY:
        expected_gross_move_bps = (exit_ / entry - 1.0) * BPS_MULTIPLIER
    else:
        expected_gross_move_bps = (entry / exit_ - 1.0) * BPS_MULTIPLIER

    return estimate_cost_from_expected_move(
        ticker=ticker,
        side=resolved_side,
        expected_gross_move_bps=expected_gross_move_bps,
        horizon=horizon,
        midprice=entry,
        spread=derived_spread,
        spread_bps=spread_bps,
        order_style=order_style,
        quantity=quantity,
        is_crossed_or_locked=is_crossed_or_locked,
        config=config,
        trace_id=trace_id,
    )


def exceeds_min_edge_threshold(estimate: CostEstimate) -> bool:
    """Return True only when net edge is strictly greater than min edge."""
    if not isinstance(estimate, CostEstimate):
        raise CostModelError("estimate must be a CostEstimate")
    return estimate.net_expected_edge_bps > estimate.min_edge_bps


def _resolve_spread_bps(
    *,
    midprice: float,
    spread: float | None,
    spread_bps: float | None,
    config: CostModelConfig,
) -> tuple[float, bool, str | None]:
    if spread_bps is not None:
        resolved_spread_bps = _finite_float(spread_bps, "spread_bps")
        if resolved_spread_bps < 0:
            raise CostModelError("spread_bps must be non-negative")
    elif spread is not None:
        resolved_spread = _finite_float(spread, "spread")
        if resolved_spread < 0:
            raise CostModelError("spread must be non-negative")
        resolved_spread_bps = resolved_spread / midprice * BPS_MULTIPLIER
    else:
        resolved_spread_bps = 0.0

    if resolved_spread_bps == 0:
        return (
            config.fallback_minimum_spread_bps,
            True,
            "fallback_minimum_spread_bps_applied",
        )
    return resolved_spread_bps, False, None


def _size_penalty_bps(quantity: float, config: CostModelConfig) -> float:
    if quantity <= config.size_penalty_free_quantity:
        return 0.0
    excess_quantity = quantity - config.size_penalty_free_quantity
    return (excess_quantity / 1000.0) * config.size_penalty_bps_per_1000_shares


def _validate_trade_side(side: SignalSide) -> SignalSide:
    try:
        resolved_side = SignalSide(side)
    except ValueError as exc:
        raise CostModelError("side must be SignalSide.BUY or SignalSide.SELL") from exc
    if resolved_side not in TRADE_ENTRY_SIDES:
        raise CostModelError("cost model supports only BUY and SELL trade entry sides")
    return resolved_side


def _validate_horizon(horizon: str) -> TradeHorizon:
    try:
        return TradeHorizon(horizon)
    except ValueError as exc:
        raise CostModelError("horizon must be one of: 1m, 5m, 15m") from exc


def _validate_order_style(order_style: str) -> OrderStyle:
    try:
        return OrderStyle(order_style)
    except ValueError as exc:
        raise CostModelError("order_style must be MARKET or LIMIT_AT_MID") from exc


def _finite_non_negative(value: Any, field_name: str) -> float:
    numeric_value = _finite_float(value, field_name)
    if numeric_value < 0:
        raise CostModelError(f"{field_name} must be non-negative")
    return numeric_value


def _positive_finite_float(value: Any, field_name: str) -> float:
    numeric_value = _finite_float(value, field_name)
    if numeric_value <= 0:
        raise CostModelError(f"{field_name} must be positive")
    return numeric_value


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise CostModelError(f"{field_name} must be numeric, not bool")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise CostModelError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric_value):
        raise CostModelError(f"{field_name} must be finite")
    return numeric_value
