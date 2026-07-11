# Phase 08 Plan: Locked Exchange Adapter Contracts

## Scope

- Typed normal-order, Algo-order, signed-request, user-stream, and reconciliation contracts.
- Hard `LIVE_TRADING_ENABLED=false` implementation that blocks every authenticated operation before any signer/transport boundary.
- Mock reconciliation mismatch coverage.

## Non-goals

- No API key/secret storage, signing implementation, REST/WebSocket request, listen key, `/order/test`, matching-engine order, or frontend/Codex/AI trade tool.

## Exit Gate

- Every locked adapter method raises `LIVE_TRADING_DISABLED`.
- Contract tests prove reconciliation mismatch is visible.
- No credential or network action is required for the application or tests.
