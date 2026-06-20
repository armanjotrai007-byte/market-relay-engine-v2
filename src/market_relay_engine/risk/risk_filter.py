"""Deterministic Risk Filter V1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
from typing import Any

from market_relay_engine.common.config import load_yaml_config
from market_relay_engine.common.time import ensure_timezone_aware_utc, parse_utc_iso
from market_relay_engine.contracts.context import ContextFlag, ContextStateSnapshot
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.market_data.cost_model import CostEstimate
from market_relay_engine.risk.decisions import (
    REASON_APPROVED,
    REASON_SIGNAL_EXIT,
    REASON_SIGNAL_NO_ACTION,
    build_risk_decision,
)
from market_relay_engine.risk.rules import (
    check_confidence,
    check_context_risk,
    check_cost_estimate,
    check_daily_limits,
    check_latency,
    check_portfolio_placeholders,
    check_spread,
    check_staleness,
)


ENTRY_SIGNALS = {SignalSide.BUY, SignalSide.SELL}
NO_ACTION_SIGNALS = {SignalSide.HOLD, SignalSide.DO_NOTHING}
HIGH_RISK_LEVELS = {"HIGH", "CRITICAL"}
ELEVATED_RISK_LEVELS = {"ELEVATED", "MEDIUM"}
DEFAULT_REDUCE_SIZE_FACTOR_ON_ELEVATED_RISK = 0.5


@dataclass(frozen=True, kw_only=True)
class MarketRiskInput:
    """Market quality facts consumed by Risk Filter V1."""

    ticker: str
    spread_dollars: float | None
    spread_bps: float | None
    latency_ms: float | None
    market_data_time: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("ticker must be a non-empty string")
        object.__setattr__(self, "ticker", self.ticker.strip())
        object.__setattr__(
            self,
            "spread_dollars",
            _optional_non_negative_float(self.spread_dollars, "spread_dollars"),
        )
        object.__setattr__(
            self,
            "spread_bps",
            _optional_non_negative_float(self.spread_bps, "spread_bps"),
        )
        object.__setattr__(
            self,
            "latency_ms",
            _optional_non_negative_float(self.latency_ms, "latency_ms"),
        )
        if self.market_data_time is not None:
            object.__setattr__(
                self,
                "market_data_time",
                ensure_timezone_aware_utc(self.market_data_time),
            )


@dataclass(frozen=True, kw_only=True)
class ContextRiskInput:
    """Generic context-risk facts consumed by Risk Filter V1."""

    event_window_active: bool = False
    high_risk_context_active: bool = False
    elevated_risk_context_active: bool = False
    context_snapshot_id: str | None = None
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "event_window_active",
            "high_risk_context_active",
            "elevated_risk_context_active",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be bool")
        if self.context_snapshot_id is not None and (
            not isinstance(self.context_snapshot_id, str)
            or not self.context_snapshot_id.strip()
        ):
            raise ValueError("context_snapshot_id must be non-empty when provided")
        for reason in self.reasons:
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("context reasons must be non-empty strings")


@dataclass(frozen=True, kw_only=True)
class AccountRiskInput:
    """Placeholder account risk facts for Risk Filter V1."""

    daily_loss_dollars: float = 0.0
    consecutive_losses: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "daily_loss_dollars",
            _non_negative_float(self.daily_loss_dollars, "daily_loss_dollars"),
        )
        object.__setattr__(
            self,
            "consecutive_losses",
            _non_negative_int(self.consecutive_losses, "consecutive_losses"),
        )


@dataclass(frozen=True, kw_only=True)
class PortfolioRiskInput:
    """Placeholder portfolio risk facts for Risk Filter V1."""

    duplicate_or_conflicting_order: bool = False
    open_positions: int = 0
    symbol_position_exists: bool = False

    def __post_init__(self) -> None:
        for field_name in ("duplicate_or_conflicting_order", "symbol_position_exists"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be bool")
        object.__setattr__(
            self,
            "open_positions",
            _non_negative_int(self.open_positions, "open_positions"),
        )


@dataclass(frozen=True, kw_only=True)
class RiskFilterConfig:
    """Strict config values consumed by deterministic Risk Filter V1."""

    min_model_confidence: float
    confidence_requires_calibration: bool
    calibration_required_before_live: bool
    max_spread_dollars: float
    max_spread_bps: float
    max_latency_ms: float
    stale_market_data_seconds: float
    reject_if_expected_edge_below_cost: bool
    max_open_positions: int
    max_position_per_symbol: int
    max_daily_loss_dollars: float
    max_consecutive_losses: int
    block_during_eia_window: bool
    block_during_cpi_window: bool
    block_during_fomc_window: bool
    reduce_size_on_ai_elevated_risk: bool
    block_on_ai_high_risk: bool
    reduce_size_factor_on_elevated_risk: float = (
        DEFAULT_REDUCE_SIZE_FACTOR_ON_ELEVATED_RISK
    )

    def __post_init__(self) -> None:
        for field_name in (
            "min_model_confidence",
            "max_spread_dollars",
            "max_spread_bps",
            "max_latency_ms",
            "stale_market_data_seconds",
            "max_daily_loss_dollars",
            "reduce_size_factor_on_elevated_risk",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_float(getattr(self, field_name), field_name),
            )
        for field_name in (
            "max_open_positions",
            "max_position_per_symbol",
            "max_consecutive_losses",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(getattr(self, field_name), field_name),
            )
        for field_name in (
            "confidence_requires_calibration",
            "calibration_required_before_live",
            "reject_if_expected_edge_below_cost",
            "block_during_eia_window",
            "block_during_cpi_window",
            "block_during_fomc_window",
            "reduce_size_on_ai_elevated_risk",
            "block_on_ai_high_risk",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be bool")
        if self.reduce_size_factor_on_elevated_risk <= 0:
            raise ValueError("reduce_size_factor_on_elevated_risk must be > 0")
        if self.reduce_size_factor_on_elevated_risk > 1:
            raise ValueError("reduce_size_factor_on_elevated_risk must be <= 1.0")

    @property
    def event_blocking_enabled(self) -> bool:
        """Return true when any configured V1 event-window block is enabled."""
        return (
            self.block_during_eia_window
            or self.block_during_cpi_window
            or self.block_during_fomc_window
        )

    @classmethod
    def from_yaml(
        cls,
        path: str | Path = "config/risk_limits.yaml",
        *,
        base_dir: str | Path | None = None,
    ) -> "RiskFilterConfig":
        """Load Risk Filter V1 config from existing risk_limits.yaml."""
        return cls.from_mapping(load_yaml_config(path, base_dir=base_dir))

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "RiskFilterConfig":
        """Build strict Risk Filter V1 config from a YAML mapping."""
        if not isinstance(config, Mapping):
            raise ValueError("risk filter config must be a mapping")

        signal_thresholds = _required_mapping(config, "signal_thresholds")
        market_quality = _required_mapping(config, "market_quality")
        execution_quality = _required_mapping(config, "execution_quality")
        position_limits = _required_mapping(config, "position_limits")
        daily_limits = _required_mapping(config, "daily_limits")
        event_risk = _required_mapping(config, "event_risk")

        return cls(
            min_model_confidence=_required_value(
                signal_thresholds,
                "signal_thresholds.min_model_confidence",
            ),
            confidence_requires_calibration=_required_value(
                signal_thresholds,
                "signal_thresholds.confidence_requires_calibration",
            ),
            calibration_required_before_live=_required_value(
                signal_thresholds,
                "signal_thresholds.calibration_required_before_live",
            ),
            max_spread_dollars=_required_value(
                market_quality,
                "market_quality.max_spread_dollars",
            ),
            max_spread_bps=_required_value(
                market_quality,
                "market_quality.max_spread_bps",
            ),
            max_latency_ms=_required_value(
                market_quality,
                "market_quality.max_latency_ms",
            ),
            stale_market_data_seconds=_required_value(
                market_quality,
                "market_quality.stale_market_data_seconds",
            ),
            reject_if_expected_edge_below_cost=_required_value(
                execution_quality,
                "execution_quality.reject_if_expected_edge_below_cost",
            ),
            max_open_positions=_required_value(
                position_limits,
                "position_limits.max_open_positions",
            ),
            max_position_per_symbol=_required_value(
                position_limits,
                "position_limits.max_position_per_symbol",
            ),
            max_daily_loss_dollars=_required_value(
                daily_limits,
                "daily_limits.max_daily_loss_dollars",
            ),
            max_consecutive_losses=_required_value(
                daily_limits,
                "daily_limits.max_consecutive_losses",
            ),
            block_during_eia_window=_required_value(
                event_risk,
                "event_risk.block_during_eia_window",
            ),
            block_during_cpi_window=_required_value(
                event_risk,
                "event_risk.block_during_cpi_window",
            ),
            block_during_fomc_window=_required_value(
                event_risk,
                "event_risk.block_during_fomc_window",
            ),
            reduce_size_on_ai_elevated_risk=_required_value(
                event_risk,
                "event_risk.reduce_size_on_ai_elevated_risk",
            ),
            block_on_ai_high_risk=_required_value(
                event_risk,
                "event_risk.block_on_ai_high_risk",
            ),
        )


def evaluate_risk(
    *,
    signal: ModelSignal,
    market: MarketRiskInput,
    cost_estimate: CostEstimate | None = None,
    cost_estimate_id: str | None = None,
    context: ContextRiskInput | None = None,
    account: AccountRiskInput | None = None,
    portfolio: PortfolioRiskInput | None = None,
    evaluation_time: datetime,
    config: RiskFilterConfig | None = None,
) -> RiskDecision:
    """Evaluate one model signal with deterministic V1 risk rules."""
    resolved_config = config or RiskFilterConfig.from_yaml()
    resolved_context = context or ContextRiskInput()
    resolved_account = account or AccountRiskInput()
    resolved_portfolio = portfolio or PortfolioRiskInput()
    resolved_evaluation_time = ensure_timezone_aware_utc(evaluation_time)
    signal_side = SignalSide(signal.signal)
    resolved_cost_estimate_id = (
        cost_estimate_id
        if cost_estimate_id is not None
        else getattr(cost_estimate, "cost_estimate_id", None)
    )

    if signal_side in NO_ACTION_SIGNALS:
        return build_risk_decision(
            signal=signal,
            evaluation_time=resolved_evaluation_time,
            decision=RiskDecisionType.DO_NOTHING,
            approved=False,
            reason=REASON_SIGNAL_NO_ACTION,
            thresholds_used={"decision_rule": REASON_SIGNAL_NO_ACTION},
            cost_estimate_id=resolved_cost_estimate_id,
            context_snapshot_id=resolved_context.context_snapshot_id,
        )

    if signal_side is SignalSide.EXIT:
        return build_risk_decision(
            signal=signal,
            evaluation_time=resolved_evaluation_time,
            decision=RiskDecisionType.EXIT,
            approved=True,
            reason=REASON_SIGNAL_EXIT,
            thresholds_used={"decision_rule": REASON_SIGNAL_EXIT},
            cost_estimate_id=resolved_cost_estimate_id,
            context_snapshot_id=resolved_context.context_snapshot_id,
        )

    if signal_side in ENTRY_SIGNALS:
        cost_block = check_cost_estimate(
            signal=signal,
            cost_estimate=cost_estimate,
            reject_if_expected_edge_below_cost=(
                resolved_config.reject_if_expected_edge_below_cost
            ),
        )
        if cost_block is not None:
            return _block(
                signal=signal,
                evaluation_time=resolved_evaluation_time,
                reason=cost_block[0],
                thresholds_used=cost_block[1],
                cost_estimate_id=resolved_cost_estimate_id,
                context_snapshot_id=resolved_context.context_snapshot_id,
            )

    for block in (
        check_confidence(signal, resolved_config),
        check_spread(market, resolved_config),
        check_latency(market, resolved_config),
        check_staleness(
            market=market,
            evaluation_time=resolved_evaluation_time,
            config=resolved_config,
        ),
        check_daily_limits(resolved_account, resolved_config),
        check_portfolio_placeholders(resolved_portfolio, resolved_config),
    ):
        if block is not None:
            return _block(
                signal=signal,
                evaluation_time=resolved_evaluation_time,
                reason=block[0],
                thresholds_used=block[1],
                cost_estimate_id=resolved_cost_estimate_id,
                context_snapshot_id=resolved_context.context_snapshot_id,
            )

    context_result = check_context_risk(resolved_context, resolved_config)
    if context_result is not None:
        decision, approved, reason, thresholds_used, reduce_size_factor = context_result
        return build_risk_decision(
            signal=signal,
            evaluation_time=resolved_evaluation_time,
            decision=decision,
            approved=approved,
            reason=reason,
            thresholds_used=thresholds_used,
            cost_estimate_id=resolved_cost_estimate_id,
            context_snapshot_id=resolved_context.context_snapshot_id,
            reduce_size_factor=reduce_size_factor,
            extra_reasons=resolved_context.reasons,
        )

    return build_risk_decision(
        signal=signal,
        evaluation_time=resolved_evaluation_time,
        decision=RiskDecisionType.APPROVE,
        approved=True,
        reason=REASON_APPROVED,
        thresholds_used={"decision_rule": REASON_APPROVED},
        cost_estimate_id=resolved_cost_estimate_id,
        context_snapshot_id=resolved_context.context_snapshot_id,
    )


def context_risk_input_from_contracts(
    *,
    context_snapshot: ContextStateSnapshot | None = None,
    context_flags: Sequence[ContextFlag] = (),
    evaluation_time: datetime,
) -> ContextRiskInput:
    """Map PR3 context contracts into generic Risk Filter V1 booleans."""
    resolved_evaluation_time = ensure_timezone_aware_utc(evaluation_time)
    event_window_active = False
    high_risk_context_active = False
    elevated_risk_context_active = False
    context_snapshot_id: str | None = None
    reasons: list[str] = []

    if context_snapshot is not None:
        context_snapshot_id = context_snapshot.context_snapshot_id
        risk_level = _normalized_text(context_snapshot.risk_level)
        if risk_level in HIGH_RISK_LEVELS:
            high_risk_context_active = True
            reasons.append("context_snapshot_risk_level_high")
        elif risk_level in ELEVATED_RISK_LEVELS:
            elevated_risk_context_active = True
            reasons.append("context_snapshot_risk_level_elevated")
        if _context_snapshot_event_window_active(context_snapshot, resolved_evaluation_time):
            event_window_active = True
            reasons.append("context_snapshot_event_window_active")

    for flag in context_flags:
        if flag.valid_until is not None and flag.valid_until < resolved_evaluation_time:
            continue
        flag_type = flag.flag_type or ""
        normalized_flag_type = flag_type.lower()
        if "event_window" in normalized_flag_type:
            event_window_active = True
            reasons.append("context_flag_event_window_active")

        severity = _normalized_text(flag.severity)
        if severity in HIGH_RISK_LEVELS:
            high_risk_context_active = True
            reasons.append("context_flag_severity_high")
        elif severity in ELEVATED_RISK_LEVELS:
            elevated_risk_context_active = True
            reasons.append("context_flag_severity_elevated")

    return ContextRiskInput(
        event_window_active=event_window_active,
        high_risk_context_active=high_risk_context_active,
        elevated_risk_context_active=elevated_risk_context_active,
        context_snapshot_id=context_snapshot_id,
        reasons=_dedupe(reasons),
    )


def _context_snapshot_event_window_active(
    snapshot: ContextStateSnapshot,
    evaluation_time: datetime,
) -> bool:
    active_flag_ids = {
        flag_id.strip()
        for flag_id in snapshot.active_context_flag_ids
        if isinstance(flag_id, str) and flag_id.strip()
    }
    if not active_flag_ids:
        return False
    for entry in _context_snapshot_entries(snapshot):
        if entry.get("value") is not True or not _context_snapshot_entry_is_fresh(entry, evaluation_time):
            continue
        details = entry.get("details")
        if not isinstance(details, Mapping):
            continue
        flag_id = details.get("context_flag_id")
        flag_type = details.get("flag_type")
        if (
            isinstance(flag_id, str)
            and flag_id.strip() in active_flag_ids
            and isinstance(flag_type, str)
            and "event_window" in flag_type.lower()
        ):
            return True
    return False


def _context_snapshot_entries(snapshot: ContextStateSnapshot) -> list[Mapping[str, Any]]:
    summary = snapshot.context_summary
    entries: list[Mapping[str, Any]] = []
    global_entries = summary.get("global")
    if isinstance(global_entries, Mapping):
        entries.extend(entry for entry in global_entries.values() if isinstance(entry, Mapping))
    ticker_groups = summary.get("tickers")
    if isinstance(ticker_groups, Mapping):
        ticker_entries = ticker_groups.get(snapshot.ticker)
        if isinstance(ticker_entries, Mapping):
            entries.extend(entry for entry in ticker_entries.values() if isinstance(entry, Mapping))
    sector_groups = summary.get("sectors")
    if isinstance(sector_groups, Mapping) and snapshot.sector is not None:
        sector_entries = sector_groups.get(snapshot.sector)
        if isinstance(sector_entries, Mapping):
            entries.extend(entry for entry in sector_entries.values() if isinstance(entry, Mapping))
    return entries


def _context_snapshot_entry_is_fresh(entry: Mapping[str, Any], evaluation_time: datetime) -> bool:
    if entry.get("expired") is True:
        return False
    valid_until = entry.get("valid_until")
    if valid_until is None:
        return True
    if not isinstance(valid_until, str):
        return False
    try:
        return parse_utc_iso(valid_until) >= evaluation_time
    except (TypeError, ValueError):
        return False


def _block(
    *,
    signal: ModelSignal,
    evaluation_time: datetime,
    reason: str,
    thresholds_used: dict[str, Any],
    cost_estimate_id: str | None,
    context_snapshot_id: str | None,
) -> RiskDecision:
    return build_risk_decision(
        signal=signal,
        evaluation_time=evaluation_time,
        decision=RiskDecisionType.BLOCK,
        approved=False,
        reason=reason,
        thresholds_used=thresholds_used,
        cost_estimate_id=cost_estimate_id,
        context_snapshot_id=context_snapshot_id,
    )


def _required_mapping(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = _required_value(config, key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be a mapping")
    return value


def _required_value(config: Mapping[str, Any], path: str) -> Any:
    key = path.rsplit(".", maxsplit=1)[-1]
    if key not in config:
        raise ValueError(f"Missing risk filter config key: {path}")
    return config[key]


def _optional_non_negative_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _non_negative_float(value, field_name)


def _non_negative_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric, not bool")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(numeric_value):
        raise ValueError(f"{field_name} must be finite")
    if numeric_value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return numeric_value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not bool")
    try:
        integer_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if integer_value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if not isinstance(value, int) and str(integer_value) != str(value).strip():
        raise ValueError(f"{field_name} must be an integer")
    return integer_value


def _normalized_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().upper()


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
