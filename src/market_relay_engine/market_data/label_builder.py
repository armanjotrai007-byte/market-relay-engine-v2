"""Cost-aware supervised training label builder."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, time, timedelta
from enum import Enum
import math
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_relay_engine.common.time import ensure_timezone_aware_utc
from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.market_data.cost_model import (
    CostModelConfig,
    CostModelError,
    OrderStyle,
    estimate_cost_from_mid_prices,
)


SUPPORTED_LABEL_HORIZONS = ("1m", "5m", "15m")
LABEL_ENTRY_SIDES = {SignalSide.BUY, SignalSide.SELL}


class LabelBuilderError(ValueError):
    """Raised when a supervised label cannot be built safely."""


class LabelHorizon(str, Enum):
    """Supported PR 10 label horizons."""

    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"


@dataclass(frozen=True, kw_only=True)
class LabelBuilderConfig:
    """Configuration for deterministic cost-aware label generation."""

    horizons: tuple[str, ...] = SUPPORTED_LABEL_HORIZONS
    label_version: str = "labels_v1"
    default_quantity: float = 1.0
    default_order_style: str = OrderStyle.MARKET.value
    min_forward_price_age_seconds: float = 0.0
    max_forward_price_tolerance_seconds: float = 5.0
    allow_missing_forward_price: bool = False
    market_timezone: str = "America/New_York"
    regular_market_open: str = "09:30"
    regular_market_close: str = "16:00"
    enforce_regular_market_hours: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.horizons, str) or not isinstance(self.horizons, tuple):
            raise LabelBuilderError("horizons must be a tuple of supported horizons")
        if not self.horizons:
            raise LabelBuilderError("horizons must not be empty")
        object.__setattr__(
            self,
            "horizons",
            tuple(normalize_horizon(horizon) for horizon in self.horizons),
        )

        if not isinstance(self.label_version, str) or not self.label_version.strip():
            raise LabelBuilderError("label_version must be a non-empty string")
        object.__setattr__(self, "label_version", self.label_version.strip())

        object.__setattr__(
            self,
            "default_quantity",
            _positive_finite_float(self.default_quantity, "default_quantity"),
        )
        object.__setattr__(
            self,
            "default_order_style",
            _validate_order_style(self.default_order_style),
        )
        object.__setattr__(
            self,
            "min_forward_price_age_seconds",
            _finite_non_negative(
                self.min_forward_price_age_seconds,
                "min_forward_price_age_seconds",
            ),
        )
        object.__setattr__(
            self,
            "max_forward_price_tolerance_seconds",
            _finite_non_negative(
                self.max_forward_price_tolerance_seconds,
                "max_forward_price_tolerance_seconds",
            ),
        )
        if not isinstance(self.allow_missing_forward_price, bool):
            raise LabelBuilderError("allow_missing_forward_price must be bool")
        if not isinstance(self.enforce_regular_market_hours, bool):
            raise LabelBuilderError("enforce_regular_market_hours must be bool")

        _market_zone(self)
        market_open = _parse_market_time(
            self.regular_market_open,
            "regular_market_open",
        )
        market_close = _parse_market_time(
            self.regular_market_close,
            "regular_market_close",
        )
        if market_open >= market_close:
            raise LabelBuilderError("regular_market_open must be before regular_market_close")


@dataclass(frozen=True, kw_only=True)
class ForwardPriceObservation:
    """Normalized future midprice observation used only for label generation."""

    event_time: datetime
    ticker: str
    midprice: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_time",
            ensure_timezone_aware_utc(self.event_time),
        )
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise LabelBuilderError("ticker must be a non-empty string")
        object.__setattr__(self, "ticker", self.ticker.strip())
        object.__setattr__(
            self,
            "midprice",
            _positive_finite_float(self.midprice, "midprice"),
        )


@dataclass(frozen=True, kw_only=True)
class LabelExample:
    """JSON-safe cost-aware label for future supervised training."""

    snapshot_time: datetime
    ticker: str
    horizon: str
    side: SignalSide
    entry_midprice: float
    forward_event_time: datetime
    forward_midprice: float
    expected_gross_move_bps: float
    net_expected_edge_bps: float
    total_cost_bps: float
    min_edge_bps: float
    profitable_after_costs: bool
    cost_assumptions_version: str
    label_version: str
    feature_snapshot_id: str
    feature_version: str
    trace_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "snapshot_time",
            ensure_timezone_aware_utc(self.snapshot_time),
        )
        object.__setattr__(
            self,
            "forward_event_time",
            ensure_timezone_aware_utc(self.forward_event_time),
        )
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise LabelBuilderError("ticker must be a non-empty string")
        object.__setattr__(self, "ticker", self.ticker.strip())
        object.__setattr__(self, "horizon", normalize_horizon(self.horizon))
        object.__setattr__(self, "side", _validate_label_side(self.side))
        object.__setattr__(
            self,
            "entry_midprice",
            _positive_finite_float(self.entry_midprice, "entry_midprice"),
        )
        object.__setattr__(
            self,
            "forward_midprice",
            _positive_finite_float(self.forward_midprice, "forward_midprice"),
        )

        for field_info in fields(self):
            value = getattr(self, field_info.name)
            if isinstance(value, bool):
                continue
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise LabelBuilderError(f"{field_info.name} must be finite")
                continue
            if field_info.name in {
                "snapshot_time",
                "forward_event_time",
                "ticker",
                "horizon",
                "side",
                "cost_assumptions_version",
                "label_version",
                "feature_snapshot_id",
                "feature_version",
                "trace_id",
                "reason",
            }:
                continue
            if isinstance(value, int):
                continue
            raise LabelBuilderError(
                f"{field_info.name} contains non-JSON-safe value: "
                f"{type(value).__name__}"
            )

        for field_name in (
            "cost_assumptions_version",
            "label_version",
            "feature_snapshot_id",
            "feature_version",
        ):
            _require_non_empty_string(getattr(self, field_name), field_name)
        _require_optional_non_empty_string(self.trace_id, "trace_id")
        _require_optional_non_empty_string(self.reason, "reason")


def normalize_horizon(horizon: str | LabelHorizon) -> str:
    """Return a supported horizon string or fail clearly."""
    try:
        return LabelHorizon(horizon).value
    except ValueError as exc:
        raise LabelBuilderError("horizon must be one of: 1m, 5m, 15m") from exc


def horizon_to_timedelta(horizon: str | LabelHorizon) -> timedelta:
    """Return the explicit timedelta for a supported label horizon."""
    normalized = normalize_horizon(horizon)
    if normalized == LabelHorizon.ONE_MINUTE.value:
        return timedelta(minutes=1)
    if normalized == LabelHorizon.FIVE_MINUTES.value:
        return timedelta(minutes=5)
    if normalized == LabelHorizon.FIFTEEN_MINUTES.value:
        return timedelta(minutes=15)
    raise LabelBuilderError("horizon must be one of: 1m, 5m, 15m")


def find_forward_price(
    snapshot_time: datetime,
    ticker: str,
    forward_prices: Sequence[ForwardPriceObservation],
    horizon: str | LabelHorizon,
    tolerance_seconds: float,
    config: LabelBuilderConfig | None = None,
) -> ForwardPriceObservation:
    """Return the earliest same-ticker forward midprice after the target horizon."""
    resolved_config = config or LabelBuilderConfig()
    resolved_snapshot_time = ensure_timezone_aware_utc(snapshot_time)
    resolved_ticker = _non_empty_string(ticker, "ticker")
    resolved_horizon = normalize_horizon(horizon)
    resolved_tolerance = _finite_non_negative(tolerance_seconds, "tolerance_seconds")

    _require_regular_market_time(
        resolved_snapshot_time,
        resolved_config,
        "snapshot_time",
    )
    target_time = resolved_snapshot_time + horizon_to_timedelta(resolved_horizon)
    _require_regular_market_time(target_time, resolved_config, "target_time")

    earliest_time = target_time + timedelta(
        seconds=resolved_config.min_forward_price_age_seconds
    )
    latest_time = target_time + timedelta(seconds=resolved_tolerance)
    latest_regular_time = _regular_market_close_utc(target_time, resolved_config)
    if latest_regular_time is not None and latest_regular_time < latest_time:
        latest_time = latest_regular_time

    candidates = [
        observation
        for observation in forward_prices
        if _is_matching_forward_observation(
            observation=observation,
            ticker=resolved_ticker,
            earliest_time=earliest_time,
            latest_time=latest_time,
            config=resolved_config,
        )
    ]
    if not candidates:
        raise LabelBuilderError(
            "No forward price found for "
            f"{resolved_ticker} horizon {resolved_horizon} "
            f"between {earliest_time.isoformat()} and {latest_time.isoformat()}"
        )
    return min(candidates, key=lambda observation: observation.event_time)


def build_label_for_snapshot(
    snapshot: FeatureSnapshot,
    forward_prices: Sequence[ForwardPriceObservation],
    side: SignalSide,
    horizon: str,
    config: LabelBuilderConfig | None = None,
    cost_config: CostModelConfig | None = None,
    order_style: str | None = None,
    quantity: float | None = None,
    trace_id: str | None = None,
) -> LabelExample:
    """Build one cost-aware label for one feature snapshot and side."""
    if not isinstance(snapshot, FeatureSnapshot):
        raise LabelBuilderError("snapshot must be a FeatureSnapshot")
    resolved_config = config or LabelBuilderConfig()
    resolved_side = _validate_label_side(side)
    resolved_horizon = normalize_horizon(horizon)
    resolved_order_style = _validate_order_style(
        order_style or resolved_config.default_order_style
    )
    resolved_quantity = _positive_finite_float(
        resolved_config.default_quantity if quantity is None else quantity,
        "quantity",
    )
    resolved_trace_id = trace_id if trace_id is not None else snapshot.trace_id
    _require_optional_non_empty_string(resolved_trace_id, "trace_id")

    entry_midprice = _feature_positive_float(snapshot, "midprice")
    forward_observation = find_forward_price(
        snapshot.snapshot_time,
        snapshot.ticker,
        forward_prices,
        resolved_horizon,
        resolved_config.max_forward_price_tolerance_seconds,
        config=resolved_config,
    )

    try:
        cost_estimate = estimate_cost_from_mid_prices(
            ticker=snapshot.ticker,
            side=resolved_side,
            entry_midprice=entry_midprice,
            exit_midprice=forward_observation.midprice,
            horizon=resolved_horizon,
            spread=_optional_feature_float(snapshot, "spread"),
            spread_bps=_optional_feature_float(snapshot, "spread_bps"),
            order_style=resolved_order_style,
            quantity=resolved_quantity,
            is_crossed_or_locked=_feature_bool(
                snapshot,
                "is_crossed_or_locked",
                default=False,
            ),
            config=cost_config,
            trace_id=resolved_trace_id,
        )
    except CostModelError as exc:
        raise LabelBuilderError(f"Cost model failed for label: {exc}") from exc

    return LabelExample(
        snapshot_time=snapshot.snapshot_time,
        ticker=snapshot.ticker,
        horizon=resolved_horizon,
        side=resolved_side,
        entry_midprice=entry_midprice,
        forward_event_time=forward_observation.event_time,
        forward_midprice=forward_observation.midprice,
        expected_gross_move_bps=cost_estimate.expected_gross_move_bps,
        net_expected_edge_bps=cost_estimate.net_expected_edge_bps,
        total_cost_bps=cost_estimate.total_cost_bps,
        min_edge_bps=cost_estimate.min_edge_bps,
        profitable_after_costs=cost_estimate.profitable_after_costs,
        cost_assumptions_version=cost_estimate.assumptions_version,
        label_version=resolved_config.label_version,
        feature_snapshot_id=snapshot.feature_snapshot_id,
        feature_version=snapshot.feature_version,
        trace_id=resolved_trace_id,
        reason=cost_estimate.reason,
    )


def build_labels_for_snapshots(
    snapshots: Sequence[FeatureSnapshot],
    forward_prices: Sequence[ForwardPriceObservation],
    sides: Sequence[SignalSide] = (SignalSide.BUY, SignalSide.SELL),
    config: LabelBuilderConfig | None = None,
    cost_config: CostModelConfig | None = None,
) -> list[LabelExample]:
    """Build cost-aware labels for every snapshot, configured horizon, and side."""
    resolved_config = config or LabelBuilderConfig()
    labels: list[LabelExample] = []
    for snapshot in snapshots:
        for horizon in resolved_config.horizons:
            for side in sides:
                try:
                    labels.append(
                        build_label_for_snapshot(
                            snapshot=snapshot,
                            forward_prices=forward_prices,
                            side=side,
                            horizon=horizon,
                            config=resolved_config,
                            cost_config=cost_config,
                        )
                    )
                except LabelBuilderError as exc:
                    if (
                        resolved_config.allow_missing_forward_price
                        and _is_skippable_label_error(exc)
                    ):
                        continue
                    raise
    return labels


def _is_matching_forward_observation(
    *,
    observation: Any,
    ticker: str,
    earliest_time: datetime,
    latest_time: datetime,
    config: LabelBuilderConfig,
) -> bool:
    if not isinstance(observation, ForwardPriceObservation):
        raise LabelBuilderError("forward_prices must contain ForwardPriceObservation")
    if observation.ticker != ticker:
        return False
    if observation.event_time < earliest_time or observation.event_time > latest_time:
        return False
    _require_regular_market_time(observation.event_time, config, "forward_event_time")
    return True


def _require_regular_market_time(
    value: datetime,
    config: LabelBuilderConfig,
    field_name: str,
) -> None:
    if not config.enforce_regular_market_hours:
        return
    local_value = ensure_timezone_aware_utc(value).astimezone(_market_zone(config))
    market_open = _parse_market_time(config.regular_market_open, "regular_market_open")
    market_close = _parse_market_time(
        config.regular_market_close,
        "regular_market_close",
    )
    local_time = local_value.time()
    if local_time < market_open or local_time > market_close:
        raise LabelBuilderError(
            f"{field_name} is outside regular market hours "
            f"{config.regular_market_open}-{config.regular_market_close} "
            f"{config.market_timezone}"
        )


def _regular_market_close_utc(
    target_time: datetime,
    config: LabelBuilderConfig,
) -> datetime | None:
    if not config.enforce_regular_market_hours:
        return None
    zone = _market_zone(config)
    local_target = ensure_timezone_aware_utc(target_time).astimezone(zone)
    close_time = _parse_market_time(config.regular_market_close, "regular_market_close")
    close_local = datetime.combine(local_target.date(), close_time, tzinfo=zone)
    return close_local.astimezone(ensure_timezone_aware_utc(target_time).tzinfo)


def _market_zone(config: LabelBuilderConfig) -> ZoneInfo:
    if not isinstance(config.market_timezone, str) or not config.market_timezone.strip():
        raise LabelBuilderError("market_timezone must be a non-empty string")
    try:
        return ZoneInfo(config.market_timezone)
    except ZoneInfoNotFoundError as exc:
        raise LabelBuilderError(f"invalid market_timezone: {config.market_timezone}") from exc


def _parse_market_time(value: str, field_name: str) -> time:
    if not isinstance(value, str) or not value.strip():
        raise LabelBuilderError(f"{field_name} must be HH:MM")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise LabelBuilderError(f"{field_name} must be HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise LabelBuilderError(f"{field_name} must be HH:MM") from exc
    if f"{hour:02d}:{minute:02d}" != value.strip():
        raise LabelBuilderError(f"{field_name} must be zero-padded HH:MM")
    try:
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise LabelBuilderError(f"{field_name} must be a valid HH:MM time") from exc


def _validate_label_side(side: SignalSide) -> SignalSide:
    try:
        resolved_side = SignalSide(side)
    except ValueError as exc:
        raise LabelBuilderError("side must be SignalSide.BUY or SignalSide.SELL") from exc
    if resolved_side not in LABEL_ENTRY_SIDES:
        raise LabelBuilderError("label builder supports only BUY and SELL sides")
    return resolved_side


def _validate_order_style(order_style: str) -> str:
    try:
        return OrderStyle(order_style).value
    except ValueError as exc:
        raise LabelBuilderError("order_style must be MARKET or LIMIT_AT_MID") from exc


def _feature_positive_float(snapshot: FeatureSnapshot, feature_name: str) -> float:
    return _positive_finite_float(
        snapshot.features.get(feature_name),
        f"snapshot.features.{feature_name}",
    )


def _optional_feature_float(
    snapshot: FeatureSnapshot,
    feature_name: str,
) -> float | None:
    value = snapshot.features.get(feature_name)
    if value is None:
        return None
    return _finite_float(value, f"snapshot.features.{feature_name}")


def _feature_bool(
    snapshot: FeatureSnapshot,
    feature_name: str,
    *,
    default: bool,
) -> bool:
    value = snapshot.features.get(feature_name, default)
    if not isinstance(value, bool):
        raise LabelBuilderError(f"snapshot.features.{feature_name} must be bool")
    return value


def _is_skippable_label_error(exc: LabelBuilderError) -> bool:
    text = str(exc).lower()
    return "no forward price" in text or "regular market hours" in text


def _finite_non_negative(value: Any, field_name: str) -> float:
    numeric_value = _finite_float(value, field_name)
    if numeric_value < 0:
        raise LabelBuilderError(f"{field_name} must be non-negative")
    return numeric_value


def _positive_finite_float(value: Any, field_name: str) -> float:
    numeric_value = _finite_float(value, field_name)
    if numeric_value <= 0:
        raise LabelBuilderError(f"{field_name} must be positive")
    return numeric_value


def _finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise LabelBuilderError(f"{field_name} must be numeric, not bool")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise LabelBuilderError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric_value):
        raise LabelBuilderError(f"{field_name} must be finite")
    return numeric_value


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LabelBuilderError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_non_empty_string(value: str, field_name: str) -> None:
    _non_empty_string(value, field_name)


def _require_optional_non_empty_string(value: str | None, field_name: str) -> None:
    if value is not None:
        _require_non_empty_string(value, field_name)
