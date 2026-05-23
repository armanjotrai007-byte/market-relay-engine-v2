"""Validate deterministic Risk Filter V1 without external services."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.contracts.model import ModelSignal, SignalSide  # noqa: E402
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType  # noqa: E402
from market_relay_engine.market_data.cost_model import (  # noqa: E402
    estimate_cost_from_expected_move,
)
from market_relay_engine.risk import (  # noqa: E402
    AccountRiskInput,
    ContextRiskInput,
    MarketRiskInput,
    evaluate_risk,
)


EVALUATION_TIME = datetime(2024, 1, 2, 15, 30, tzinfo=UTC)


def main() -> int:
    signal = _signal()
    market = _market()
    cost = _cost()

    results = [
        evaluate_risk(
            signal=signal,
            market=market,
            cost_estimate=cost,
            evaluation_time=EVALUATION_TIME,
        ),
        evaluate_risk(
            signal=signal,
            market=market,
            cost_estimate=None,
            evaluation_time=EVALUATION_TIME,
        ),
        evaluate_risk(
            signal=signal,
            market=_market(spread_bps=20.0),
            cost_estimate=cost,
            evaluation_time=EVALUATION_TIME,
        ),
        evaluate_risk(
            signal=signal,
            market=_market(latency_ms=2000.0),
            cost_estimate=cost,
            evaluation_time=EVALUATION_TIME,
        ),
        evaluate_risk(
            signal=signal,
            market=_market(market_data_time=EVALUATION_TIME - timedelta(seconds=10)),
            cost_estimate=cost,
            evaluation_time=EVALUATION_TIME,
        ),
        evaluate_risk(
            signal=signal,
            market=market,
            cost_estimate=cost,
            context=ContextRiskInput(elevated_risk_context_active=True),
            evaluation_time=EVALUATION_TIME,
        ),
        evaluate_risk(
            signal=signal,
            market=market,
            cost_estimate=cost,
            account=AccountRiskInput(daily_loss_dollars=50.0),
            evaluation_time=EVALUATION_TIME,
        ),
    ]

    expected = [
        RiskDecisionType.APPROVE,
        RiskDecisionType.BLOCK,
        RiskDecisionType.BLOCK,
        RiskDecisionType.BLOCK,
        RiskDecisionType.BLOCK,
        RiskDecisionType.REDUCE_SIZE,
        RiskDecisionType.BLOCK,
    ]
    for index, (decision, expected_decision) in enumerate(
        zip(results, expected, strict=True),
        start=1,
    ):
        assert isinstance(decision, RiskDecision), index
        assert decision.decision is expected_decision, (index, decision)
        assert decision.decision_time == EVALUATION_TIME, index
        assert decision.reasons, index
        assert decision.thresholds_used, index

    print("Risk filter check PASS")
    return 0


def _signal() -> ModelSignal:
    return ModelSignal(
        signal_time=EVALUATION_TIME,
        ticker="XOM",
        signal=SignalSide.BUY,
        confidence=0.72,
        raw_score=0.34,
        model_version="check_model_v1",
        calibration_version="check_calibration_v1",
        feature_version="check_features_v1",
        feature_snapshot_id="feature_snapshot_check",
        signal_id="signal_check",
        trace_id="trace_check_risk_filter",
    )


def _market(
    *,
    spread_bps: float | None = 1.0,
    latency_ms: float | None = 10.0,
    market_data_time: datetime | None = EVALUATION_TIME - timedelta(seconds=1),
) -> MarketRiskInput:
    return MarketRiskInput(
        ticker="XOM",
        spread_dollars=0.01,
        spread_bps=spread_bps,
        latency_ms=latency_ms,
        market_data_time=market_data_time,
    )


def _cost():
    return estimate_cost_from_expected_move(
        ticker="XOM",
        side=SignalSide.BUY,
        expected_gross_move_bps=20.0,
        horizon="1m",
        midprice=100.0,
        spread_bps=1.0,
        trace_id="trace_check_risk_filter",
    )


if __name__ == "__main__":
    raise SystemExit(main())
