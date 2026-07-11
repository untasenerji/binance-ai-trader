# Phase 06 Plan: Ladder and Risk Planning

## Scope

- Equal-percent, ATR, technical-level, order-book liquidity, and time-sliced ladder blueprints.
- Decimal quantity solver using entry/exit fees, slippage, funding buffer, stop distance, filters, and risk budget.
- Full-position server-side stop representation and refreshed reduce-only TP allocation for confirmed partial fills.

## Non-goals

- No order submission, authenticated endpoint, market entry, leverage change, or server-side state mutation.

## Exit Gate

- All-fill projected loss is no greater than the risk budget.
- Filter/min-notional failure is a skip, never a quantity/risk increase.
- Exit quantities never exceed confirmed exchange position quantity.
