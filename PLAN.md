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

## Phase 2 record

Phase 2 is complete. The backend now has Decimal-only domain primitives, exact exchange filter helpers, an explicit trade-plan state machine, and D-026 backend hard-cap rejection. Property tests reject float literals in the domain and prove rounding stays within the requested value. No market or authenticated exchange integration was added.

Phase 3 is active under the user's explicit 2026-07-10 authorization. It remains limited to public, unauthenticated market data.

## Phase 3 record

Phase 3 is complete. Official Binance public REST and WebSocket routing contracts were revalidated; the credential-free client, freshness clock, reconnect policy, and shadow order-book sequence procedure are covered by mock tests and a live public server-time smoke test. No authenticated endpoint or execution path was added.

Phase 4 is active under the user's explicit authorization and is limited to local persistence, replay, audit, and reconciliation skeletons.

## Phase 4 record

Phase 4 is complete. SQLAlchemy/Alembic persistence records append-only audit events, duplicate deliveries, projections, and reconciliation runs. SQLite tests and the local Docker PostgreSQL migration passed. A persistence write failure is a hard gate for new entries; no exchange adapter was introduced.

Phase 5 is active under the user's explicit authorization and remains research-only. It must implement D-027 as a no-trade candidate gate rather than an order-frequency target.

## Phase 5 record

Phase 5 is complete. Research strategies emit only candidates, never orders. D-027 is enforced by a complete candidate gate and a no-trade baseline. Deterministic backtest and walk-forward workflows account for fees, slippage, and funding without look-ahead.

Phase 6 is active under the user's explicit authorization and remains limited to Decimal ladder, position sizing, stop, and take-profit planning.

## Phase 6 record

Phase 6 is complete. The planner supports five ladder variants, exact costs, risk-budgeted quantities, and partial-fill exit refresh. Filter failures skip the plan rather than increase risk, and every test-confirmed TP allocation remains within confirmed position quantity.

Phase 7 is active under the user's explicit authorization and is limited to a local simulator and controlled failure injection.

## Phase 7 record

Phase 7 is complete. The local simulator covers fills, duplicate/delayed delivery, UNKNOWN outcomes, rate limits, clock skew, disconnect, stop rejection, and slippage. Unsafe or uncertain outcomes pause, reconcile, reduce risk, or halt; they never create an automatic duplicate order.

Phase 8 is active under the user's explicit authorization and is limited to feature-locked Binance adapter contracts and mocks.

## Phase 8 record

Phase 8 is complete. Binance normal/algo/user-stream/test-order/reconciliation contracts exist only behind a fail-closed locked adapter. No signer, transport, API key, secret, or network invocation is implemented or reachable before Phase 14.

Phase 9 is active under the user's explicit authorization and remains advisory-only with no AI tool access.

## Phase 9 record

Phase 9 is complete. The AI layer is a local, no-tools Responses API contract with strict schema validation, a mock-only provider, model-availability evidence, Decimal usage accounting, and fail-safe outcome handling. No API key, SDK client, network request, account availability request, hosted tool, order capability, or risk mutation exists. Account model availability is intentionally unverified under the credential lock; the configured model remains unavailable until a future authorized local check proves access. Advisory output cannot affect a candidate, while `veto_only` is limited to an allowlisted one-way rejection.

Phase 10 is active under the user's explicit 2026-07-11 authorization and remains a local locked-state UI. It must not request credentials, activate a connection, or expose any order path.

## Phase 10 record

Phase 10 is complete. The local operator workspace has ten navigable screens, desktop/tablet/mobile layouts, visual stale/disconnected/partial-fill/missing-stop/halt states, and a local control-plane WebSocket that reports only anonymous locked status. The Phase 14 connection wizard is display-only and disabled. No credential field, connection test, user stream, account call, test order, real order, or browser execution path exists.

Phase 11 is active under the user's explicit 2026-07-11 authorization and remains limited to redacted observability, metrics, alerts, and backup/restore procedures. It must not add a trading transport or credential flow.

## Phase 11 record

Phase 11 is complete. Structured logs redact sensitive fields before their sink, local metrics are label-free, and the local alert contract includes deterministic deduplication without any network adapter. Database/disk/clock/restart uncertainty produces a no-new-entry recovery decision. Daily reports use Decimal values and backup/restore readiness requires the Phase 4 audit hash chain to validate. No credential, exchange, or execution capability was added.

Phase 12 is active under the user's explicit 2026-07-11 authorization and remains limited to threat modeling, dependency/security scans, and local chaos recovery. The live adapter remains hard locked.

## Phase 12 record

Phase 12 is complete. The threat model documents the credential, execution, AI, persistence, network, and supply-chain boundaries. Full Python and Node dependency scans are enforced by `scripts/check.ps1`; an initial pytest vulnerability finding was upgraded and the final scans were clean. A durable, atomic local recovery checkpoint proves that a partial simulated position can be recovered only with valid audit/projection evidence and confirmed simulated stop evidence. Network partitions pause entries and require reconciliation plus stop re-verification; absent or unconfirmed protection hard-halts. The result never unlocks execution and does not claim a real Binance-side verification.

Phase 13 is active under the user's explicit 2026-07-11 authorization and remains limited to pre-live acceptance documentation, a read-only risk profile preview, user guidance, and full validation. The Phase 14 connection flow remains inactive.

## Phase 13 record

Phase 13 is complete. `GET /api/risk-config-preview` returns a fixed, read-only serialization of D-026 backend caps and the UI renders it without any mutation or activation control. The user guide and final acceptance record document local operation and the remaining Phase 14 gates. Full acceptance passed with 70 backend tests, one deliberately deselected credential-free public smoke test, frontend format/lint/type-check/test/build, two Playwright scenarios, clean Python and Node dependency scans, a clean Gitleaks scan, pre-commit, and Docker Compose configuration. The Connection Wizard remains display-only, `LIVE_TRADING_ENABLED=false`, and no credential or order capability was introduced.

## Independent audit remediation record

The first independent P0/P1/P2 remediation completed on 2026-07-12 and is recorded in `docs/AUDIT_REMEDIATION_REPORT.md`. Its historical 84.06% and 83.73% values are coverage.py total coverage, not true branch coverage.

The second independent remediation is recorded in `docs/SECOND_AUDIT_REMEDIATION_REPORT.md`. Its local PostgreSQL-inclusive validation is 200 passed plus 1 intentionally deselected public-live test, 83.01% total coverage, and 65.49% true branch coverage (`702/1072`) against the independent 65% branch-only gate. The Windows full chain is 193 passed plus 8 deliberate deselections, 82.80% total coverage, and 65.11% true branch coverage (`698/1072`). The workflow installs checksum-verified Gitleaks before its backend secret-scan contract; a Linux container smoke test passed, but hosted GitHub Actions has not run for this uncommitted worktree and is not claimed as a pass. The base Git revision is `022631d`; no second-remediation commit or push was created by instruction. Phase 14 remains locked.
