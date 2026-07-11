# Phase 07 Plan: Simulator and Failure Injection

## Scope

- Deterministic local matching behavior for partial fill, duplicate/delayed event, UNKNOWN order state, stop rejection, and spread/slippage.
- Controlled 429, 418, -1021, and disconnect failure paths.
- Safety coordinator actions for pause, reconciliation, cancel, emergency reduction, and halt.

## Non-goals

- No Binance connection, account data, signed request, test order, real order, or browser-triggered command.

## Exit Gate

- Unknown outcome prevents duplicate economic order until reconciliation.
- Stop rejection produces cancel pending entries, emergency reduce, and hard halt actions.
- All listed fault cases are automated tests.
