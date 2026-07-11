# Operations Runbook

## Scope Boundary

This runbook applies to the local Phase 12 control plane. `LIVE_TRADING_ENABLED` remains false. No procedure here authorizes credential entry, Binance connection, test order, or real order.

## Structured Logs

- Emit logs through `app.observability.logging.StructuredLogger`.
- Use an event name plus structured fields; values for secret-bearing keys and recognizable credential text are redacted before the sink receives them.
- Do not use labels or account identifiers in metrics or alert dedupe keys.
- Treat an attempted secret-bearing log field as a defect even when redaction masks it.

## Metrics And Alerts

- Local metrics are exposed at `GET /api/metrics` as label-free plain text.
- Metrics report operational state only: execution lock, control-plane phase, recovery blocks, alert delivery/suppression, clock skew, and local health conditions.
- Phases 11-13 use only `InMemoryAlertAdapter`; no email, chat, webhook, broker, or trading adapter is configured.
- Alert dedupe is keyed by an explicit safe incident code. A repeat inside the configured window is recorded as suppressed rather than redelivered.

## Fault Response

| Condition | Immediate state | Required follow-up |
|---|---|---|
| Database write unavailable | New entries blocked; critical alert | Restore durable storage, reconcile local audit state, then reset the persistence breaker only after a successful reconciliation. |
| Disk full | New entries blocked; critical alert | Free verified local capacity, validate backup readiness, restore durable writes, then reconcile. |
| Clock skew above local threshold | New entries paused; resync required | Refresh trusted time source in a future authorized exchange phase; do not create an order while offset is unresolved. |
| Restart with mismatch | New entries paused; reconciliation required | Replay local audit, compare future exchange truth when available, and confirm stop protection before any later activation. |
| Restart with unconfirmed protection | Hard halt | Keep entry authority disabled and require containment/reconciliation workflow. |
| Network partition | New entries paused; stop re-verification required | Preserve only last confirmed protection evidence, restore connectivity, reconcile, and re-verify protection before a future activation. |

## Backup And Restore Readiness

1. Keep new entry authority disabled and verify the append-only audit hash chain.
2. Create a local database backup outside Git, for example under ignored `backups/`.
3. Record the generated `BackupManifest`: timestamp, audit-event count, final record hash, and chain-verification result.
4. Validate a restore in an isolated local database profile before treating a backup as recoverable.
5. After restore, compare event count and final record hash with the manifest, verify the complete chain, replay projections, and keep all entry authority paused until reconciliation is clean.

For the optional local Docker PostgreSQL profile, a non-production backup command is:

```powershell
docker compose --profile storage exec -T db pg_dump -U postgres -d uta -Fc > backups\uta-local.dump
```

The backup directory is intentionally ignored. Do not copy secrets, `.env` files, keyring material, or account identifiers into a backup report or test artifact.
