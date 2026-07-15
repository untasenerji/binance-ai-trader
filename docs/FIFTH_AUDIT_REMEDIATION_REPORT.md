# Fifth Independent Audit Remediation Report

**Date:** 2026-07-15
**Base revision:** `b4a3dc86b350e3a997b8d0470a70f62057ae9021`
**Commit:** Not created, as requested.

## Safety Boundary

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated Binance transport, testnet path, account endpoint, test order, or real-order path was added or invoked.
- Simulation evidence remains distinct from exchange-confirmed evidence. Durable local stop identity is only an expected reconciliation fact; a typed exchange snapshot is still required.

## Red-First Evidence

- Named regressions reproduced the cross-plan `9 USDT @ 1x + 200 USDT @ 100x` margin error, non-atomic post-fill pending state, writable breaker state, invalid UNKNOWN timelines, forged policy lineage, unprovable custom trainers, and missing durable Algo-stop expectation before their fixes.
- SQLite and PostgreSQL populated-0008 migration tests exercised nullable legacy fields and append-only trigger management. The config-owner assertion first failed with `postgres != uta_policy_config`; the forward migration then made the test green.
- The medium redaction suite initially exposed five encoded/quoted secret leaks before bounded recursive decoding was implemented.
- No item below is marked `LOCAL PASS` without an executable green regression.

## Finding Disposition

| # | Finding | Remediation and evidence | Status |
|---:|---|---|---|
| 1 | Cross-plan margin reused the new plan leverage. | Portfolio margin now sums each confirmed and pending LONG/SHORT plan notional divided by that plan's own durable leverage. The required `9 + 200/100 = 11` counterexample is rejected against equity 10. | LOCAL PASS |
| 2 | Fill, risk block, and pending cancellation were not atomic. | Fill accounting, actual-risk recomputation, durable block/reduction state, and cancellation of known active entry stages share one transaction. Rollback and restart tests prove no filled/open intermediate state survives. | LOCAL PASS |
| 3 | Breaker state could be opened by constructor or assignment. | Breaker state is private, constructor state injection is unavailable, and `halted_reason` is read-only. Only defined trip and verified-recovery methods mutate it. | LOCAL PASS |
| 4 | UNKNOWN evidence lacked causal time validation. | Submit, UNKNOWN, query start/end, observation, watermark, nonnegative duration, identity, and provenance are revalidated both on write and restart. | LOCAL PASS |
| 5 | Populated 0008 migration could conflict with append-only evidence. | `0009` temporarily manages the guard in the migration transaction, normalizes legacy query identity/time fields, reinstalls and verifies the guard, and supports real SQLite/PostgreSQL populated upgrades. | LOCAL PASS |
| 6 | Runtime could forge policy versions and loader lineage was incomplete. | Runtime policy tables are SELECT-only; a separate NOLOGIN config owner owns policy tables/sequence. Loader checks base identity, contiguous versions, immutable fingerprints, symbol, side, stop/exit references, leverage, and risk budget lineage. | LOCAL PASS |
| 7 | Walk-forward custom trainers could use hidden held-out state. | Walk-forward accepts only exact reviewed built-in trainers; arbitrary custom trainers fail closed before fitting. Funding settlement behavior remains equal to direct backtest behavior. | LOCAL PASS |
| 8 | Encoded and structured secrets could leak. | Recursive redaction handles escaped JSON, Unicode keys, single quotes, Basic/Bearer values, nested mappings, and repeated percent encoding with an eight-layer fail-closed bound. | LOCAL PASS |
| 9 | Reconciliation lacked durable expected Algo-stop identity. | The policy's durable stop reference is loaded as the local expected Algo ID; typed snapshot evidence must still match symbol, side, namespace, `closePosition`, and position direction. Missing stops remain fail-closed. | LOCAL PASS |
| 10 | Existing PostgreSQL runtime roles could retain dangerous flags or ownership. | `0009` normalizes role flags and TEMP restrictions; `0010` hardens already-upgraded databases, removes config-role membership, and assigns policy ownership to the separate NOLOGIN role. Downgrade/re-upgrade is tested on SQLite and PostgreSQL. | LOCAL PASS |
| 11 | New high-risk branches lacked targeted evidence. | Financial, transaction rollback/restart, simulator, ledger, breaker, logging, and migration tests cover the reported counterexamples. Existing branch-only module gates remain enforced. | LOCAL PASS |

## Open External Verification

- Hosted GitHub Actions was not run from this local workspace: **NOT VERIFIED**.
- The credential-free `live_public` Binance smoke test was deliberately excluded from the deterministic full suite. A direct attempt in this environment failed during TLS negotiation (`WRONG_VERSION_NUMBER`), so external public connectivity is **NOT VERIFIED** rather than PASS.
- No local code finding from the fifth audit remains open. These external checks do not unlock Phase 14.

## Changed Files

- Migrations/runtime: `backend/alembic/versions/0009_evidence_risk_hardening.py`, new `0010_runtime_policy_ownership.py`, `backend/app/observability/logging.py`, `backend/app/persistence/circuit_breaker.py`, `backend/app/simulation/intent_ledger.py`, and `backend/app/strategy/backtest.py`.
- Tests: new `test_fifth_audit_high_regressions.py` and `test_fifth_audit_medium_regressions.py`; prior UNKNOWN, recovery, policy-role, strategy, and walk-forward tests were updated for the stricter contracts.
- Documentation: this report and `docs/PHASE_STATUS.md`.

## Test And Migration Evidence

- PostgreSQL-inclusive deterministic suite: **313 passed, 1 live-public deselected, 1 warning**.
- PostgreSQL-only suite: **18 passed, 296 deselected, 1 warning**.
- Migration-focused SQLite/PostgreSQL suite: **9 passed**.
- Populated `0008 -> HEAD` ran against real SQLite and PostgreSQL, then current replay/recovery and append-only mutation rejection passed.
- `0010` downgrade/re-upgrade ownership and runtime privilege behavior passed on both SQLite and PostgreSQL.

## Coverage Evidence

The PostgreSQL-inclusive app suite reached 85.43% combined statement/branch coverage. True branch coverage is reported separately: **71.56% (`1039/1452`)**.

| Module | Covered branches | True branch coverage |
|---|---:|---:|
| `app/planning/fills.py` | 53/70 | 75.71% |
| `app/simulation/intent_ledger.py` | 209/286 | 73.08% |
| `app/simulation/simulator.py` | 88/124 | 70.97% |
| `app/persistence/circuit_breaker.py` | 22/24 | 91.67% |
| `app/persistence/recovery_service.py` | 8/8 | 100.00% |
| `app/exchange/contracts.py` | 74/80 | 92.50% |
| `app/observability/logging.py` | 28/30 | 93.33% |
| `app/strategy/backtest.py` | 110/150 | 73.33% |

Migration coverage was measured separately because the production app coverage source is `app`, not Alembic revisions:

| Migration | Covered branches | True branch coverage |
|---|---:|---:|
| `0009_evidence_risk_hardening.py` | 43/66 | 65.15% |
| `0010_runtime_policy_ownership.py` | 8/8 | 100.00% |

## Exact Validation Results

| Command | Result |
|---|---|
| `uv run --directory backend --locked pytest -m "not live_public" --cov-fail-under=80 --cov-report=json:fifth-audit-coverage.json --cov-report=xml:fifth-audit-coverage.xml --junitxml=test-results/fifth-audit-junit.xml -q` | PASS: 313 passed, 1 deselected; 85.43% combined coverage |
| `uv run --directory backend --locked python scripts/verify_branch_coverage.py ...` | PASS: 71.56% true branches; every configured module gate passed |
| `uv run --directory backend --locked pytest -m postgresql --no-cov -q` | PASS: 18 passed, 296 deselected |
| Migration-focused `coverage run --branch --source=alembic/versions ...` | PASS: 9 passed; `0009` 43/66 and `0010` 8/8 true branches |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1` | PASS: Gitleaks history/worktree clean across 14 commits; dependency scans clean; 127 backend files formatted; lint and mypy (115 files) clean; backend 295 passed/19 deselected, 85.12% combined and 71.07% true branch coverage; frontend format/lint/type-check/unit/build and 2 E2E tests passed |
| `uv run --directory backend --locked pre-commit run --all-files` | PASS: all hooks |
| Gitleaks history and working-tree scans | PASS: no leaks |
| `docker compose config --quiet` | PASS |
| `git diff --check` | PASS |

## Git Status

No commit was created. The final `git status --short` is intentionally dirty with the requested remediation:

```text
 M backend/alembic/versions/0009_evidence_risk_hardening.py
 M backend/app/observability/logging.py
 M backend/app/persistence/circuit_breaker.py
 M backend/app/simulation/intent_ledger.py
 M backend/app/strategy/backtest.py
 M backend/tests/test_durable_unknown_ledger.py
 M backend/tests/test_fourth_audit_high_regressions.py
 M backend/tests/test_recovery_evidence.py
 M backend/tests/test_second_audit_high_regressions.py
 M backend/tests/test_security_recovery.py
 M backend/tests/test_strategy_lab.py
 M backend/tests/test_third_audit_high_regressions.py
 M backend/tests/test_walkforward_isolation.py
 M backend/tests/test_walkforward_train_only.py
 M docs/PHASE_STATUS.md
?? backend/alembic/versions/0010_runtime_policy_ownership.py
?? backend/tests/test_fifth_audit_high_regressions.py
?? backend/tests/test_fifth_audit_medium_regressions.py
?? docs/FIFTH_AUDIT_REMEDIATION_REPORT.md
```
