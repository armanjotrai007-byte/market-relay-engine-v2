from market_relay_engine.common.serialization import from_json_string, to_json_dict, to_json_string
from market_relay_engine.contracts.market import MarketRecord
from tests.fixtures.market_records import (
    build_market_record_examples,
    make_defense_market_record,
    make_market_quote_record,
    make_market_trade_record,
    make_oil_market_record,
)


def test_market_record_fixtures_instantiate_contracts() -> None:
    examples = build_market_record_examples()

    assert examples
    assert all(isinstance(example, MarketRecord) for example in examples)
    assert {"XOM", "CVX", "LMT", "RTX"}.issubset({example.ticker for example in examples})


def test_market_trade_and_quote_records_use_generic_contract_fields() -> None:
    trade = make_market_trade_record()
    quote = make_market_quote_record()
    oil = make_oil_market_record()
    defense = make_defense_market_record()

    assert trade.record_type == "trade"
    assert trade.price is not None
    assert trade.size is not None
    assert quote.record_type == "quote"
    assert quote.spread == round(quote.ask_price - quote.bid_price, 4)
    assert oil.ticker == "XOM"
    assert defense.ticker == "LMT"


def test_market_record_fixtures_serialize_to_json_safe_values() -> None:
    parsed = from_json_string(to_json_string(make_market_quote_record()))
    json_dict = to_json_dict(make_market_quote_record())

    assert parsed == json_dict
    assert parsed["event_time"].endswith("Z")
    assert parsed["source_event_time"].endswith("Z")
    assert parsed["local_receive_time"].endswith("Z")
