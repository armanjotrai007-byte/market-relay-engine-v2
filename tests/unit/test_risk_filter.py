from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market_relay_engine.contracts.context import ContextFlag, ContextStateSnapshot
from market_relay_engine.contracts.model import SignalSide
from market_relay_engine.contracts.risk import RiskDecision, RiskDecisionType
from market_relay_engine.market_data.cost_model import estimate_cost_from_expected_move
from market_relay_engine.questdb.writer import risk_decision_to_row
from market_relay_engine.risk import (
    AccountRiskInput,
    ContextRiskInput,
    MarketRiskInput,
    PortfolioRiskInput,
    RiskFilterConfig,
    build_risk_decision,
    context_risk_input_from_contracts,
    effective_size_factor,
    evaluate_risk,
    is_entry_allowed,
)
from tests.fixtures.model_signals import make_model_signal


EVALUATION_TIME = datetime(2024, 1, 2, 15, 30, tzinfo=UTC)


def test_existing_risk_limits_yaml_loads_successfully() -> None:
    config = RiskFilterConfig.from_yaml()

    assert config.max_spread_bps == 10.0
    assert config.reject_if_expected_edge_below_cost is True
    assert config.event_blocking_enabled is True


def test_config_validation_rejects_missing_wrong_type_and_negative_values() -> None:
    missing = _config_mapping()
    del missing['daily_limits']
    with pytest.raises(ValueError, match='daily_limits'):
        RiskFilterConfig.from_mapping(missing)

    wrong_type = _config_mapping()
    wrong_type['signal_thresholds']['min_model_confidence'] = 'bad'  # type: ignore[index]
    with pytest.raises(ValueError, match='min_model_confidence'):
        RiskFilterConfig.from_mapping(wrong_type)

    negative = _config_mapping()
    negative['market_quality']['max_latency_ms'] = -1  # type: ignore[index]
    with pytest.raises(ValueError, match='max_latency_ms'):
        RiskFilterConfig.from_mapping(negative)


def test_clean_buy_and_sell_approve() -> None:
    assert _evaluate().decision is RiskDecisionType.APPROVE
    assert _evaluate(signal=make_model_signal(signal=SignalSide.SELL)).decision is RiskDecisionType.APPROVE


@pytest.mark.parametrize('side', [SignalSide.HOLD, SignalSide.DO_NOTHING])
def test_no_action_signals_do_not_require_cost(side: SignalSide) -> None:
    decision = _evaluate(signal=make_model_signal(signal=side), cost_estimate=None)

    assert decision.decision is RiskDecisionType.DO_NOTHING
    assert decision.approved is False
    assert decision.reasons == ['signal_no_action']
    assert decision.thresholds_used == {'decision_rule': 'signal_no_action'}


def test_exit_short_circuits_bad_inputs() -> None:
    decision = _evaluate(
        signal=make_model_signal(signal=SignalSide.EXIT),
        market=_market(spread_bps=99.0, market_data_time=EVALUATION_TIME - timedelta(minutes=5)),
        cost_estimate=None,
        account=AccountRiskInput(daily_loss_dollars=999.0),
        context=ContextRiskInput(high_risk_context_active=True),
    )

    assert decision.decision is RiskDecisionType.EXIT
    assert decision.approved is True
    assert decision.reasons == ['signal_exit']


def test_cost_estimate_enforcement_for_entries_and_id_preservation() -> None:
    assert _evaluate(cost_estimate=None).reasons == ['missing_cost_estimate']
    assert _evaluate(cost_estimate=_cost(expected_gross_move_bps=1.0)).reasons == [
        'cost_not_profitable_after_costs'
    ]
    assert _evaluate(cost_estimate=_cost(ticker='LMT')).reasons == ['cost_estimate_ticker_mismatch']

    estimate = _cost()
    object.__setattr__(estimate, 'cost_estimate_id', 'cost_from_estimate')
    assert _evaluate(cost_estimate=estimate).cost_estimate_id == 'cost_from_estimate'
    assert _evaluate(cost_estimate_id='cost_explicit').cost_estimate_id == 'cost_explicit'


def test_confidence_market_account_portfolio_and_context_rules() -> None:
    assert _evaluate(signal=make_model_signal(confidence=0.49), config=_config(min_model_confidence=0.5)).reasons == [
        'confidence_too_low'
    ]
    assert _evaluate(market=_market(spread_dollars=None, spread_bps=None)).reasons == ['missing_spread']
    assert _evaluate(market=_market(spread_dollars=0.06)).reasons == ['spread_dollars_too_wide']
    assert _evaluate(market=_market(spread_bps=14.2)).reasons == ['spread_bps_too_wide']
    assert _evaluate(market=_market(latency_ms=None)).reasons == ['missing_latency_ms']
    assert _evaluate(market=_market(latency_ms=1001.0)).reasons == ['latency_too_high']
    assert _evaluate(market=_market(market_data_time=None)).reasons == ['missing_market_data_time']
    assert _evaluate(market=_market(market_data_time=EVALUATION_TIME - timedelta(seconds=10))).reasons == [
        'stale_market_data'
    ]
    assert _evaluate(account=AccountRiskInput(daily_loss_dollars=50.0)).reasons == ['daily_loss_limit_hit']
    assert _evaluate(account=AccountRiskInput(consecutive_losses=3)).reasons == ['consecutive_loss_limit_hit']
    assert _evaluate(portfolio=PortfolioRiskInput(duplicate_or_conflicting_order=True)).reasons == [
        'duplicate_or_conflicting_order'
    ]
    assert _evaluate(portfolio=PortfolioRiskInput(open_positions=3)).reasons == ['max_open_positions_hit']
    assert _evaluate(portfolio=PortfolioRiskInput(symbol_position_exists=True)).reasons == [
        'max_position_per_symbol_hit'
    ]


def test_context_priority_and_reduce_size_contract() -> None:
    event = _evaluate(context=ContextRiskInput(event_window_active=True, elevated_risk_context_active=True))
    high = _evaluate(context=ContextRiskInput(high_risk_context_active=True, elevated_risk_context_active=True))
    reduced = _evaluate(context=ContextRiskInput(elevated_risk_context_active=True))

    assert event.reasons == ['event_window_active']
    assert high.reasons == ['high_context_risk']
    assert reduced.decision is RiskDecisionType.REDUCE_SIZE
    assert reduced.approved is True
    assert reduced.reduce_size_factor == 0.5
    assert is_entry_allowed(reduced) is True
    assert effective_size_factor(reduced) == 0.5
    assert effective_size_factor(_evaluate()) == 1.0
    assert effective_size_factor(_evaluate(cost_estimate=None)) == 0.0


@pytest.mark.parametrize('factor', [None, 0.0, -0.1, 1.5])
def test_reduce_size_factor_validation_rejects_invalid_values(factor: float | None) -> None:
    with pytest.raises(ValueError, match='reduce_size_factor'):
        build_risk_decision(
            signal=make_model_signal(),
            evaluation_time=EVALUATION_TIME,
            decision=RiskDecisionType.REDUCE_SIZE,
            approved=True,
            reason='elevated_context_risk',
            thresholds_used={'rule': 'elevated_context_risk'},
            reduce_size_factor=factor,
        )

    with pytest.raises(ValueError, match='only valid for REDUCE_SIZE'):
        build_risk_decision(
            signal=make_model_signal(),
            evaluation_time=EVALUATION_TIME,
            decision=RiskDecisionType.APPROVE,
            approved=True,
            reason='approved',
            thresholds_used={'decision_rule': 'approved'},
            reduce_size_factor=0.5,
        )


def test_evaluation_time_staleness_utc_and_no_utc_now(monkeypatch: pytest.MonkeyPatch) -> None:
    import market_relay_engine.common.time as time_module

    def fail_utc_now() -> datetime:
        raise AssertionError('utc_now must not be called by evaluate_risk')

    monkeypatch.setattr(time_module, 'utc_now', fail_utc_now)
    historical_time = datetime(2024, 2, 1, 15, 30, tzinfo=UTC)

    fresh = _evaluate(evaluation_time=historical_time, market=_market(market_data_time=historical_time - timedelta(seconds=1)))
    stale = _evaluate(evaluation_time=historical_time, market=_market(market_data_time=historical_time - timedelta(seconds=10)))

    assert fresh.decision_time == historical_time
    assert fresh.decision is RiskDecisionType.APPROVE
    assert stale.reasons == ['stale_market_data']
    assert fresh.decision_time.utcoffset() == timedelta(0)


def test_context_adapter_maps_snapshot_flags_expiration_and_neutral_state() -> None:
    snapshot = ContextStateSnapshot(
        snapshot_time=EVALUATION_TIME,
        ticker='XOM',
        context_snapshot_id='context_snapshot_123',
        risk_level='ELEVATED',
    )
    context = context_risk_input_from_contracts(
        context_snapshot=snapshot,
        context_flags=[
            _flag(severity='HIGH'),
            _flag(severity='MEDIUM'),
            _flag(severity='HIGH', valid_until=EVALUATION_TIME - timedelta(seconds=1)),
            _flag(severity='normal', flag_type='macro_EVENT_WINDOW'),
        ],
        evaluation_time=EVALUATION_TIME,
    )

    assert context.context_snapshot_id == 'context_snapshot_123'
    assert context.high_risk_context_active is True
    assert context.elevated_risk_context_active is True
    assert context.event_window_active is True
    assert 'context_snapshot_risk_level_elevated' in context.reasons
    assert context_risk_input_from_contracts(evaluation_time=EVALUATION_TIME) == ContextRiskInput()


def test_decision_output_fields_thresholds_and_questdb_cost_id_fallback() -> None:
    signal = make_model_signal(trace_id='trace_pr16')
    decision = _evaluate(signal=signal, context=ContextRiskInput(context_snapshot_id='context_snapshot_1'))
    spread = _evaluate(market=_market(spread_bps=14.2))
    low_confidence = _evaluate(signal=make_model_signal(confidence=0.49), config=_config(min_model_confidence=0.5))
    cost_failure = _evaluate(cost_estimate=_cost(expected_gross_move_bps=1.0))

    assert isinstance(decision, RiskDecision)
    assert decision.context_snapshot_id == 'context_snapshot_1'
    assert decision.trace_id == 'trace_pr16'
    assert decision.thresholds_used == {'decision_rule': 'approved'}
    assert set(low_confidence.thresholds_used) == {'rule', 'actual_confidence', 'min_model_confidence'}
    assert spread.thresholds_used == {
        'rule': 'spread_bps_too_wide',
        'actual_spread_bps': 14.2,
        'max_spread_bps': 10.0,
    }
    assert set(cost_failure.thresholds_used) == {
        'rule',
        'profitable_after_costs',
        'net_expected_edge_bps',
        'min_edge_bps',
    }

    decision_with_cost = _evaluate(cost_estimate_id='cost_from_decision')
    assert risk_decision_to_row(decision_with_cost)['cost_estimate_id'] == 'cost_from_decision'
    assert risk_decision_to_row(decision_with_cost, cost_estimate_id='explicit_cost')['cost_estimate_id'] == 'explicit_cost'


def _evaluate(
    *,
    signal=None,
    market: MarketRiskInput | None = None,
    cost_estimate: object | None = None,
    cost_estimate_id: str | None = None,
    context: ContextRiskInput | None = None,
    account: AccountRiskInput | None = None,
    portfolio: PortfolioRiskInput | None = None,
    evaluation_time: datetime = EVALUATION_TIME,
    config: RiskFilterConfig | None = None,
) -> RiskDecision:
    resolved_signal = signal or make_model_signal()
    resolved_cost = _cost(ticker=resolved_signal.ticker, side=resolved_signal.signal) if cost_estimate is None else cost_estimate
    if cost_estimate is None and cost_estimate_id is None:
        resolved_cost = _cost(ticker=resolved_signal.ticker, side=resolved_signal.signal)
    return evaluate_risk(
        signal=resolved_signal,
        market=market or _market(ticker=resolved_signal.ticker),
        cost_estimate=resolved_cost,
        cost_estimate_id=cost_estimate_id,
        context=context,
        account=account,
        portfolio=portfolio,
        evaluation_time=evaluation_time,
        config=config or _config(),
    )


def _market(
    *,
    ticker: str = 'XOM',
    spread_dollars: float | None = 0.01,
    spread_bps: float | None = 1.0,
    latency_ms: float | None = 10.0,
    market_data_time: datetime | None = EVALUATION_TIME - timedelta(seconds=1),
) -> MarketRiskInput:
    return MarketRiskInput(
        ticker=ticker,
        spread_dollars=spread_dollars,
        spread_bps=spread_bps,
        latency_ms=latency_ms,
        market_data_time=market_data_time,
    )


def _cost(
    *,
    ticker: str = 'XOM',
    side: SignalSide = SignalSide.BUY,
    expected_gross_move_bps: float = 20.0,
):
    return estimate_cost_from_expected_move(
        ticker=ticker,
        side=side,
        expected_gross_move_bps=expected_gross_move_bps,
        horizon='1m',
        midprice=100.0,
        spread_bps=1.0,
    )


def _flag(
    *,
    severity: str,
    flag_type: str = 'context_risk',
    valid_until: datetime | None = EVALUATION_TIME + timedelta(minutes=5),
) -> ContextFlag:
    return ContextFlag(
        event_time=EVALUATION_TIME,
        source='fixture_context',
        flag_type=flag_type,
        severity=severity,
        ticker='XOM',
        valid_until=valid_until,
    )


def _config(**overrides: object) -> RiskFilterConfig:
    return RiskFilterConfig.from_mapping(_config_mapping(**overrides))


def _config_mapping(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        'signal_thresholds': {
            'min_model_confidence': 0.0,
            'confidence_requires_calibration': True,
            'calibration_required_before_live': True,
        },
        'market_quality': {
            'max_spread_dollars': 0.05,
            'max_spread_bps': 10,
            'max_latency_ms': 1000,
            'stale_market_data_seconds': 5,
        },
        'execution_quality': {'reject_if_expected_edge_below_cost': True},
        'position_limits': {'max_open_positions': 3, 'max_position_per_symbol': 1},
        'daily_limits': {'max_daily_loss_dollars': 50, 'max_consecutive_losses': 3},
        'event_risk': {
            'block_during_eia_window': True,
            'block_during_cpi_window': True,
            'block_during_fomc_window': True,
            'reduce_size_on_ai_elevated_risk': True,
            'block_on_ai_high_risk': True,
        },
    }
    for key, value in overrides.items():
        for section in config.values():
            if isinstance(section, dict) and key in section:
                section[key] = value
                break
        else:
            config[key] = value
    return config
