# Second Independent Audit Remediation Report

**Date:** 2026-07-13
**Base revision:** `022631d5e91823976d4c5a5063bc5fa217ed5a55`
**Checkpoint commit:** `bc05bac` (`fix: remediate second independent audit findings`)
**Checkpoint state:** The remediation is committed locally. No push was created.

## Safety State

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated transport, account endpoint, test order, or real-order path was added or invoked.
- `LIVE_TRADING_ENABLED=false` remains enforced by the fail-closed adapter and is covered by recursive architecture tests.

## Finding Disposition

Every implementation finding received a failing focused regression before its correction. A result marked `LOCAL PASS` has executable local evidence below; it is not a claim about an unrun hosted environment.

| Priority | Finding | Evidence | Status |
|---|---|---|---|
| High | SHORT notional, exposure, and margin used a lower adverse fill price. | `test_second_audit_high_regressions.py`, `test_financial_properties.py` prove commitment uses the conservative maximum of entry and adverse fill for each SHORT stage. | LOCAL PASS |
| High | Breaker reset accepted caller-supplied evidence. | `test_persistence_breaker.py`, `test_recovery_evidence.py`, and `test_second_audit_high_regressions.py` require a repository probe, replay, reconciliation, and durable-ledger evidence. | LOCAL PASS |
| High | `ReconciliationOutcome.is_clean` trusted only reason codes. | Regression verifies all mismatch collections independently make the outcome dirty. | LOCAL PASS |
| High | UNKNOWN absence resolution was caller-controlled and non-durable. | `test_durable_unknown_ledger.py` and second-audit regressions require persisted, repeated evidence from normal/algo orders, trade history, stream watermark, and position snapshot sources. | LOCAL PASS |
| High | Fill accounting lacked multi-stage VWAP, fees, semantic conflict handling, and restart persistence. | `test_simulator_fill_matrix.py`, `test_simulator_actual_risk_gate.py`, and property tests cover durable facts, cumulative fills, exact duplicate versus conflict, restart, and actual-risk entry blocking. | LOCAL PASS |
| High | Audit hash omitted delivery status, semantic fingerprint, and chain sequence/head evidence. | `test_audit_chain_atomicity.py` and replay tests cover protected metadata and head `last_sequence`. | LOCAL PASS |
| High | A populated 0001 database could not upgrade safely. | SQLite populated-0001 upgrade test plus `test_postgresql_runtime_role.py::test_populated_postgresql_0001_database_upgrades_to_replayable_head`. | LOCAL PASS |
| High | PostgreSQL runtime identity had excessive database power. | PostgreSQL role/GRANT/REVOKE test proves a restricted `uta_runtime` role can append allowed evidence but cannot mutate, delete, truncate, or perform DDL. | LOCAL PASS |
| High | The reported branch gate used combined coverage. | `scripts/verify_branch_coverage.py` reads only `covered_branches / num_branches`; `test_branch_coverage_gate.py` protects the distinction. | LOCAL PASS |
| High | Walk-forward did not prove train-only fit/freeze. | `test_walkforward_train_only.py` requires a training-slice fit and a frozen strategy with matching training boundary/configuration fingerprint. | LOCAL PASS |
| Medium | Semantic conflicts could reach the reducer before durable audit evidence. | `test_audit_chain_atomicity.py` proves malformed semantic conflict is recorded without a reducer failure. | LOCAL PASS |
| Medium | Backtests permitted overlapping positions; funding lacked symbol/timeframe identity. | `test_backtest_temporal_integrity.py` and `test_backtest_funding.py`. | LOCAL PASS |
| Medium | Basic credentials, URL-encoded assignments, and event text could leak to logs. | `test_observability.py` uses canaries for all three paths. | LOCAL PASS |
| Medium | Backend CI expected Gitleaks without installing it. | `test_ci_gitleaks_install.py` requires an ordered, checksum-verified install before backend tests; the exact Linux command was smoke-tested in a container. | LOCAL PASS; hosted run pending |
| Low | Locked-adapter static check missed nested modules and aliases. | `test_locked_adapter_architecture.py` creates a nested aliased transport/network bypass and requires recursive AST detection. | LOCAL PASS |
| Low | Financial property coverage missed non-power-of-ten increments, SHORT, and multi-stage fills. | Hypothesis properties cover `0.003` step size, multi-stage SHORT commitment, VWAP, fees, and stop risk. | LOCAL PASS |

## Validation Evidence

| Command | Result |
|---|---|
| `uv run --directory backend --locked pytest -m "not live_public" --cov-fail-under=80 --junitxml=test-results/junit.xml --cov-report=xml:coverage.xml --cov-report=json:coverage.json -q` | PASS: 200 passed, 1 deselected; PostgreSQL acceptance and populated-0001 migration included; total coverage 83.01%. |
| `uv run --directory backend --locked python scripts/verify_branch_coverage.py --coverage-json coverage.json --minimum 65` | PASS: true branch coverage 65.49% (`702/1072`). |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1` | PASS: 193 passed, 8 deselected; total coverage 82.80%; true branch coverage 65.11% (`698/1072`); frontend format/lint/type-check/Vitest/build and 2 Playwright scenarios passed; Gitleaks and dependency scans passed. |
| `backend/.venv/Scripts/pre-commit.exe run --all-files` | PASS: backend format/lint/type-check, frontend lint/type-check, and secret scan passed. |
| `docker compose config --quiet` | PASS. |
| Linux container equivalent of the CI Gitleaks install step | PASS: downloaded Gitleaks 8.30.0, verified SHA-256, extracted it, and executed the binary. |

The 80% pytest threshold is total coverage and is deliberately reported as such. The independent true-branch gate is 65% and uses only branch counters. No combined statement-plus-branch value is called branch coverage.

## Hosted CI Evidence Boundary

The workflow now installs a pinned Gitleaks 8.30.0 release in the backend job, verifies its SHA-256, exposes it through `GITHUB_PATH`, and runs the backend secret-scan contract after installation. The exact installation sequence passed in a Linux container.

GitHub Actions itself was **not** run for the remediation later checkpointed as `bc05bac` with documentation follow-up `6296e85`; therefore hosted execution is **NOT VERIFIED** and is not recorded as a pass. A hosted run requires an explicit push or equivalent CI submission. This does not authorize Phase 14.

## Remaining Gate

The second audit remediation is locally validated. Phase 14 remains locked pending its separate explicit authorization and its existing live-environment checklist; this report does not request or use credentials.
