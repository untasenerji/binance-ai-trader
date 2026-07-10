# Risk Formulas and Partial-Fill Protection

All values below are `Decimal` values. Prices and quantities sent to Binance are exact decimal strings derived from these calculations. Source IDs refer to `docs/SOURCES.md`.

## 1. Hard-Limit Envelope

For any user/UI configuration value `U_x` and backend hard cap `H_x`, the effective value is:

```text
effective_x = min(U_x, H_x)
```

The backend rejects `U_x > H_x` with `HARD_RISK_LIMIT_EXCEEDED`; it does not silently substitute a higher or lower value. The live pilot caps are defined in `MASTER_SPEC.md` Section 18 and D-026.

```text
E_bot = min(verified_available_equity, U_pilot_equity_cap, H_pilot_equity_cap)
R_budget = min(
  U_risk_per_trade, H_risk_per_trade,
  remaining_daily_loss_limit,
  remaining_weekly_drawdown_limit,
  E_bot * configured_risk_fraction_if_present
)
```

The plan is invalid unless `E_bot > 0`, `R_budget > 0`, every hard limit passes, and one-way/isolated/server-stop invariants pass.

## 2. Definitions and Stop Validity

| Symbol | Meaning |
|---|---|
| `i` | Planned entry stage index. |
| `E_i` | Requested entry price after directional tick rounding. |
| `q_i` | Planned quantity after step-size floor. |
| `f_i`, `q_i_fill` | Actual fill price and filled quantity. |
| `S` | Requested stop trigger price after directional rounding. |
| `s_in`, `s_out` | Entry and stop-exit slippage buffers in decimal form; `bps / 10000`. |
| `m_in`, `m_out` | Conservative entry and exit commission rates. |
| `u_funding` | Conservative funding-buffer rate per planned funding interval. |
| `n_funding` | Number of funding intervals conservatively buffered. |
| `w_i` | Risk weight for stage `i`; all weights sum to `1`. |

For a legal ladder:

```text
LONG:  S < min(E_i for every potentially fillable stage)
SHORT: S > max(E_i for every potentially fillable stage)
```

Any stage on the wrong side of stop is rejected. A higher stage count or larger quantity is never used to repair an invalid plan.

## 3. Directional Rounding and Worst-Case Prices

Let `tick` be the current exchange tick size and `step` the current quantity step size.

```text
floor_tick(x) = floor(x / tick) * tick
ceil_tick(x)  = ceil(x / tick) * tick
floor_step(x) = floor(x / step) * step
```

The planner must calculate risk using exactly the rounded/requested prices, not the unrounded strategy target.

| Value | Long | Short |
|---|---|---|
| Requested entry price | `ceil_tick(entry_target)` | `floor_tick(entry_target)` |
| Requested stop trigger | `floor_tick(stop_target)` | `ceil_tick(stop_target)` |
| Requested fixed TP limit | `floor_tick(tp_target)` | `ceil_tick(tp_target)` |
| Worst entry fill | `ceil_tick(E_i * (1 + s_in))` | `floor_tick(E_i * (1 - s_in))` |
| Worst stop exit fill | `floor_tick(S * (1 - s_out))` | `ceil_tick(S * (1 + s_out))` |

`pricePrecision` and `quantityPrecision` are never used as rounding rules; current filters from `exchangeInfo` are authoritative (B-03).

## 4. Planned All-Fill Risk

For long stage `i`:

```text
price_loss_i = q_i * (worst_entry_fill_i - worst_stop_exit_fill_i)
entry_fee_i  = q_i * worst_entry_fill_i * m_in
exit_fee_i   = q_i * worst_stop_exit_fill_i * m_out
funding_i    = q_i * worst_entry_fill_i * u_funding * n_funding
risk_i       = price_loss_i + entry_fee_i + exit_fee_i + funding_i
```

For short stage `i`:

```text
price_loss_i = q_i * (worst_stop_exit_fill_i - worst_entry_fill_i)
entry_fee_i  = q_i * worst_entry_fill_i * m_in
exit_fee_i   = q_i * worst_stop_exit_fill_i * m_out
funding_i    = q_i * worst_entry_fill_i * u_funding * n_funding
risk_i       = price_loss_i + entry_fee_i + exit_fee_i + funding_i
```

`u_funding` is a positive loss buffer. Directional realized funding is stored separately; the plan never treats favorable future funding as risk budget.

Per-unit risk and sizing:

```text
unit_risk_i = risk_i / q_i
raw_qty_i   = (R_budget * w_i) / unit_risk_i
q_i         = floor_step(raw_qty_i)
```

After every rounding and filter operation the planner recomputes:

```text
R_all_fill = sum(risk_i)
N_all_fill = sum(q_i * worst_entry_fill_i)
M_required = sum(q_i * worst_entry_fill_i / effective_leverage) + required_reserve

R_all_fill <= R_budget
N_all_fill <= effective_symbol_and_total_exposure_limits
M_required <= E_bot
```

Each stage must also pass current `minQty`, `maxQty`, step, price, notional, symbol status, leverage bracket, and trigger-protection checks. If a rounded stage is too small, the only legal outcomes are merge with an existing stage while preserving the total-risk proof, remove it, or return `SKIP_TRADE`.

## 5. Realized Entry, Costs, and Risk

For the filled-stage set `F`:

```text
Q_filled   = sum(q_i_fill for i in F)
avg_entry  = sum(q_i_fill * f_i for i in F) / Q_filled
notional   = sum(q_i_fill * f_i for i in F)
```

Actual stop-risk projection after a partial or full fill:

```text
LONG actual_risk  = sum(q_i_fill * (f_i - worst_stop_exit_fill))
                  + actual_or_conservative_entry_fees
                  + projected_stop_exit_fees
                  + funding_buffer_for_open_quantity

SHORT actual_risk = sum(q_i_fill * (worst_stop_exit_fill - f_i))
                  + actual_or_conservative_entry_fees
                  + projected_stop_exit_fees
                  + funding_buffer_for_open_quantity
```

The UI later shows planned and actual values separately: gross price loss, entry fee, exit fee, spread/slippage buffer, funding buffer, realized funding, and net PnL. OpenAI cost is an operational cost, not a hidden trade PnL component.

## 6. Partial Fills, Stop, and Take-Profit Updates

1. On any entry `PARTIALLY_FILLED` or `FILLED` event, read/confirm `Q_pos = abs(positionAmt)` from Binance position state. The exchange position, not a local fill sum, is authoritative.
2. Before the trade enters `POSITION_PROTECTED`, submit and verify exactly one protective Algo `STOP_MARKET` with `closePosition=true`, the inverse side (`SELL` for long, `BUY` for short), and no `quantity` or `reduceOnly`. This close-all stop covers the position existing when it triggers; it is re-verified after every fill/reconciliation.
3. If the stop is not confirmed active, cancel remaining entry stages, enter emergency reduction for the confirmed quantity, and hard-halt. No new stage may fill under bot control without a confirmed stop.
4. Fixed partial TP orders are normal `LIMIT` orders with `reduceOnly=true`, not `closePosition=true`. After any position-size change, cancel only obsolete normal TP orders, then calculate a fresh allocation from `Q_pos`.

For TP weights `t_1 ... t_k` with `sum(t_j)=1`:

```text
tp_qty_j = floor_step(Q_pos * t_j)      for j = 1 .. k-1
tp_qty_k = Q_pos - sum(tp_qty_j)
```

The final remainder is assigned to the last TP only if it passes current exchange filters. A below-minimum TP is merged into a later legal target or omitted; it never increases total quantity.

```text
sum(open_reduce_only_tp_qty) <= Q_pos
```

On a new entry fill, TP cancellation/replacement happens only after the protective stop remains verified. On a TP fill, retain the close-all stop for the remaining position, re-read `Q_pos`, and recalculate only remaining TPs. When the stop triggers, cancel pending entries and normal TPs best-effort, reconcile, and never assume the closing fill from local intent alone.

## 7. Long and Short Summary

| Aspect | Long | Short |
|---|---|---|
| Entry side | BUY | SELL |
| Stop side | SELL | BUY |
| Valid stop | Below every potential entry | Above every potential entry |
| Price loss | Entry minus stop exit | Stop exit minus entry |
| Fixed TP side | SELL reduce-only | BUY reduce-only |
| Directional rounding | Entry up, stop down, TP down | Entry down, stop up, TP up |

No formula uses leverage to reduce the reported price risk. Leverage affects required margin and exchange caps, while stop loss, fees, slippage, and funding determine the real risk budget.
