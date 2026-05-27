"""Validate risk decision logging without external services."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from market_relay_engine.contracts.risk import RiskDecision  # noqa: E402
from market_relay_engine.risk.logging import log_risk_decision  # noqa: E402
from tests.fixtures.risk_decisions import (  # noqa: E402
    make_approve_risk_decision,
    make_block_risk_decision,
    make_reduce_size_risk_decision,
)


class InMemoryRiskDecisionWriter:
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
        return None


class FailingRiskDecisionWriter:
    def write_risk_decision(
        self,
        decision: RiskDecision,
        **kwargs: Any,
    ) -> object | None:
        raise RuntimeError("fake ledger failure")


def main() -> int:
    decisions = [
        make_approve_risk_decision(index=1),
        make_block_risk_decision(index=2),
        make_reduce_size_risk_decision(index=3),
    ]
    writer = InMemoryRiskDecisionWriter()

    for decision in decisions:
        result = log_risk_decision(
            decision,
            writer,
            run_id="check_run",
            session_id="check_session",
        )
        assert result.success is True
        assert result.decision is decision

    assert writer.decisions == decisions
    assert writer.kwargs == [
        {"run_id": "check_run", "session_id": "check_session"},
        {"run_id": "check_run", "session_id": "check_session"},
        {"run_id": "check_run", "session_id": "check_session"},
    ]

    failure_result = log_risk_decision(decisions[0], FailingRiskDecisionWriter())
    assert failure_result.success is False
    assert failure_result.error_message == "fake ledger failure"

    try:
        log_risk_decision(
            decisions[0],
            FailingRiskDecisionWriter(),
            raise_on_failure=True,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("strict logging failure should raise")

    print("Risk logging check PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
