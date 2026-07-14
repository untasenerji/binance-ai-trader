# Phase 06 Ladder and Risk Validation Report

**Date:** 2026-07-11
**Scope:** Decimal price ladder, quantity, risk, stop, and TP planning only.

## Delivered

- Explicit equal-percent, ATR, technical-level, liquidity-weighted, and time-sliced ladder blueprints.
- Liquidity ladder remains disabled until shadow-data approval.
- Decimal risk-per-unit projection including stop distance, entry/exit fees, slippage, and funding buffer.
- Filter-aware quantity solver with `SKIP_TRADE`-style reasons and no automatic risk increase.
- Full-position close-position stop model and refreshed partial-fill TP allocation.

## Validation

| Check | Result | Evidence |
|---|---|---|
| Ladder variants | PASS | Five variants are constructed; order-book variant rejects use until explicitly enabled. |
| All-fill risk | PASS | Planned total loss remains no greater than the supplied budget. |
| Minimum notional | PASS | Restrictive filter returns `MIN_NOTIONAL_OR_FILTER_FAILURE` with no stages. |
| Long/short | PASS | Price-loss projection is symmetric with zero costs; directional stop validation is separate. |
| Rounding | PASS | Directional price rounding uses long entry-up/stop-down/TP-down and short entry-down/stop-up/TP-up before risk recheck. |
| Partial fill exits | PASS | Stop uses `closePosition`; reduce-only TP legs total exactly the confirmed position and never exceed it. |
| Backend suite | PASS | pytest: 39 passed, 1 live-public test deliberately deselected. |

## Independent Audit Remediation (2026-07-12)

- `PlanningContext` and `RiskEnvelope` require verified equity, leverage, brackets, reserve, exposures, loss limits, position counts, margin mode, and stop capability; missing facts skip or halt.
- Worst-case entry/stop slippage is tick-rounded conservatively and all-fill loss, notional, and required margin are rechecked after filters.
- Unscheduled blueprints that collapse to the same rounded entry tick fail closed with `ROUNDED_STAGE_PRICE_COLLISION`; time-sliced stages retain their schedule identity.
- Historical 84.06% was combined total coverage, not branch coverage. Current second-audit PostgreSQL-inclusive validation: 200 passed, 1 public-live test deselected, 83.01% total coverage, and 65.49% true branch coverage (`702/1072`).

## Safety Confirmation

- This phase produces in-memory planning values only. It has no exchange, API key, order, or leverage path.
- D-012 and D-026 remain authoritative: unavailable minimums cannot cause automatic risk, capital, leverage, or stage expansion.

## Outcome

Phase 6 exit gate is satisfied. Phase 7 is authorized by the user's 2026-07-10 instruction and implements a fully local exchange simulator with controlled failure injection.

## Git Checkpoint

- Phase 4-8 implementation checkpoint: `66a34e8` (`feat: add persistence planning and safety simulation`).
