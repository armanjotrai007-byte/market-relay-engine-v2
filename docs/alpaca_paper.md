# Alpaca Paper Client Wrapper

## Purpose

PR20 adds a small paper-only Alpaca Trading API wrapper. It accepts PR19
resolved order intents and can submit MARKET buy/sell orders to Alpaca paper
trading only when explicitly enabled.

PR20 does not support live trading, the live Alpaca endpoint, retries, bracket
orders, stop loss, take profit, limit orders, options, crypto, QuestDB writes,
model inference, AI calls, external collectors, async services, or new heavy
dependencies.

## Configuration

Local `.env` values stay on the server laptop only and must not be committed.
Codex and GitHub must never receive real Alpaca keys.

Required local variables for real account validation are:

```text
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

The only accepted base URL is exactly:

```text
https://paper-api.alpaca.markets
```

The wrapper rejects live URLs, lookalike paper hostnames, URL paths, and non-HTTPS
paper URLs. Importing the module and running normal validation does not require
real keys.

## Order Submission

`AlpacaPaperClient.submit_order(...)` accepts only resolved BUY/SELL intents.
Callers must pass PR18 `CLOSE_POSITION` intents through PR19
`resolve_close_position_intent(...)` before calling `submit_order(...)`.
The client rejects `CLOSE_POSITION` because Alpaca accepts broker buy/sell sides,
not local close-position semantics.

PR20 supports MARKET orders only. If a resolved intent carries an `order_type` or
`order_style` that is not MARKET, the client raises `AlpacaPaperError`.
`LIMIT_AT_MID` and other limit-order support are deferred. A future PR must add
limit order support or restrict the cost model/order flow to MARKET before real
paper execution.

Every order payload includes a deterministic `client_order_id` for idempotency.
The client prefers `intent.order_id` when present, otherwise uses
`intent.source_signal_id`, sanitizes it, and keeps it at 48 characters or less.
The payload quantity is always a safe string, never a raw Python float.

## Failure Handling

Local safety or configuration failures raise `AlpacaPaperError`. Broker and
network failures return `AlpacaPaperResponse(success=False, ...)`.

```python
try:
    response = client.submit_order(resolved_intent)
except AlpacaPaperError as exc:
    # local safety/config problem
    ...
else:
    if not response.success:
        # broker/network rejection
        ...
```

Alpaca may reject unsupported fractional short sells or other broker validation
failures with 403 or 422. PR20 returns `success=False` cleanly for those cases.
Future order sizing and position logic should decide whether a SELL is a
long-close or short-open and enforce broker constraints before submission.

## Validation

Default validation is offline and mocked:

```powershell
python scripts/check_alpaca_paper.py
```

It builds a fake account request and a mocked order submission path, makes no
network call, submits no paper order, and prints:

```text
Alpaca paper check PASS
```

Optional real validation checks account connectivity only:

```powershell
python scripts/check_alpaca_paper.py --required
```

The required mode loads real local `.env` values, validates the paper base URL,
calls `GET /v2/account`, prints no secrets, and never submits an order.

## Future OrderEvent Correlation

PR20 only returns `AlpacaPaperResponse`. The caller must combine the original
`OrderIntent` or `ResolvedOrderIntent` with `AlpacaPaperResponse` to build future
`OrderEvent` records.

The response `broker_order_id` should map to a future `order_events.broker_order_id`
field. The local `client_order_id` and source order/signal IDs should preserve
idempotency and correlation across local order state, Alpaca responses, and later
execution metrics.

## Next PR

PR21 should capture execution metrics and order result events after the wrapper
returns account/order responses.
