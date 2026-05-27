from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.market_data.cost_model import estimate_cost_from_expected_move
from market_relay_engine.risk import (
    MarketRiskInput,
    RiskDecisionLogResult,
    RiskFilterConfig,
    evaluate_risk_and_log,
    log_risk_decision,
)
from tests.fixtures.model_signals import make_model_signal
from tests.fixtures.risk_decisions import (
    make_approve_risk_decision,
    make_block_risk_decision,
    make_reduce_size_risk_decision,
    make_risk_decision,
)


EVALUATION_TIME = datetime(2024, 1, 2, 15, 30, tzinfo=UTC)


class FakeRiskDecisionWriter:
    def __init__(self) -> None:
        self.decisions: list[RiskDecision] = []
        self.kwargs: list[dict[str, Any]] = []

    def write_risk_decision(
        self,
        decision: RiskDecision,
        **kwargs: Any,
    ) -> object | None:
        self.decisions.append(decision)
        self.kwargs.append(kwargs)
        return {"ok": True}


class FailingRiskDecisionWriter:
    def write_risk_decision(
        self,
        decision: RiskDecision,
        **kwargs: Any,
    ) -> object | None:
        raise RuntimeError("ledger unavailable")


@pytest.mark.parametrize(
    "decision",
    [
        make_approve_risk_decision(index=1),
        make_block_risk_decision(index=2),
        make_reduce_size_risk_decision(index=3),
        make_risk_decision(
            decision=RiskDecisionType.EXIT,
            approved=True,
            reasons=["signal_exit"],
            index=4,
        ),
        make_risk_decision(
            decision=RiskDecisionType.DO_NOTHING,
            approved=False,
            reasons=["signal_no_action"],
            index=5,
        ),
    ],
)
def test_log_risk_decision_logs_every_decision_type(decision: RiskDecision) -> None:
    writer = FakeRiskDecisionWriter()

    result = log_risk_decision(decision, writer)

    assert result == RiskDecisionLogResult(
        decision=decision,
        attempted=True,
        success=True,
    )
    assert writer.decisions == [decision]
    assert writer.decisions[0] is decision


def test_log_risk_decision_passes_writer_kwargs_and_preserves_cost_id() -> None:
    writer = FakeRiskDecisionWriter()
    decision = make_approve_risk_decision(cost_estimate_id="cost_estimate_123")

    result = log_risk_decision(
        decision,
        writer,
        run_id="run_1",
        session_id="session_1",
    )

    assert result.success is True
    assert writer.decisions == [decision]
    assert writer.decisions[0].cost_estimate_id == "cost_estimate_123"
    assert writer.kwargs == [{"run_id": "run_1", "session_id": "session_1"}]


def test_evaluate_risk_and_log_returns_result_with_produced_decision() -> None:
    writer = FakeRiskDecisionWriter()

    result = evaluate_risk_and_log(
        signal=make_model_signal(),
        market=_market(),
        cost_estimate=_cost(),
        evaluation_time=EVALUATION_TIME,
        config=_config(),
        writer=writer,
    )

    assert isinstance(result, RiskDecisionLogResult)
    assert result.success is True
    assert result.decision.decision is RiskDecisionType.APPROVE
    assert writer.decisions == [result.decision]


def test_evaluate_risk_and_log_logs_cost_estimate_id_and_writer_kwargs() -> None:
    writer = FakeRiskDecisionWriter()

    result = evaluate_risk_and_log(
        signal=make_model_signal(),
        market=_market(),
        cost_estimate=_cost(),
        cost_estimate_id="cost_from_pr16",
        evaluation_time=EVALUATION_TIME,
        config=_config(),
        writer=writer,
        run_id="run_2",
        session_id="session_2",
    )

    assert result.decision.cost_estimate_id == "cost_from_pr16"
    assert writer.decisions == [result.decision]
    assert writer.decisions[0].cost_estimate_id == "cost_from_pr16"
    assert writer.kwargs == [{"run_id": "run_2", "session_id": "session_2"}]


def test_writer_failure_returns_failure_by_default() -> None:
    decision = make_block_risk_decision()

    result = log_risk_decision(decision, FailingRiskDecisionWriter())

    assert result.decision is decision
    assert result.attempted is True
    assert result.success is False
    assert result.error_message == "ledger unavailable"


def test_writer_failure_raises_when_requested() -> None:
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        log_risk_decision(
            make_block_risk_decision(),
            FailingRiskDecisionWriter(),
            raise_on_failure=True,
        )


def test_exit_decision_survives_logging_failure_without_exception() -> None:
    exit_decision = make_risk_decision(
        decision=RiskDecisionType.EXIT,
        approved=True,
        reasons=["signal_exit"],
    )

    result = log_risk_decision(exit_decision, FailingRiskDecisionWriter())

    assert result.success is False
    assert result.decision is exit_decision
    assert result.decision.decision is RiskDecisionType.EXIT


def test_writer_none_raises_clear_value_error() -> None:
    with pytest.raises(ValueError, match="No writer provided to log_risk_decision"):
        log_risk_decision(make_approve_risk_decision(), None)


def test_risk_logging_has_no_external_or_questdb_dependency() -> None:
    source = Path("src/market_relay_engine/risk/logging.py").read_text(encoding="utf-8")

    assert "market_relay_engine.questdb" not in source
    assert "requests" not in source
    assert "alpaca" not in source.lower()


def _market() -> MarketRiskInput:
    return MarketRiskInput(
        ticker="XOM",
        spread_dollars=0.01,
        spread_bps=1.0,
        latency_ms=10.0,
        market_data_time=EVALUATION_TIME - timedelta(seconds=1),
    )


def _cost():
    return estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=20.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=1.0,
    )


def _config() -> RiskFilterConfig:
    return RiskFilterConfig.from_mapping(
        {
            "signal_thresholds": {
                "min_model_confidence": 0.0,
                "confidence_requires_calibration": True,
                "calibration_required_before_live": True,
            },
            "market_quality": {
                "max_spread_dollars": 0.05,
                "max_spread_bps": 10,
                "max_latency_ms": 1000,
                "stale_market_data_seconds": 5,
            },
            "execution_quality": {"reject_if_expected_edge_below_cost": True},
            "position_limits": {
                "max_open_positions": 3,
                "max_position_per_symbol": 1,
            },
            "daily_limits": {
                "max_daily_loss_dollars": 50,
                "max_consecutive_losses": 3,
            },
            "event_risk": {
                "block_during_eia_window": True,
                "block_during_cpi_window": True,
                "block_during_fomc_window": True,
                "reduce_size_on_ai_elevated_risk": True,
                "block_on_ai_high_risk": True,
            },
        }
    )
