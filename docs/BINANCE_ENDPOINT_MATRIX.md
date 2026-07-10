# Binance USD-SM Futures Endpoint Matrix

**Research-only contract.** This document specifies later adapter behavior; it does not authorize a connection, signed request, `/order/test`, or live order. Source IDs refer to `docs/SOURCES.md`.

## Common Contract

| Concern | Required rule | Source |
|---|---|---|
| REST base URL | `https://fapi.binance.com` for production; Phase 0 does not call it. | B-01 |
| Security | `TRADE` and `USER_DATA` routes require API key plus signature. `USER_STREAM` requires API key. Public routes require no account credential. | B-01 |
| Timestamp | Send an integer UTC millisecond timestamp on signed requests; calculate and monitor clock skew from `GET /fapi/v1/time`. | B-01, B-03 |
| `recvWindow` | Binance defaults to `5000`; V1 uses a configurable value no greater than the documented `60000` maximum, bounded by the clock-skew policy. | B-01, B-02 |
| Decimal transport | Internal calculations use `Decimal`; price, quantity, trigger, and notional values are serialized from their exact decimal representation, never from binary float. | D-016, B-02 |
| Idempotency | Normal orders use unique `newClientOrderId`; Algo orders use unique `clientAlgoId`, each respecting the documented 36-character pattern. | D-017, B-02 |
| Source of truth | User stream is the primary timely source. REST account, position, normal-order, algo-order, and trade queries reconcile after uncertainty, reconnect, or restart. | D-018, B-04, B-06 |

## Public Market Data

| Function | Method and path | Security | V1 use | Critical handling | Source |
|---|---|---|---|---|---|
| Clock / connectivity | `GET /fapi/v1/time` | None | Startup and periodic skew measurement. | Do not use `exchangeInfo.serverTime` as the clock source. A skew breach pauses new entries. | B-03 |
| Exchange and symbols | `GET /fapi/v1/exchangeInfo` | None | Symbol status, filters, `triggerProtect`, supported types, and rate-limit metadata. | Cache with version/time; refresh before planning, after filter error, reconnect, and defined TTL. | B-03 |
| Candles | `GET /fapi/v1/klines` | None | Closed-candle research and ATR inputs. | Never use an unfinished bar as a closed signal. | B-03 |
| Mark price / current funding | `GET /fapi/v1/premiumIndex` | None | Mark-price stop context, next funding time, current funding estimate. | Use Decimal strings; mark-price data has a freshness deadline. | B-03 |
| Funding history / interval changes | `GET /fapi/v1/fundingRate`, `GET /fapi/v1/fundingInfo` | None | Funding buffer and research. | `fundingInfo` is not a guarantee of the next paid funding; planner uses a conservative buffer. | B-03 |
| Best bid / ask | `GET /fapi/v1/ticker/bookTicker` | None | Spread and slippage guard. | Reject new entries if stale or over `max_spread_bps`. | B-03 |
| Depth snapshot | `GET /fapi/v1/depth` | None | Snapshot for local order-book synchronization. | Use only with diff-depth sequence procedure; never treat one snapshot as durable liquidity. | B-03, B-08 |

## Exchange Information and Symbol Filters

The planner obtains rules from the current symbol entry in `exchangeInfo`, not from fixed decimal places. It requires `status=TRADING` and uses filter values exactly as supplied by Binance.

| Validation | Required backend behavior |
|---|---|
| Price | Apply the current `tickSize`, `minPrice`, and `maxPrice`. Do not infer tick size from `pricePrecision`. |
| Quantity | Apply `stepSize`, `minQty`, and `maxQty` from the relevant current quantity filter. Quantities always round down to the legal step before risk re-check. |
| Notional | Apply the current symbol notional/minimum-notional rule to every executable entry and reduce-only exit. A minimum failure produces stage merge/reduction or `SKIP_TRADE`, never a risk increase. |
| Conditional trigger | For price protection, consume the current symbol `triggerProtect`. A chosen stop policy must be tested against the actual current filter. |
| Symbol changes | Refresh filters and invalidate an unsubmitted plan on filter/status change or filter-related error. |
| Rate limits | Read `rateLimits` from `exchangeInfo`, then observe response headers at runtime; no static per-minute total is embedded in code. |

## Account, Position, and Configuration

| Function | Method and path | Security | V1 use | Critical handling | Source |
|---|---|---|---|---|---|
| Account snapshot | `GET /fapi/v3/account` | USER_DATA | Startup and reconciliation balance/margin view. | Reconcile before arming; do not expose account identity to AI/UI logs. | B-04 |
| Balance | `GET /fapi/v3/balance` | USER_DATA | Determine available bot equity within pilot cap. | Bot usable equity is the smaller of verified availability and the hard pilot cap. | B-04 |
| Position risk | `GET /fapi/v3/positionRisk` | USER_DATA | Authoritative position quantity, entry, mark price, and isolated state. | Use together with `ACCOUNT_UPDATE`; any unknown/manual position halts automation. | B-02, B-04, B-06 |
| Position mode | `GET /fapi/v1/positionSide/dual` | USER_DATA | Verify One-way mode. | `dualSidePosition=false` is required; a mismatch aborts activation. | B-04 |
| Multi-assets mode | `GET /fapi/v1/multiAssetsMargin` | USER_DATA | Verify single-asset mode. | `multiAssetsMargin=false` is required for V1. | B-04 |
| Symbol configuration | `GET /fapi/v1/symbolConfig` | USER_DATA | Verify `ISOLATED`, auto-add false, current leverage, max notional. | Any mismatch blocks arming and forces reconciliation. | B-04 |
| Commission | `GET /fapi/v1/commissionRate` | USER_DATA | Use actual maker/taker rate in plan estimates. | Refresh on activation and relevant interval; use conservative rate until known. | B-04 |
| Leverage brackets | `GET /fapi/v1/leverageBracket` | USER_DATA | Validate notional and leverage constraints. | Use account-specific response, not generic assumptions. | B-04 |

## Leverage and Margin Controls

| Function | Method and path | Security | V1 policy | Source |
|---|---|---|---|---|
| Set leverage | `POST /fapi/v1/leverage` | TRADE | Only in local activation after configuration validation; requested leverage must be less than or equal to D-026 hard cap, then returned value must be re-read/verified. | B-02 |
| Set margin type | `POST /fapi/v1/marginType` | TRADE | Only `ISOLATED`; never request `CROSSED`. A rejected/mismatched change aborts activation. | B-02 |
| Change position mode | `POST /fapi/v1/positionSide/dual` | TRADE | Not a routine trading action. Only local activation may request One-way when no position/open order permits it. | B-02, B-09 |
| Auto-add margin | No setting action selected for V1. | N/A | Read and require `isAutoAddMargin=false`; do not use an endpoint to add margin. | B-04 |

## Normal Orders

| Function | Method and path | Security | V1 role | Critical handling | Source |
|---|---|---|---|---|---|
| Place normal order | `POST /fapi/v1/order` | TRADE | Entry LIMIT orders, explicit emergency reduce-only order, and fixed-price reduce-only TP LIMIT orders. | New entry obeys hard caps and is disabled until Phase 14. Use `newClientOrderId`; avoid batch orders during uncertainty. | B-02 |
| Test request | `POST /fapi/v1/order/test` | TRADE | Phase 14 local activation only. | Validates request without sending to matching engine; no use in Phase 0-13. | B-02 |
| Query normal order | `GET /fapi/v1/order` or `GET /fapi/v1/openOrder` | USER_DATA | Resolve a known client ID and restore state. | Use `origClientOrderId` during UNKNOWN/restart reconciliation. | B-02 |
| List normal open orders | `GET /fapi/v1/openOrders` | USER_DATA | Reconciliation and manual-conflict check. | Always scope by symbol when possible; unscoped route has a higher documented weight. | B-02 |
| Normal order history | `GET /fapi/v1/allOrders` | USER_DATA | Bounded recovery evidence. | History retention is limited; do not depend on it as permanent audit storage. | B-02 |
| Fills/trades | `GET /fapi/v1/userTrades` | USER_DATA | Reconcile fills, fees, realized PnL. | Store exchange trade IDs and dedupe before state mutation. | B-02 |
| Cancel normal order(s) | `DELETE /fapi/v1/order`, `DELETE /fapi/v1/allOpenOrders` | TRADE | Cancel unfilled entries and obsolete normal TP orders. | Never use a bulk cancel without role filtering/reconciliation; protection is checked separately. | B-02 |

## Conditional and Algo Orders

| Function | Method and path | Security | V1 role | Critical handling | Source |
|---|---|---|---|---|---|
| Place Algo order | `POST /fapi/v1/algoOrder` | TRADE | All STOP, conditional TP, and trailing orders. | Set `algoType=CONDITIONAL`; use `clientAlgoId`. Protective stop uses `STOP_MARKET` and `closePosition=true`. | B-02 |
| Query Algo order | `GET /fapi/v1/algoOrder` | USER_DATA | Verify a known stop/TP and resolve UNKNOWN. | Query by `algoId` or `clientAlgoId`; actual order details may appear only after trigger. | B-02 |
| List Algo open orders | `GET /fapi/v1/openAlgoOrders` | USER_DATA | Verify stop exists and identify manual/unknown protection. | Scope by symbol; unscoped request has higher documented weight. | B-02 |
| Algo history | `GET /fapi/v1/allAlgoOrders` | USER_DATA | Recovery/reconciliation evidence. | Apply documented time/history limits and retain local audit events. | B-02 |
| Cancel Algo order(s) | `DELETE /fapi/v1/algoOrder`, `DELETE /fapi/v1/algoOpenOrders` | TRADE | Replace obsolete conditional TP/trailing orders or cancel pending protection only in controlled exit flow. | Never cancel the only confirmed stop until replacement is acknowledged and reconciled. | B-02 |
| Close-all constraint | `closePosition=true` on `STOP_MARKET`/`TAKE_PROFIT_MARKET`. | TRADE | Full-position server-side stop. | Do not send `quantity` or `reduceOnly` with `closePosition=true`. | B-02 |

## User Data and Market Streams

| Stream / action | Method or endpoint | V1 use | Required handling | Source |
|---|---|---|---|---|
| Start/keepalive/close listen key | `POST` / `PUT` / `DELETE /fapi/v1/listenKey` | Phase 8 onwards. | Keep alive before 60 minutes; regenerate on `-1125`/expiry; never log the listen key. | B-05, B-06 |
| User stream | `wss://fstream.binance.com/private/ws/<listenKey>` or documented private routed subscription | Orders, fills, positions, configuration, algo states. | Plan reconnect before 24 hours; use `E` ordering as documented; dedupe despite ordering guarantee. | B-06, B-07 |
| `ORDER_TRADE_UPDATE` | Private user event | Normal order NEW/PARTIAL/FILLED/CANCELED/EXPIRED state. | Persist idempotently; partial fill creates/refreshes protection before management. | B-06 |
| `ALGO_UPDATE` | Private user event | Stop/conditional lifecycle. | `NEW` confirms placement; `REJECTED`, `EXPIRED`, and unexpected disappearance enter protected-failure handling. | B-06 |
| `CONDITIONAL_ORDER_TRIGGER_REJECT` | Private user event | Stop failed when triggered. | Critical incident: reconcile, emergency reduce path, hard halt. | B-06 |
| Public diff depth | `wss://fstream.binance.com/public/...@depth` | Order-book shadow data only. | Buffer, snapshot, accept correct `U/u`, require `pu` continuity, rebuild after gap. | B-07, B-08 |
| Market streams | `wss://fstream.binance.com/market/...` | Mark price, agg trades, other regular feeds. | Route every stream to documented path; no unrouted-path assumption. | B-07 |

## Rate Limits, Errors, Partial Fills, and Reconciliation

| Topic | Required contract | Source |
|---|---|---|
| Rate limits | Central throttle tracks response headers and current `exchangeInfo.rateLimits`. `429` pauses new entries and backs off; repeated violations can become `418`, which hard-halts with no automatic retry. | B-01, B-03 |
| 503 UNKNOWN | The `Unknown error...` 503 variant has unknown execution status. Keep intent/idempotency record, wait for user-stream evidence, query by client ID, reconcile, and only then decide whether a new attempt ID is lawful. | B-01, B-02 |
| Retryable 503 | `Service Unavailable` and documented internal failure variants are failed operations; retry only through bounded backoff, never blind duplicate order submission. | B-01 |
| Timestamp | `-1021` triggers time resync and a bounded signed-request retry only when the operation outcome is known not to be UNKNOWN. | B-01, B-09 |
| Filters | Precision, tick, quantity, notional, symbol-status, and trigger errors cause cache refresh plus plan invalidation. They never increase risk or leverage. | B-03, B-09 |
| Partial fill | `ORDER_TRADE_UPDATE` can report `PARTIALLY_FILLED`; use confirmed Binance position quantity to refresh actual risk and TP allocation. | B-06 |
| Reconciliation bundle | Position risk, account/balance, normal open orders, algo open orders, known order/algo query, and recent trades. | B-02, B-04, B-06 |

## Explicit Non-Selections

- No Binance trading MCP, browser-exposed order endpoint, Codex order tool, or AI order tool.
- No batch orders in the initial uncertainty-sensitive path.
- No testnet/live credential or `/order/test` call before the locked Phase 14 activation flow.
- No market entry for the V1 pilot except an emergency risk-reduction path defined and tested later.
