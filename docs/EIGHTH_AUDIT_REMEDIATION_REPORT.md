# Eighth Independent Audit Remediation Report

**Date:** 2026-07-16
**Audited base revision:** `6da4819bbf8c0fe3fdbd78977d222f299fcf1bc3`
**Implementation checkpoint:** `7303fc54a44b3337f03599b2d3f8e69d649d84e5`
(`fix: remediate eighth independent audit findings`).
**Documentation checkpoint:** Recorded separately after the implementation checkpoint.

## Safety Boundary

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated Binance transport, account access, testnet path,
  test-order path, or live-order path was added or invoked.
- The authenticated-source reconciliation batch is a sealed data contract. It does not provide
  network transport, credentials, or execution authority by itself.
- Published migrations `0009`, `0011`, and `0012` have no working-tree diff. All new schema
  behavior is forward-only migration `0013_execution_safety_core`.
- The checkpoint does not enable execution or relax any Phase 14 gate.

## Red-First Evidence

- Before remediation, the eighth-audit HIGH regression set reproduced eight non-PostgreSQL
  failures. The dedicated PostgreSQL effective-ACL counterexample also failed.
- The reproduced failures covered stale-envelope fill rollback, restart reactivation, envelope
  head fencing, mixed/stale reconciliation provenance, and inherited/column privilege paths.
- No finding below is marked PASS without a final green executable regression on the remediated
  tree.

## Finding Disposition

| # | Audit finding | Remediation and executable evidence | Status |
|---:|---|---|---|
| 1 | A realized fill could roll back when its entry envelope became stale, and restart could restore an active `NEW` intent. | `FillEvent` now carries complete immutable identity, side, timestamp, and observation provenance. A valid exchange fact is persisted before risk authorization is evaluated. Under the same account lock and transaction, cumulative quantity, VWAP, fee, intent state, current-head portfolio risk, entry fencing, reduction requirements, and recovery state are persisted. Stale envelope is an entry-authorization failure, not a fill-rejection reason. Deterministic risk-calculation failure retains the fill and creates durable hard-block/recovery state. LONG/SHORT, full/partial, duplicate/redelivery, restart, concurrent cross-plan, SQLite, PostgreSQL, and all six fill-UoW crash checkpoints pass. | PASS |
| 2 | Publishing a narrower envelope did not atomically fence active intents bound to the old head. | Envelope-head publication now evaluates `PREPARED`, `SUBMITTING`, `UNKNOWN`, `NEW`, and `PARTIALLY_FILLED` risk-increasing intents in the same transaction. Remaining unfilled quantity becomes cancel-required/blocked, prior capability generation is invalidated, and already-realized partial fills remain in accounting. Existing account exposure is re-evaluated against the new head and unsafe state creates durable reduction and recovery requirements. | PASS |
| 3 | Reconciliation maps and mixed local facts could authorize breaker recovery without one bounded exchange query provenance. | Added exact/final `ExchangeReconciliationObservationBatch` with account, epoch, correlation, request/fetch/server times, age/skew bounds, positions, normal orders, Algo observations, and a canonical fingerprint. Breaker, recovery, audit, ledger, and security boundaries require the exact batch type and revalidate freshness after restart. Future, stale, mixed-query, missing/mismatched account/correlation/source, and simulated/local inputs fail closed. | PASS |
| 4 | PostgreSQL runtime SELECT-only policy protection did not prove effective column, PUBLIC, inherited, owner, or role-escalation behavior. | Forward-only `0013` enumerates and constrains table/column ACLs, PUBLIC grants, direct/transitive memberships, role options, owners, predefined write roles, config-owner paths, schema CREATE, and database TEMP. Real `uta_runtime` sessions proved SELECT succeeds while INSERT, every-column UPDATE, REFERENCES, DELETE, TRUNCATE, ALTER, DROP, CREATE TRIGGER, TEMP/shadow paths, and `SET ROLE` escalation fail. | PASS |
| 5 | Many legacy quarantine rows could collapse to one case while orphan rows kept recovery permanently denied. | Added append-only canonical quarantine cases plus durable many-to-one source links retaining every source table/row identity, client/query identity, provenance, and evidence. Backfill links every legacy row. One verified append-only resolution clears the canonical case for every linked source, remains idempotent, and stays resolved after restart. SQLite and real PostgreSQL migration/regression tests pass. | PASS |
| 6 | Strategy specification/fit provenance stopped before candidate gating and plan creation. | Added immutable `StrategyLineage` across fit result, frozen evaluator, `SignalCandidate`, `CandidateGate`, walk-forward result, and `LadderPlan`. Exact allowlisted specification identity, specification hash, fit hash, train-dataset hash, strategy ID/version, and trainer version are revalidated at each boundary. Candidate substitution, post-construction mutation, plan swap, and held-out-data metamorphic cases fail closed or preserve train-only identity as required. | PASS |
| 7 | Migration retry covered only completed checkpoints, not partial DDL. | Forward-only `0013` uses schema introspection, resumable markers, partial-table recovery, deterministic repair, and explicit final postconditions. Tests inject failures for missing indexes/guards, partially added columns/tables/FKs/uniques/triggers, marker-ahead states, and all named checkpoints. SQLite and PostgreSQL both recover from `0008`, `0009`, `0011`, and `0012` to HEAD. | PASS |

## Financial And Restart Invariants

- A realized fill is never discarded because current policy/envelope evaluation rejects further
  risk.
- `filled_quantity > 0` cannot restart as an active `NEW` or `PREPARED` intent.
- Full exchange identity detects exact duplicate delivery idempotently and rejects semantic
  conflict without corrupting quantity, VWAP, fee, exposure, margin, or actual risk.
- A new envelope head can revoke new entry authority but cannot erase existing exposure.
- Risk calculation failure leaves entries fenced and recovery required; it does not fail open.
- Breaker reset and restart recovery require fresh, exact, durable reconciliation evidence.

## Test And Migration Evidence

- Full deterministic non-PostgreSQL backend suite: **474 passed, 51 deselected, 1 warning**.
  Combined statement-plus-branch coverage: **82.71%**.
- Full real PostgreSQL suite on the final tree: **50 passed, 475 deselected, 1 warning**.
- Focused cross-dialect migration/retry coverage:
  - SQLite: **70 passed, 17 deselected**.
  - PostgreSQL: **17 passed, 70 deselected**.
- Focused runtime policy-table ACL test: **1 passed, 2 deselected**.
- Targeted eighth-audit branch hardening: **36 passed**.
- The single warning is Starlette's upstream `httpx` deprecation notice; it is not a failed
  safety control.
- `scripts/check.ps1` passed secret/dependency scans, Python format/lint/mypy, backend tests and
  both coverage gates, frontend format/lint/typecheck, one unit test, production build, and two
  Playwright E2E tests.
- `pre-commit run --all-files` passed all six configured hooks through the locked backend
  environment.
- Gitleaks scanned **20 commits** and the worktree: no leaks. Dependency scan found no known
  vulnerabilities.
- `docker compose -f compose.yaml config --quiet` passed.

## PostgreSQL Effective ACL Evidence

The final real-session tests use the actual `uta_runtime` identity, not only
`has_table_privilege` assertions.

- SELECT on all protected policy/evidence tables succeeds.
- Table and every-column INSERT/UPDATE/REFERENCES privileges are absent and actual statements
  fail.
- DELETE, TRUNCATE, ALTER, DROP, and CREATE TRIGGER fail.
- PUBLIC-derived and inherited column privileges are rejected by migration postconditions.
- `SET ROLE uta_policy_config`, `SET ROLE pg_write_all_data`, and
  `SET ROLE pg_database_owner` fail.
- PUBLIC schema CREATE and database TEMP are absent; temporary shadow-table construction fails.
- Runtime audit append permissions remain sufficient for canonical audit delivery while
  UPDATE/DELETE/TRUNCATE/DDL remain denied.

## True Branch Coverage

These are true branch ratios, not the combined pytest-cov percentage.

| Module | Covered branches | True branch coverage | Gate/result |
|---|---:|---:|---|
| Total application | 1376/2032 | 67.72% | 65% PASS |
| `app/simulation/intent_ledger.py` | 377/534 | 70.60% | 70% PASS |
| `app/simulation/simulator.py` | 86/122 | 70.49% | 70% PASS |
| `app/planning/fills.py` | 91/130 | 70.00% | 70% PASS |
| `app/persistence/circuit_breaker.py` | 38/50 | 76.00% | 70% PASS |
| `app/exchange/contracts.py` | 164/228 | 71.93% | 70% PASS |
| `app/persistence/recovery_service.py` | 8/8 | 100.00% | 70% PASS |
| `app/security/recovery.py` | 40/46 | 86.96% | Reported risk module; PASS |
| `app/strategy/models.py` | 55/76 | 72.37% | Reported risk module; PASS |
| `app/strategy/gate.py` | 17/22 | 77.27% | Reported risk module; PASS |
| `alembic/versions/0012_account_scope_safety.py` | 91/136 | 66.91% | No migration percentage gate |
| `alembic/versions/0013_execution_safety_core.py` | 127/170 | 74.71% | No migration percentage gate |

## Exact Validation Results

| Command | Result |
|---|---|
| `uv run pytest --cov-fail-under=80 --cov-report=json:eighth-audit-coverage.json --junitxml=test-results/eighth-audit-junit.xml -q` | PASS: 474 passed, 51 deselected, combined coverage 82.71%. |
| `uv run pytest -m postgresql --no-cov -q` | PASS: 50 passed, 475 deselected on real PostgreSQL. |
| `uv run coverage run --parallel-mode --branch --source=alembic -m pytest tests/test_eighth_audit_migration_regressions.py tests/test_seventh_audit_persistence_regressions.py tests/test_sixth_audit_high_regressions.py --no-cov -m "not postgresql and not live_public" -q` | PASS: 70 passed, 17 deselected on SQLite. |
| The same migration command with `-m postgresql` | PASS: 17 passed, 70 deselected on real PostgreSQL. |
| `uv run pytest tests/test_postgresql_runtime_role.py -k runtime_policy_tables_are_effectively_select_only -m postgresql --no-cov -q` | PASS: actual `uta_runtime` policy/evidence mutations and escalation paths denied. |
| `uv run python scripts/verify_branch_coverage.py ...` | PASS: application and all configured risk-module gates passed; additional requested modules are reported above. |
| `scripts/check.ps1` | PASS: complete local backend/frontend/security/build/E2E acceptance chain. |
| `uv run --directory backend --locked pre-commit run --all-files` | PASS: all six hooks. |
| `scripts/secret-scan.ps1` | PASS: Gitleaks history and worktree clean. |
| `docker compose -f compose.yaml config --quiet` | PASS. |
| `git diff --check` | PASS; Git emitted only informational Windows LF/CRLF normalization warnings. |
| Published migration diff for `0009`, `0011`, and `0012` | PASS: empty. |

## Changed Files

- Forward migration: new `backend/alembic/versions/0013_execution_safety_core.py` only.
- Exchange/recovery contracts: `backend/app/exchange/contracts.py`, locked adapter, audit,
  circuit breaker, recovery service, and security recovery.
- Durable execution safety: persistence database/models, fills/risk planning, intent ledger, and
  simulator.
- Strategy lineage: strategy models, strategies, backtest, gate, and planning lineage binding.
- Test infrastructure: package marker plus test-only reconciliation and strategy factories.
- Regression coverage: new eighth-audit HIGH/MEDIUM/migration/branch suites and focused updates
  to existing financial, persistence, recovery, reconciliation, simulator, and strategy tests.
- Documentation: this report.

## Pre-Commit Git Status

The following status was captured before the implementation checkpoint. Generated coverage,
test-result, build, and cache artifacts remained ignored.

```text
 M backend/app/exchange/contracts.py
 M backend/app/exchange/locked_adapter.py
 M backend/app/persistence/audit.py
 M backend/app/persistence/circuit_breaker.py
 M backend/app/persistence/database.py
 M backend/app/persistence/models.py
 M backend/app/persistence/recovery_service.py
 M backend/app/planning/fills.py
 M backend/app/planning/risk.py
 M backend/app/security/recovery.py
 M backend/app/simulation/intent_ledger.py
 M backend/app/simulation/simulator.py
 M backend/app/strategy/backtest.py
 M backend/app/strategy/gate.py
 M backend/app/strategy/models.py
 M backend/app/strategy/strategies.py
 M backend/tests/conftest.py
 M backend/tests/test_backtest_funding.py
 M backend/tests/test_backtest_temporal_integrity.py
 M backend/tests/test_candidate_gate.py
 M backend/tests/test_directional_risk.py
 M backend/tests/test_durable_unknown_ledger.py
 M backend/tests/test_fifth_audit_high_regressions.py
 M backend/tests/test_fill_risk_and_exits.py
 M backend/tests/test_financial_properties.py
 M backend/tests/test_fourth_audit_branch_hardening.py
 M backend/tests/test_fourth_audit_high_regressions.py
 M backend/tests/test_fourth_audit_medium_regressions.py
 M backend/tests/test_locked_exchange_adapter.py
 M backend/tests/test_persistence.py
 M backend/tests/test_persistence_breaker.py
 M backend/tests/test_planning.py
 M backend/tests/test_planning_envelope.py
 M backend/tests/test_postgresql_runtime_role.py
 M backend/tests/test_reconciliation.py
 M backend/tests/test_recovery_evidence.py
 M backend/tests/test_second_audit_high_regressions.py
 M backend/tests/test_security_recovery.py
 M backend/tests/test_seventh_audit_high_regressions.py
 M backend/tests/test_seventh_audit_persistence_regressions.py
 M backend/tests/test_simulation.py
 M backend/tests/test_simulator_actual_risk_gate.py
 M backend/tests/test_simulator_fill_matrix.py
 M backend/tests/test_sixth_audit_high_regressions.py
 M backend/tests/test_sixth_audit_medium_regressions.py
 M backend/tests/test_strategy_lab.py
 M backend/tests/test_third_audit_high_regressions.py
 M backend/tests/test_third_audit_medium_regressions.py
 M backend/tests/test_walkforward_isolation.py
 M backend/tests/test_walkforward_train_only.py
?? backend/alembic/versions/0013_execution_safety_core.py
?? backend/tests/__init__.py
?? backend/tests/reconciliation_factory.py
?? backend/tests/strategy_factory.py
?? backend/tests/test_eighth_audit_branch_hardening.py
?? backend/tests/test_eighth_audit_high_regressions.py
?? backend/tests/test_eighth_audit_medium_regressions.py
?? backend/tests/test_eighth_audit_migration_regressions.py
?? docs/EIGHTH_AUDIT_REMEDIATION_REPORT.md
```

## Remaining External Verification And Phase Gate

- Hosted GitHub Actions: **NOT VERIFIED**.
- External credential-free `live_public`: **NOT VERIFIED** and deliberately not run.
- No locally reproduced eighth-audit finding remains open in deterministic or real local
  PostgreSQL scope.
- These local PASS results do not unlock execution. Phase 14 remains **LOCKED** pending its
  separate explicit acceptance process.
