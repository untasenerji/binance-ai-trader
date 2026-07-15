# Fourth Independent Audit Remediation Report

**Date:** 2026-07-15
**Base revision:** `e19628eb75fe4b74d43286656d25865a359a9875`
**Implementation checkpoint:** `971aa6f830763c05837468d22d9cce6c58f6c66f` (`fix: remediate fourth independent audit findings`).

## Safety Boundary

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated Binance transport, account endpoint, test order, or real-order path was added or invoked.
- `SimulatedProtectionEvidence` and `ExchangeProtectionEvidence` are separate exact types. The simulator can produce only rehearsal readiness; it cannot create exchange-confirmed position, stop, or reduce-only evidence.
- The future live evidence gate accepts only the exact exchange evidence type and has no activation or submission behavior.

## Red-First Evidence

- The initial HIGH regression run reproduced 14 non-PostgreSQL failures and three PostgreSQL failures before implementation changes.
- Separate red tests reproduced the recovery terminology/type substitution, caller-authored UNKNOWN observation, policy revision identity, class scalar, and arithmetically derived scalar counterexamples before their fixes.
- The initial MEDIUM run reproduced ten failures; further global-holder/global-scalar/converted-scalar counterexamples each failed before isolation hardening.
- No finding below is marked locally passed without a later green executable test.

## Finding Disposition

| Priority | Finding | Remediation and evidence | Status |
|---|---|---|---|
| High | Rehearsal proof could masquerade as exchange-confirmed protection. | Separate non-substitutable evidence models, rehearsal-only recovery schema v3, side-correct typed algo stops, and an exact-type future live gate. | LOCAL PASS |
| High | Pending, cross-plan, symbol, total, margin, reserve, and post-fill risk were incomplete for LONG/SHORT. | Durable portfolio recomputation runs before acceptance and after every fill. Better-fill breaches cancel/block pending entries and create a durable typed reduction requirement. | LOCAL PASS |
| High | Terminal late/duplicate fills could lose quantity, VWAP, fees, or risk. | Exact duplicate detection precedes lifecycle checks; authoritative ABSENT/REJECTED/CANCELLED late fills persist, semantic conflicts reject, and restart rebuilds all Decimal accounting. | LOCAL PASS |
| High | Breaker reset and reconciliation could accept caller/override claims or a wrong-side stop. | `PersistenceRecoveryService` requires concrete repository/ledger/snapshot types and calls the concrete repository method. Reconciliation derives every mismatch and requires SELL stops for LONG and BUY stops for SHORT. | LOCAL PASS |
| High | UNKNOWN absence proof was caller-authored or causally weak. | Public observation injection was removed. The simulator query service derives durable client/economic/attempt/namespace/time/fingerprint provenance; all five sources and repeated windows are required, and restart restores unresolved intents. | LOCAL PASS |
| High | Legacy populated migrations and replay could diverge. | Alembic `0006` writes all required reconciliation fields and normalizes replayable legacy payloads; `0009` adds hardened evidence/policy state. Valid and adversarial populated-0001 upgrades run on SQLite and PostgreSQL through HEAD and current replay. | LOCAL PASS |
| High | PostgreSQL runtime could mutate durable risk policy. | Base and version records are append-only, loaded fingerprints are reverified, runtime has only required SELECT/INSERT privileges, and the runtime principal is not a superuser. | LOCAL PASS |
| Medium | Walk-forward funding and train-only isolation were bypassable. | Funding settlements are passed unchanged. Pre-fit custom state, globals, class/slot state, held-out objects, copied/converted/derived scalars, and opaque state fail closed; reviewed built-in trainers use an exact allowlist. | LOCAL PASS |
| Medium | Encoded, quoted, header, or nested secrets could leak. | Recursive redaction covers escaped/double/single-quoted assignments, repeated URL decoding, Basic/Bearer values, and nested structured values. | LOCAL PASS |
| Medium | Risky branches lacked module evidence. | Branch-only gates now cover fills, intent ledger, simulator, breaker, concrete recovery/reconciliation service, exchange reconciliation contracts, and logging separately. | LOCAL PASS |
| Low | CI actions used mutable major tags and status docs were stale. | Every remote action is pinned to a full commit SHA; stale checkpoint and simulated-evidence wording is corrected. | LOCAL PASS; hosted CI pending |

## Changed Files

- Workflow/scripts: `.github/workflows/ci.yml`, `scripts/check.ps1`.
- Migrations/persistence: `backend/alembic/versions/0006_legacy_audit_repair_and_evidence.py`, new `0009_evidence_risk_hardening.py`, `backend/app/persistence/{circuit_breaker,database,models,recovery_service}.py`.
- Safety/runtime: `backend/app/exchange/contracts.py`, `backend/app/planning/fills.py`, `backend/app/security/recovery.py`, `backend/app/simulation/{intent_ledger,simulator}.py`.
- Research/logging/coverage: `backend/app/strategy/backtest.py`, `backend/app/observability/logging.py`, `backend/scripts/verify_branch_coverage.py`.
- Tests: updated fixtures and prior audit/simulation/migration/recovery tests; new `test_fourth_audit_high_regressions.py`, `test_fourth_audit_medium_regressions.py`, and `test_fourth_audit_branch_hardening.py`.
- Documentation: `PLAN.md`, `docs/PHASE_STATUS.md`, `docs/THREAT_MODEL.md`, prior audit/phase reports, and this report.

## PostgreSQL Evidence

`uv run pytest -m postgresql -q --no-cov` passed 13 tests (274 deselected). These include populated-0001 migration/replay, restart recovery, concurrent audit-chain behavior, runtime-role grants and denials, non-superuser identity, append-only policy versions, and durable risk-reduction state.

## Coverage Evidence

The PostgreSQL-inclusive suite passed 286 tests with one deliberate credential-free `live_public` deselection. Coverage.py total coverage was 85.54%; true branch coverage was 71.84% (`1013/1410`). These metrics are reported separately.

| Module | Covered branches | True branch coverage |
|---|---:|---:|
| `app/planning/fills.py` | 53/70 | 75.71% |
| `app/simulation/intent_ledger.py` | 180/252 | 71.43% |
| `app/simulation/simulator.py` | 88/124 | 70.97% |
| `app/persistence/circuit_breaker.py` | 22/24 | 91.67% |
| `app/persistence/recovery_service.py` | 8/8 | 100.00% |
| `app/exchange/contracts.py` | 74/80 | 92.50% |
| `app/observability/logging.py` | 23/24 | 95.83% |

## Exact Validation Commands

| Command | Result |
|---|---|
| `uv run ruff format --check .` | PASS |
| `uv run ruff check .` | PASS |
| `uv run mypy` | PASS: 113 source files |
| `uv run pytest -q --no-cov tests/test_fourth_audit_high_regressions.py tests/test_fourth_audit_medium_regressions.py tests/test_fourth_audit_branch_hardening.py tests/test_durable_unknown_ledger.py tests/test_simulator_actual_risk_gate.py tests/test_security_recovery.py tests/test_persistence_breaker.py tests/test_migrations.py` | PASS: 80 passed, 4 deselected (before the final branch/isolation additions, which are included in the full suite) |
| `uv run pytest -m postgresql -q --no-cov` | PASS: 13 passed, 274 deselected |
| `uv run pytest -m "not live_public" --cov-fail-under=80 --cov-report=json:fourth-audit-coverage.json --cov-report=xml:fourth-audit-coverage.xml --junitxml=test-results/fourth-audit-junit.xml` | PASS: 286 passed, 1 deselected; 85.54% total coverage |
| `uv run python scripts/verify_branch_coverage.py --coverage-json fourth-audit-coverage.json --minimum 65 --module app/planning/fills.py=70 --module app/simulation/intent_ledger.py=70 --module app/simulation/simulator.py=70 --module app/persistence/circuit_breaker.py=70 --module app/persistence/recovery_service.py=70 --module app/exchange/contracts.py=70 --module app/observability/logging.py=70` | PASS: 71.84% true branches and every module gate passed |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1` | PASS: Gitleaks clean across 12 commits and the worktree; no known Python/Node vulnerabilities; 124 files formatted; lint/mypy clean; backend 273 passed and 14 PostgreSQL/live-public deselected; 85.28% total and 71.35% true branch coverage (`1006/1410`); every module gate passed; frontend format/lint/type-check/Vitest/build and 2 Playwright tests passed |
| `uv run --directory backend --locked pre-commit run --all-files` | PASS: backend format/lint/type-check, frontend lint/type-check, and secret scan |
| `docker compose config --quiet` | PASS |
| `git diff --check` | PASS |

## Git Evidence

`git diff --check` completed with exit code 0. The verified pre-checkpoint worktree was:

```text
 M .github/workflows/ci.yml
 M PLAN.md
 M backend/alembic/versions/0006_legacy_audit_repair_and_evidence.py
 M backend/app/exchange/contracts.py
 M backend/app/observability/logging.py
 M backend/app/persistence/circuit_breaker.py
 M backend/app/persistence/database.py
 M backend/app/persistence/models.py
 M backend/app/planning/fills.py
 M backend/app/security/recovery.py
 M backend/app/simulation/intent_ledger.py
 M backend/app/simulation/simulator.py
 M backend/app/strategy/backtest.py
 M backend/scripts/verify_branch_coverage.py
 M backend/tests/conftest.py
 M backend/tests/test_branch_coverage_gate.py
 M backend/tests/test_ci_gitleaks_install.py
 M backend/tests/test_durable_unknown_ledger.py
 M backend/tests/test_migrations.py
 M backend/tests/test_persistence_breaker.py
 M backend/tests/test_second_audit_high_regressions.py
 M backend/tests/test_security_recovery.py
 M backend/tests/test_simulation.py
 M backend/tests/test_simulator_actual_risk_gate.py
 M backend/tests/test_third_audit_high_regressions.py
 M docs/AUDIT_REMEDIATION_REPORT.md
 M docs/PHASE_STATUS.md
 M docs/SECOND_AUDIT_REMEDIATION_REPORT.md
 M docs/THIRD_AUDIT_REMEDIATION_REPORT.md
 M docs/THREAT_MODEL.md
 M docs/test-reports/phase-12.md
 M docs/test-reports/phase-13.md
 M scripts/check.ps1
?? backend/alembic/versions/0009_evidence_risk_hardening.py
?? backend/app/persistence/recovery_service.py
?? backend/tests/test_fourth_audit_branch_hardening.py
?? backend/tests/test_fourth_audit_high_regressions.py
?? backend/tests/test_fourth_audit_medium_regressions.py
?? docs/FOURTH_AUDIT_REMEDIATION_REPORT.md
```

## Remaining Boundary

All fourth-audit code findings are locally closed by executable local evidence. Hosted GitHub Actions is **NOT VERIFIED**. No claim in this report authorizes Phase 14 or constitutes real Binance evidence.
