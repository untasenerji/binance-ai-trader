# Phase 04 Plan: Persistence, Replay, and Audit

## Scope

- SQLAlchemy models and Alembic migration for local audit, dedupe ledger, projections, and reconciliation runs.
- Append-only audit hash chain and duplicate delivery behavior.
- Restart replay and an explicit persistence-failure circuit breaker.

## Non-goals

- No account, position, order, user-stream, credential, signature, or exchange reconciliation call.

## Exit Gate

- SQLite unit/migration tests and PostgreSQL Docker migration succeed.
- Duplicate delivery remains audit-visible but causes one state projection.
- Database write failure blocks new entries until reconciliation clears the breaker.
