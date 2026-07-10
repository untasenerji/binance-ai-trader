# Phase 0 Report - Research and Decision Lock

**Status:** completed documentation-only phase
**Date:** 2026-07-10
**Scope:** research, decisions, contract matrices, formulas, failure handling, and source traceability. No application feature, API credential, key read, signed request, test-order request, or matching-engine order was created or sent.

## Outcome

The project can remain safely paused after Phase 0. `docs/BINANCE_ENDPOINT_MATRIX.md`, `docs/RISK_FORMULAS.md`, and `docs/FAILURE_SCENARIOS.md` provide the implementation contracts for later phases. `docs/CONFLICT_MATRIX.md` records the resolved or intentionally deferred inconsistencies.

The current OpenAI catalog verifies `gpt-5.6-terra` and `gpt-5.6-luna`. They remain configuration candidates, not embedded defaults or a capability assumption: Phase 9 must verify real API-account availability before selecting either one. An unavailable model makes AI advisory unavailable; it cannot alter risk, state, or exchange behavior.

## Locked Decisions

| ID | Locked decision | Phase 0 implication |
|---|---|---|
| D-001 | Binance USD-SM Futures only. | Endpoint matrix is USD-SM only. |
| D-002 | V1 is a single-account local modular monolith. | No distributed execution or multi-tenant design. |
| D-003 | Real adapter exists later but stays closed until Phase 14. | No live adapter feature is built in Phase 0. |
| D-004 | Pilot may use live after read-only and live `/order/test` checks. | Testnet is not a substitute for the safety gates. |
| D-005 | One-way mode only. | `positionSide=BOTH`; no simultaneous long/short on a symbol. |
| D-006 | Isolated margin only. | Cross margin and auto-add margin are preflight failures. |
| D-007 | Leverage and capital cannot increase without approval. | D-026 makes the current pilot ceilings backend-enforced. |
| D-008 | Every open position requires server-side stop protection. | Stop confirmation is a state-machine gate. |
| D-009 | Rejected/missing stop triggers emergency reduction. | Failure policy halts instead of continuing unprotected. |
| D-010 | No martingale or uncontrolled averaging down. | Back-loaded ladder remains disabled by hard policy. |
| D-011 | Total staged risk is planned before entry. | Solver uses all-fill worst-case loss. |
| D-012 | Exchange minimums never increase risk automatically. | Invalid quantities cause stage reduction or `SKIP_TRADE`. |
| D-013 | AI never sees Binance credentials or has order authority. | AI receives a minimised structured snapshot only. |
| D-014 | AI mode is advisory/veto only; no order authority. | No exchange, risk, or config tool is passed to the model. |
| D-015 | Withdrawal and transfer permissions are off. | Future connection wizard validates least privilege. |
| D-016 | Trading calculations use `Decimal` only. | Formulas and transport values stay exact strings/decimals. |
| D-017 | Every order has a unique client ID and idempotency record. | UNKNOWN handling queries before any new attempt. |
| D-018 | Binance is authoritative over local storage. | Restart/reconnect always reconciles. |
| D-019 | Partial fill, duplicate, out-of-order, and 503 UNKNOWN are tested before live. | Failure scenarios are Phase 7 acceptance cases. |
| D-020 | Live signal loop does not require web search. | OpenAI web search is absent from the trading path. |
| D-021 | Live activation is multi-step. | No one-click trading toggle is planned. |
| D-022 | Manual position/order conflicts are refused. | Orphan/manual state halts automation. |
| D-023 | Every phase produces a test report. | `docs/test-reports/phase-00.md` is added. |
| D-024 | Codex never exposes real secrets. | Phase 0 used no local secret or account file. |
| D-025 | Risk conformity and cost-adjusted EV matter, not profit targets. | Planner reports maximum loss and costs first. |
| D-026 | V1 pilot values are backend hard caps above UI settings. | UI can lower values only; backend rejects any increase before planning. |

## Decision Classification

**Fixed now:** D-001 through D-026, the normal-versus-Algo order split, stop/TP quantity policy, user-stream reconciliation precedence, and the OpenAI no-tools boundary.

**Research resolved:** USD-SM endpoint paths, signed-request timing, filter/reconciliation sources, 503 variants, user-data lifecycle, conditional-order events, strict structured output, and documentation-only MCP scope.

**Deferred technical work:** exact ATR parameters, technical-level detection rules, order-book eligibility thresholds, TWAP scheduling, dead-man switch contract tests, symbol whitelist, and the account-accessible AI model choice. These need tests or runtime checks in later phases, not user approval today.

**User decision currently required:** none. Phase 1 is intentionally not started and requires explicit user authorization.

## Ladder Variants

| Variant | Data prerequisite | Benefit | Main risk / limit | V1 pilot posture |
|---|---|---|---|---|
| Equal price interval | Reference price and dynamic symbol filters | Simple, inspectable, deterministic. | Fixed spacing ignores volatility regime. | Supported after Phase 6 tests. |
| ATR-based | Closed candles, tested ATR window and multiplier. | Spacing adapts to volatility. | Parameters can be overfit. | Supported only with walk-forward evidence. |
| Technical level | Deterministic pivot/swing/VWAP/Donchian definition. | Aligns entries with an explicit market structure. | Overlapping or invalid levels need merge/reject logic. | Supported only after deterministic fixtures. |
| Order-book liquidity | Correct local book plus enough recorded shadow history. | Can avoid thin/unstable levels. | Snapshot/sequence gaps and spoofing make candle-only backtest invalid. | Shadow-only, default off in V1 pilot. |
| Time-sliced / TWAP-like | Validity window, participation rule, slippage observation. | Reduces impact for larger orders. | Adds operational timing complexity with little benefit at 20 USDT. | Infrastructure candidate; disabled by default. |

All variants share one risk solver: every possible stage fill, fees, adverse slippage, funding buffer, current filters, and hard caps must pass before the plan can arm.

## OpenAI Authority Boundary

| Allowed | Prohibited |
|---|---|
| Regime/risk classification, signal critique, structured advisory or allowlisted veto, plain-language explanation, post-trade analysis. | Seeing Binance keys, reading keyring/`.env`, receiving an order function, changing risk limits/leverage/stops, cancelling protection, or generating executable exchange commands. |

Phase 9 will call the Responses API with `ai_output_schema.json` as a strict structured-output contract and with no custom, hosted, MCP, web-search, shell, browser, or Binance tool exposed to the model. Backend schema validation remains mandatory even with strict output.

## MCP Boundary

Only `openaiDeveloperDocs` is permitted for development research. It is configured at `https://developers.openai.com/mcp`, is read-only, sees only public OpenAI documentation, and has no runtime application role. No Binance MCP, trading MCP, secret-bearing MCP, or MCP that can send orders is allowed.

## Phase 0 Acceptance Checklist

- [x] Required repository and documentation files were read before documentation changes.
- [x] D-001 through D-026 were extracted and classified.
- [x] Backend-over-UI hard-cap correction is recorded in `MASTER_SPEC.md` and `docs/DECISIONS.md`.
- [x] Current official Binance normal/algo, account, market, stream, timing, rate-limit, and error contracts are traceable in `docs/SOURCES.md`.
- [x] Current official OpenAI Responses, structured-output, tool-control, Docs MCP, and model-catalog decisions are traceable in `docs/SOURCES.md`.
- [x] Long/short risk, rounding, cost, partial-fill, stop, and TP rules are written for later implementation.
- [x] Required failure scenarios are specified for simulator/chaos testing.
- [x] No credential request, secret read, real API request, or real order was performed.
- [x] Phase 1 remains `NOT_STARTED` pending explicit user authorization.

The documentation-only Phase 0 gate is green. This status does not start Phase 1 or unlock Phase 14.
