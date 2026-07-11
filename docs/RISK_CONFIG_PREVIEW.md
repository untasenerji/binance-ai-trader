# Risk Configuration Preview

**Status:** Read-only Phase 13 display
**Authority:** D-026 backend hard caps
**Endpoint:** `GET /api/risk-config-preview`

This preview renders the immutable V1 live-pilot ceiling from `app.domain.risk.DEFAULT_HARD_CAPS`. It accepts no request body or configuration value, does not read a credential, and cannot unlock execution. UI input may later request a lower value, but any request above a hard cap must be rejected by the backend rather than silently changed.

| Control | Preview value | Enforcement |
|---|---:|---|
| Profile | `live_pilot_20_usdt` | Read-only identifier |
| Pilot equity cap | 20 USDT | Backend hard cap |
| Margin type | ISOLATED | Fixed V1 policy |
| Position mode | ONE_WAY | Fixed V1 policy |
| Maximum leverage | 2x | Backend hard cap |
| Concurrent positions | 1 | Backend hard cap |
| Active strategies | 1 | Backend hard cap |
| Maximum stages | 2 | Backend hard cap |
| Risk per trade | 0.10 USDT | Backend hard cap |
| Daily loss limit | 0.30 USDT | Backend hard cap |
| Weekly drawdown limit | 0.80 USDT | Backend hard cap |
| Consecutive-loss limit | 3 | Backend hard cap |
| Market entry | Disabled | Fixed V1 policy |
| Back-loaded ladder | Disabled | Fixed V1 policy |
| Server-side stop | Required | Fixed V1 policy |
| OpenAI mode | Advisory | No execution authority |
| OpenAI daily budget | 0.02 USD | Backend hard cap |

The preview is not a profit target and it is not evidence that any exchange setting has been checked. If filters or minimum notional make a plan unsafe under these values, D-027 requires a no-trade outcome.
