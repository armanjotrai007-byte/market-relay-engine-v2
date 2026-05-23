"""Typed data contracts for Market Relay Engine V2."""

from market_relay_engine.contracts.base import DEFAULT_SCHEMA_VERSION
from market_relay_engine.contracts.context import (
    ContextAIEvent,
    ContextFlag,
    ContextIndicatorSnapshot,
    ContextStateSnapshot,
)
from market_relay_engine.contracts.execution import (
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from market_relay_engine.contracts.features import FeatureSnapshot
from market_relay_engine.contracts.ledger import LatencyMetric, TradeOutcome
from market_relay_engine.contracts.market import MarketRecord
from market_relay_engine.contracts.model import ModelSignal, SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.contracts.system import SystemHealthEvent

__all__ = [
    "DEFAULT_SCHEMA_VERSION",
    "ContextAIEvent",
    "ContextFlag",
    "ContextIndicatorSnapshot",
    "ContextStateSnapshot",
    "FeatureSnapshot",
    "FillEvent",
    "LatencyMetric",
    "MarketRecord",
    "ModelSignal",
    "OrderEvent",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "RiskDecision",
    "RiskDecisionType",
    "SignalSide",
    "SystemHealthEvent",
    "TradeOutcome",
]
