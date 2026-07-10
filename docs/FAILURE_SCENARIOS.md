# Failure Scenarios and Safe-State Policies

This is a Phase 0 behavioral contract for later simulator, state-machine, and live-adapter work. It does not send requests. Source IDs refer to `docs/SOURCES.md`.

## Global Invariants

- Binance account, position, normal-order, algo-order, and user-trade truth overrides the local database after any uncertainty.
- New entries pause whenever market/user data is stale, state is uncertain, reconciliation is incomplete, or a hard risk condition fails.
- A confirmed server-side stop is required before a filled position is considered protected or managed.
- `HALTED` permits only reconciliation, cancellation, and risk-reducing actions. It never permits new entry.
- Every received event is durably recorded before an idempotent state transition; raw duplicate delivery cannot create a second fill, order, or risk allocation.
- An order intent with UNKNOWN outcome is not retried until exchange evidence resolves it.

## Scenarios

| ID | Trigger | Immediate response | Recovery / final state | Required Phase 7 test |
|---|---|---|---|---|
| F-01 | Market or user WebSocket disconnect | Pause new entries. Keep confirmed server-side stop untouched. Mark data stale. | Reconnect with jitter before/after 24-hour limit, rebuild subscriptions, then run full reconciliation before unpausing. If user stream remains unhealthy past policy threshold, cancel pending entries and halt. | Stream drop during pending stages and open protected position. |
| F-02 | Program/process restart | Start in `RECOVERY`; issue no entry command. | Load local audit state, query account/position/open normal orders/open Algo orders/known IDs/recent trades, then compare. Missing stop on a bot position escalates to emergency reduction and halt. | Kill/restart with partial fill, pending stage, and protected position. |
| F-03 | Duplicate event | Persist or identify duplicate by exchange event/order/algo/trade identity before mutation. | Exact repeat is audit-visible but state-neutral. Events that conflict materially trigger reconciliation, not a guessed merge. | Same `ORDER_TRADE_UPDATE` and `ALGO_UPDATE` delivered twice. |
| F-04 | Out-of-order/cross-source event | Do not regress a terminal state to an earlier state. Queue or ignore stale state mutation after comparison to event times and exchange IDs. | Use user-stream ordering within its documented scope; reconcile when REST and stream evidence disagree. | Delayed cancel after fill; REST snapshot older than stream event. |
| F-05 | HTTP 503 with `Unknown error...` | Set intent to `ORDER_STATUS_UNKNOWN`; do not re-send with the same or a new economic order. | Observe user stream, query normal/algo order by client ID, query open lists and position. Only after bounded evidence of absence may a new attempt ID be considered. | 503 UNKNOWN followed by NEW, FILLED, PARTIAL, CANCELED, and absent outcomes. |
| F-06 | HTTP 503 known failure / `-1008` | Treat documented known failure as failed request. Pause entry concurrency; preserve risk-reducing priority. | Bounded exponential backoff only for a known failed operation. Reduce-only, close-position, and cancel actions remain the preferred safety path. | Service unavailable, internal error, and system protection failures. |
| F-07 | HTTP 429 | Stop new nonessential calls and entries; record headers and backoff. | Resume only after throttle policy allows it and streams/reconciliation are healthy. | Rate-limit exhaustion without duplicate intent or busy-loop. |
| F-08 | HTTP 418 | Hard halt; no automatic order retry. | Require documented cooldown/operator review and fresh reconciliation before any later activation. | Escalated rate-limit ban simulation. |
| F-09 | `-1021` timestamp / clock skew | Refresh server time and invalidate the signed request timestamp. | Retry only if the request is known not to have unknown execution status; otherwise follow F-05. | Clock offset both ahead and outside `recvWindow`. |
| F-10 | Initial stop placement rejected | Cancel all remaining entries immediately. | Confirm current position, attempt only tested risk-reduction path, raise critical alert, and enter `HALTED`. | First partial fill followed by rejected Algo stop. |
| F-11 | `CONDITIONAL_ORDER_TRIGGER_REJECT` or Algo stop `REJECTED`/unexpected expiry | Treat as an unprotected open position emergency. | Query position and Algo state; cancel entries/TPs as appropriate, execute only risk-reducing fallback, then hard halt even if fallback succeeds. | Stop trigger rejected during market movement. |
| F-12 | Local/exchange position or order mismatch | Freeze automation for affected symbol. | Binance truth determines actual quantity. Local-only expected order is resolved from history; exchange-only manual/orphan position raises alert and requires user handling; bot never takes over a manual position. | Local open/exchange absent, exchange position/local absent, quantity mismatch, unknown Algo stop. |
| F-13 | Filter/status changes | Invalidate unsubmitted plans and pause entry. | Refresh `exchangeInfo`, recompute Decimal quantities/prices/risk, then either re-arm a compliant plan or skip. | Tick, step, min-notional, status, and trigger-protect change. |
| F-14 | Database/audit write failure | Block new entries; keep server-side protection intact. | Attempt durable recovery/reconciliation. If a position cannot be safely audited, cancel entries and halt; risk reduction remains permitted. | Transaction failure before/after simulated exchange event. |

## Reconciliation Procedure

```text
RECOVERY_OR_UNCERTAINTY
  -> pause new entries
  -> obtain server time and validate clock
  -> obtain account/balance and position risk
  -> obtain normal open orders and Algo open orders
  -> query each known clientOrderId/clientAlgoId where needed
  -> obtain recent user trades for fill/fee evidence
  -> compare Binance state to local audit ledger
  -> repair only local representation from Binance facts
  -> verify close-all stop for every bot-owned open position
  -> either resume permitted state or HALT
```

## Partial-Fill Safety Rules

- On the first nonzero fill, the stop-confirmation deadline begins; remaining entry stages cannot be armed into management until it succeeds.
- Pending entry stages remain independent normal orders. A stop trigger, safety halt, stale user stream, or risk-budget breach cancels them.
- The stop is `closePosition=true`; its protection is verified after each position-size change rather than resized with a quantity.
- TP quantities are recalculated from the exchange-confirmed current position after each fill. They use reduce-only normal orders and their total never exceeds the current position.

## Evidence Requirements Before Re-Entry

New entry becomes eligible only when all of the following are true:

1. Market and user streams meet freshness thresholds.
2. No unresolved 503 UNKNOWN intent exists.
3. Account mode, isolated margin, auto-add margin, leverage, filters, and hard caps match policy.
4. No manual/orphan position or order conflicts on the symbol.
5. Local audit state equals Binance reconciliation result.
6. An existing position, if any, has a confirmed valid server-side stop.
