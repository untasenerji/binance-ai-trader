# Phase 05 Plan: Strategy Research Lab

## Scope

- Strategy contract, no-trade baseline, trend pullback, volatility breakout, and mean-reversion candidate families.
- D-027 candidate gate over strategy, risk, data quality, cost, and validity.
- Deterministic close-only backtest and walk-forward workflow with fees, slippage, and funding.

## Non-goals

- No order intent, execution, account data, AI decision, parameter auto-optimization, or live strategy activation.

## Exit Gate

- Repeated scans with no signal produce no candidate.
- Look-ahead is blocked by next-bar execution.
- Costs are included and deterministic fixture results are reproducible.
