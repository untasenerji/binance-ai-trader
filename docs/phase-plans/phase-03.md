# Phase 03 Plan: Public Market Data

## Scope

- Credential-free REST reads for server time, exchange information, klines, mark/funding data, and best bid/ask.
- Routed public WebSocket URL construction, explicit 24-hour reconnect policy, and a public receive primitive.
- Freshness clock and local depth snapshot/diff sequence guard.

## Non-goals

- No authenticated route, listen key, account/position data, API key, signature, order, test order, or trading action.

## Exit Gate

- Mock contract tests cover Decimal parsing and no-auth requests.
- A real credential-free `/fapi/v1/time` smoke test passes.
- Stale, reconnect, and sequence-gap behavior is tested.
