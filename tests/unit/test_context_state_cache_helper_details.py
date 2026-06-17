from __future__ import annotations

from datetime import UTC, datetime

import pytest

from market_relay_engine.context.state_cache import (
    ContextStateCacheError,
    make_global_context_entry,
    make_sector_context_entry,
    make_ticker_context_entry,
)


BASE_TIME = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
HELPER_CASES = (
    (make_global_context_entry, {"name": "global_details"}),
    (make_ticker_context_entry, {"ticker": "AAPL", "name": "ticker_details"}),
    (make_sector_context_entry, {"sector": "TECH", "name": "sector_details"}),
)


@pytest.mark.parametrize(("factory", "scope_kwargs"), HELPER_CASES)
def test_helper_constructors_accept_none_empty_and_valid_details(
    factory: object,
    scope_kwargs: dict[str, object],
) -> None:
    none_entry = factory(  # type: ignore[operator]
        **scope_kwargs,
        value="ok",
        updated_at=BASE_TIME,
        details=None,
    )
    assert none_entry.details == {}

    empty_entry = factory(  # type: ignore[operator]
        **scope_kwargs,
        value="ok",
        updated_at=BASE_TIME,
        details={},
    )
    assert empty_entry.details == {}

    valid_details: dict[str, object] = {"nested": {"x": 1}}
    valid_entry = factory(  # type: ignore[operator]
        **scope_kwargs,
        value="ok",
        updated_at=BASE_TIME,
        details=valid_details,
    )
    assert valid_entry.details == {"nested": {"x": 1}}

    nested = valid_details["nested"]
    assert isinstance(nested, dict)
    nested["x"] = 2
    assert valid_entry.details == {"nested": {"x": 1}}


@pytest.mark.parametrize(("factory", "scope_kwargs"), HELPER_CASES)
@pytest.mark.parametrize("bad_details", [[], "", False, 0])
def test_helper_constructors_reject_invalid_falsy_details(
    factory: object,
    scope_kwargs: dict[str, object],
    bad_details: object,
) -> None:
    with pytest.raises(ContextStateCacheError):
        factory(  # type: ignore[operator]
            **scope_kwargs,
            value="ok",
            updated_at=BASE_TIME,
            details=bad_details,
        )
