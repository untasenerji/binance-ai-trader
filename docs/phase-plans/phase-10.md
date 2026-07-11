# Phase 10 Plan: Local Locked-State Operator UI

## Scope

- Ten navigable operator views: overview, market radar, trade planner, positions and orders, risk center, strategy lab, AI center, system health, audit log, and connection wizard.
- Local FastAPI control-plane WebSocket that emits only anonymous locked status.
- Health status, trade-plan/risk visualizations, audit projections, scenario states, desktop/tablet/mobile layouts, and a Phase 14-locked connection screen.

## Non-goals

- No Binance or OpenAI credential field, account lookup, user stream, browser trade tool, key transport, connection test, test order, real order, activation switch, or live execution path.

## Exit Gate

- Every required view is navigable and responsive at desktop and tablet widths; mobile has no horizontal page overflow.
- Loading, stale, disconnected, partial-fill, missing-stop, and halt states are visible through the local scenario surface.
- The control-plane stream and every UI status continue to report `live_trading_enabled=false` and locked execution.
