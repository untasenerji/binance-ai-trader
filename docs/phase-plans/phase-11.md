# Phase 11 Plan: Operations, Observability, and Notifications

## Scope

- Structured redacted JSON log contract, safe local metrics endpoint, daily operations report, local alert adapter contract, alert deduplication, recovery decisions, and backup/restore readiness.

## Non-goals

- No secret logging, account labels, network notification adapter, broker transport, Binance/OpenAI credential, external telemetry SDK, webhook, order tool, test order, or real order.

## Exit Gate

- Logs redact secret-bearing fields before sinks receive them.
- Metrics carry no account labels and include no credential value.
- Repeated alerts are deduplicated deterministically.
- Database outage, disk-full, unsafe clock skew, and restart/protection uncertainty all block new entries and expose a recovery decision.
- Backup readiness requires a valid audit hash chain; restore verification checks count, final hash, and full chain.
