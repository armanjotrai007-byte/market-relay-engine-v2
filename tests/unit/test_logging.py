from market_relay_engine.common.ids import new_run_id, new_session_id, new_trace_id
from market_relay_engine.common.logging import build_log_context, get_logger


def test_build_log_context_returns_plain_dictionary() -> None:
    run_id = new_run_id()
    session_id = new_session_id()
    trace_id = new_trace_id()

    context = build_log_context(
        run_id=run_id,
        session_id=session_id,
        trace_id=trace_id,
        component="contracts",
    )

    assert context == {
        "run_id": run_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "component": "contracts",
    }


def test_get_logger_returns_standard_logger_without_duplicate_handlers() -> None:
    logger = get_logger("market_relay_engine.tests.logging")
    second = get_logger("market_relay_engine.tests.logging")

    assert logger is second
    assert len(logger.handlers) == 1
