# Phase 02 Plan: Decimal Domain Core

## Scope

- Decimal-only trade-stage and direction value objects.
- Exchange price/quantity/notional filter validation and downward rounding.
- Explicit trade-plan state transition guards.
- D-026 backend hard-cap validation that rejects above-cap input rather than clamping it.

## Non-goals

- No network client, database, API key, signed request, order intent dispatch, or live/test order path.

## Exit Gate

- Unit and Hypothesis property tests prove Decimal rounding behavior.
- A source-level test rejects binary float literals in the domain package.
- Forbidden state transitions and above-cap risk requests are rejected.
