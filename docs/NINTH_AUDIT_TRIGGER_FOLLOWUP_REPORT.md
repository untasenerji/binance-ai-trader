# Ninth Audit Trigger Follow-up Report

**Date:** 2026-07-22
**Base HEAD:** `4eea4aa` (`docs: record ninth audit checkpoint`)
**Implementation checkpoint:** `4c53a52511176c287913eb738011759cc8922904`
(`fix: verify postgresql fill trigger semantics`).
**Documentation checkpoint:** Recorded separately after this implementation checkpoint.

## Safety Boundary

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated transport, testnet path, or order path was added or invoked.
- Published migrations `0009`, `0011`, `0012`, `0013`, and `0014` remain byte-identical to
  `HEAD`.
- New database behavior is confined to forward-only
  `backend/alembic/versions/0015_postgresql_fill_trigger_semantics.py`.

## Finding Closed

The original `0014` PostgreSQL postcondition accepted a correctly named, enabled trigger even
when its trigger function body had drifted. A red regression replaced
`reject_immutable_fill_mutation()` with `RETURN NEW`; after the `0014` marker was already
recorded, a real `durable_intent_fills` UPDATE succeeded. The red test failed with
`DID NOT RAISE DBAPIError`, proving the reported fail-open behavior before `0015` existed.

`0015_fill_trigger_semantics` closes that gap by:

- Reading each live function with `pg_get_functiondef`, plus catalog security, language, result,
  schema, and function configuration metadata.
- Computing normalized SHA-256 fingerprints for canonical and live definitions. The contract
  includes function body, schema, name, `plpgsql`, `RETURNS trigger`, `SECURITY INVOKER`, and
  the sole `search_path=pg_catalog` configuration.
- Recreating a missing or semantically different current-schema function deterministically,
  including a body changed to `RETURN NEW`, `SECURITY DEFINER`, or a non-canonical search path.
- Verifying and repairing each trigger's table, function OID, schema, enabled status, exact
  row-level `BEFORE UPDATE OR DELETE` type (`tgtype = 27`), zero arguments, and semantic
  trigger definition. Stray same-name trigger bindings are removed from other tables.
- Re-running repair when a migration marker exists but a canonical-function or trigger contract
  no longer matches.
- Running a PostgreSQL savepoint probe during the migration: normal fill and journal INSERTs must
  succeed, protected mutations must raise, and the outer savepoint is rolled back and checked for
  residual probe rows.

`durable_intent_fills` rejects every UPDATE and DELETE. The fill-fact journal rejects every
DELETE and every immutable/economic fact change (including quantity, cumulative quantity, price,
fee, identity, source, provenance, timestamps, and protection flag). Its four durable
application-state fields (`apply_status`, `apply_attempt_count`, `last_apply_error`, `applied_at`)
remain intentionally mutable for the already-established Stage A/Stage B recovery protocol; this
does not alter an economic fill fact. Regressions prove both the rejection of journal price/DELETE
mutations and the continued availability of that constrained application-state transition.

## Regression Evidence

`backend/tests/test_ninth_audit_trigger_followup_regressions.py` covers real PostgreSQL cases for:

- Correct-name ineffective function body and explicit `RETURN NEW` body.
- Missing function, wrong function, wrong function schema, wrong-table trigger, disabled trigger,
  UPDATE-only trigger, DELETE-only trigger, and function-without-trigger.
- Existing marker plus function or trigger semantic drift.
- Exchange fill-fact journal body, wrong-function, and disabled-trigger drift.
- Crash/retry after canonical function repair, trigger binding, and before marker recording.
- Repeated upgrade, actual UPDATE/DELETE denial, normal INSERT acceptance, rollback cleanup, and
  SQLite no-op compatibility.

## Validation Evidence

| Command / package | Result |
|---|---|
| Red-first single regression against pre-`0015` behavior | PASS as a red proof: the expected rejection was absent before the remediation. |
| Focused `0015` PostgreSQL regression suite | PASS: **19 PostgreSQL tests**. |
| Focused `0015` SQLite regression | PASS: **1 test**. |
| Existing SQLite migration/retry package | PASS: **20 passed, 35 deselected**. |
| Existing PostgreSQL migration/retry package, including `0015` | PASS: **34 passed, 17 deselected**. |
| Full real Docker PostgreSQL package | PASS: **75 passed, 498 deselected, 1 upstream warning**. |
| `scripts/check.ps1` | PASS: **497 passed, 76 deselected, 1 upstream warning**; format, lint, mypy, dependency/secret scans, branch gates, frontend format/lint/typecheck/unit/build, and 2 Playwright tests passed. |
| `uv run --directory backend --locked pre-commit run --all-files` | PASS: backend/frontend format, lint, type checks, and secret scan. |
| `scripts/secret-scan.ps1` | PASS: Gitleaks history (**24 commits**) and working-tree scans found no leaks. |
| `docker compose config --quiet` | PASS. |
| `git diff --check` | PASS; Git emitted only informational Windows LF/CRLF conversion warnings. |

The warning is Starlette's upstream `httpx` deprecation warning and is not a failed control.

## Coverage

`scripts/check.ps1` measured **82.79%** combined pytest coverage and **67.78% true branch
coverage (1525/2250)**. The configured 65% global branch threshold and all configured high-risk
module thresholds passed, including `intent_ledger` at 70.32% and `fills` at 70.15%.

## Migration Integrity

Working-tree comparisons against `HEAD` reported no changes for:

- `0009_evidence_risk_hardening.py`
- `0011_forward_invariants.py`
- `0012_account_scope_safety.py`
- `0013_execution_safety_core.py`
- `0014_durable_execution_facts.py`

The only new migration is `0015_postgresql_fill_trigger_semantics.py`, whose revision identifier
is `0015_fill_trigger_semantics` (within the existing 32-character `alembic_version` limit).
The only changes outside that migration are focused regression coverage and updates that teach
legacy migration acceptance tests that `0015` is now the current head.

## Remaining External Scope

- Hosted GitHub Actions has not been run in this follow-up and remains **NOT VERIFIED**.
- No authenticated Binance integration exists or was exercised.
- Phase 14 remains **LOCKED**.
