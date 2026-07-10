# PLAN.md

This plan is subordinate to `MASTER_SPEC.md`. The only project root is `E:\CHATCP UYGULAMA FX\Binance_AI_Trader_Codex_Starter_Pack\binance_ai_trader_starter`.

## Cross-phase rules

1. Work on one phase only. Do not begin the next phase automatically.
2. Read `AGENTS.md`, `MASTER_SPEC.md`, this plan, `docs/DECISIONS.md`, and `docs/PHASE_STATUS.md` before changing code.
3. Every phase has an implementation note, focused automated checks, `docs/test-reports/phase-XX.md`, and an updated phase status.
4. No real Binance key, secret, live order, or browser/Codex/MCP trade tool is permitted before the locked Phase 14 flow.
5. All money, quantity, price, rounding, cost, and risk calculations use `Decimal`. Exchange truth wins during reconciliation.
6. Backend hard limits are authoritative. UI or local API inputs above D-026 limits are rejected, never clamped upward or used to make an order pass exchange filters.
7. D-027 is authoritative: scan cadence and timeframe identify opportunities; they never impose a trade-count, trade-frequency, or order-generation obligation. A zero-trade result is valid and expected when the complete candidate gate does not pass.

## Phase gates

| Phase | Scope | Concrete outputs | Required validation / exit gate |
|---|---|---|---|
| 0 | Research and decision lock | `docs/SOURCES.md`, `docs/PHASE_0_REPORT.md`, `docs/CONFLICT_MATRIX.md`, `docs/BINANCE_ENDPOINT_MATRIX.md`, `docs/RISK_FORMULAS.md`, `docs/FAILURE_SCENARIOS.md` | Official Binance/OpenAI source verification; no unresolved safety contradiction; no implementation or credential use. |
| 1 | Repo and tooling | Python backend and React/TS frontend skeletons, Compose plus Windows fallback, README, CI, secret scan, exact commands in `AGENTS.md` | Empty app health check, lint, type check, unit-test harness, frontend lint/test harness, security scan all green. |
| 2 | Domain and Decimal core | Domain models, state machine, filter/rounding helpers, server-side hard-limit policy | Unit and property tests prove no float trade math, forbidden transitions rejected, hard limits reject UI-over-cap input. |
| 3 | Public Binance data | Public REST/WS adapter, freshness clock, reconnect and order-book snapshot logic | Public smoke test; stale/reconnect/ping-pong/snapshot-sequence tests; no authenticated routes. |
| 4 | Persistence and replay | PostgreSQL models/migrations, append-only audit events, dedupe ledger, replay/reconciliation skeleton | Restart, duplicate event, audit append, and DB-failure circuit-breaker tests. |
| 5 | Strategy lab | Strategy interface, no-trade baseline, candidate strategies, backtest and walk-forward workflow | No look-ahead; fees/slippage/funding included; deterministic fixtures and out-of-sample report; D-027 candidate gate proves no frequency-driven order candidate can be created. |
| 6 | Ladder and risk | Price variants, quantity solver, stop/TP planner, long/short risk projection | All-fill loss never exceeds budget; partial-fill exit quantities never exceed position; min-notional does not increase risk. |
| 7 | Simulator and chaos | Exchange simulator and controlled failure injection | Partial fill, duplicate, delayed event, disconnect, 503 UNKNOWN, 429, 418, -1021, and rejected-stop tests green. |
| 8 | Locked live adapter | Signed REST abstraction, User Data Stream, normal/algo order abstractions, reconciliation, `/order/test`, `LIVE_TRADING_ENABLED=false` | Contract/mocked tests with no secret; AI/frontend cannot invoke trade adapter; no matching-engine order path enabled. |
| 9 | OpenAI layer | `AIProvider`, Responses API structured-output contract, advisory/veto/post-trade modes, mock provider and cost tracking | Invalid/refusal/timeout output fails safely; no tools supplied to AI; AI cannot change limits, stops, leverage, or order state. |
| 10 | UI | Dashboard, planner, risk, health, audit, connection wizard locked state | Playwright desktop/tablet checks; empty/loading/stale/disconnected/partial-fill/stop-missing/halt states covered. |
| 11 | Observability | Redacted structured logs, metrics, alerts, backup/restore procedure | Log redaction, alert dedupe, clock-skew, disk/DB recovery tests green. |
| 12 | Security and chaos | Threat model, dependency scan, process/network partition recovery | No critical finding; restart with an open simulated position reconciles; stop-protection invariant preserved. |
| 13 | Pre-live acceptance | `FINAL_ACCEPTANCE_REPORT.md`, risk config preview, operating guide | Phases 0-12 green; live connection UI may be displayed but remains inactive. |
| 14 | Local connection and pilot | Local wizard, read checks, permissions, live `/order/test`, activation phrase, tightly capped pilot | Explicit local consent after all checklist items; first matching-engine order only after a displayed maximum-loss review. |

## Phase 0 record

Phase 0 is complete as a documentation-only phase. The `gpt-5.6-terra` and `gpt-5.6-luna` model identifiers are verified in the current official OpenAI model catalog. Phase 9 must still check real availability for the API account at runtime and choose the model through configuration without weakening the advisory-only boundary.

## Phase 1 record

Phase 1 is complete as a tooling and skeleton-only phase. It provides the FastAPI `/api/health` route, a React/TypeScript locked workspace shell, Docker Compose, Windows fallback scripts, pinned dependency locks, lint/type-check/unit/E2E tooling, pre-commit configuration, Gitleaks scanning, and CI. Docker Compose was built and exercised locally against the health endpoint. No Binance credential, authenticated endpoint, test order, real order, or OpenAI execution tool was added.

Phase 2 remains `NOT_STARTED` until the user explicitly authorizes it.
