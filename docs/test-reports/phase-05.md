# Phase 05 Strategy Lab Validation Report

**Date:** 2026-07-11
**Scope:** Research-only signals, no-trade behavior, deterministic backtesting, and walk-forward evaluation.

## Delivered

- Strategy protocol that can return a `SignalCandidate` or `None`, with no execution dependency.
- `NoTradeBaseline`, `TrendPullbackStrategy`, `VolatilityBreakoutStrategy`, and `MeanReversionStrategy` candidate families.
- D-027 `CandidateGate` requiring strategy signal, risk allowance, fresh data, acceptable cost, valid signal time, and positive expected net value.
- Next-bar backtest execution with separately measured entry/exit fees, slippage, funding, gross PnL, and net PnL.
- Deterministic walk-forward windows that retain only out-of-sample trades.

## Validation

| Check | Result | Evidence |
|---|---|---|
| No-trade baseline | PASS | Baseline returns no candidate. |
| D-027 no-frequency behavior | PASS | 100 repeated scans without a signal produced zero accepted candidates. |
| Candidate gate | PASS | Strategy, risk, data, cost, validity, and expected-value failures all reject the candidate. |
| Strategy fixtures | PASS | Trend pullback, breakout, and mean-reversion fixtures produce deterministic research candidates. |
| Look-ahead protection | PASS | Strategy receives history only through the signal bar; simulation enters on the next bar. |
| Cost accounting | PASS | Fees, slippage, and funding are individually charged and net PnL is lower than gross PnL in the fixture. |
| Walk-forward | PASS | Test windows contain only out-of-sample trades and are deterministic. |
| Backend suite | PASS | pytest: 34 passed, 1 live-public test deliberately deselected. |

## Independent Audit Remediation (2026-07-12)

- Candle streams now reject reverse, duplicate, overlapping, mixed-symbol, incomplete, and future-visible input rather than sorting or inferring safety.
- Every walk-forward window receives a fresh strategy from a factory, retains global boundaries, and rejects unsafe overlapping out-of-sample aggregation.
- Backtest accounting separately records market gross, execution PnL, slippage, executed-notional fees, and signed long/short funding settlements.
- Current PostgreSQL-inclusive validation: 172 passed, 1 public-live test deselected, 84.06% branch coverage.

## Safety Confirmation

- Strategy scans do not produce an order, order intent, or execution command.
- No trade count, scan cadence, or timeframe creates a trade obligation.
- No live market/account call, credential, or AI decision is introduced.

## Outcome

Phase 5 exit gate is satisfied. Phase 6 is authorized by the user's 2026-07-10 instruction and remains limited to Decimal ladder, risk, and exit planning.

## Git Checkpoint

- Phase 4-8 implementation checkpoint: `66a34e8` (`feat: add persistence planning and safety simulation`).
