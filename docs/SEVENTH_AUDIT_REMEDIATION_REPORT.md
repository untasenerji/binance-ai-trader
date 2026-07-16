# Seventh Independent Audit Remediation Report

**Date:** 2026-07-16
**Base revision:** `2d21ebedd0cc180d04820ccfa3314edf88ff1b72`
**Implementation checkpoint:** `a26ca31` (`fix: remediate seventh independent audit findings`).
**Documentation checkpoint:** Recorded separately after this implementation checkpoint.

## Safety Boundary

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated Binance transport, testnet path, test-order path, or live
  order path was added or invoked.
- `ExchangeAlgoOrderObservation` is a data-only contract. It does not create a transport path.
- Hosted GitHub Actions and external credential-free `live_public` execution are **NOT VERIFIED**.

## Finding Disposition

| # | Release-blocker finding | Remediation and executable evidence | Local status |
|---:|---|---|---|
| 1 | Account-scope portfolio envelope | Added immutable, versioned, append-only V1 `PortfolioEnvelopeHead` state with equity, reserve, total/symbol caps, margin, risk budgets, effective time, supersession, and fingerprint. Every projected entry rechecks every account exposure slice; cross-symbol LONG/SHORT violations and stale envelope plans fail closed. | PASS |
| 2 | Atomic account-scope fills | `record_fill` holds an account lock and PostgreSQL advisory/row locks across envelope head, fills, intents, protection, risk state, and reduction requirements. The concurrent cross-plan PostgreSQL regression proves one unsafe aggregate candidate is durably blocked. | PASS |
| 3 | Durable breaker epochs | Durable safety state carries failure/recovery/envelope/grant-generation epochs. Capabilities are re-derived from the durable state, audit replay, reconciliation, quarantine, and envelope facts; revocation failure fences prior capabilities. | PASS |
| 4 | Migration preflight and recovery | New forward-only `0012_account_scope_safety` quarantines malformed legacy evidence before guard changes, persists resumable markers, restores guards, and retries every injected checkpoint. Published `0009` and prior forward-only `0011` are byte-identical to HEAD. | PASS |
| 5 | Quarantine deny gate | Unresolved quarantine blocks recovery, capability issuance, prepare/retry, and ABSENT acceptance for its economic identity. Resolution is append-only and requires fresh typed evidence plus an operator identity. | PASS |
| 6 | PostgreSQL effective role graph | The migration evaluates transitive role relationships and effective runtime privileges, including the `INHERIT TRUE` / `SET FALSE` counterexample, then verifies runtime UPDATE/DELETE/TRUNCATE/DDL/TEMP denials. | PASS |
| 7 | Exchange observation separation | Local algorithm intents cannot stand in for typed, fresh, authenticated-source exchange observations during reconciliation. | PASS |
| 8 | Protection evidence integrity | Stop evidence is bound to the exact durable stop contract, trigger, side, working type, status, policy/envelope versions, fingerprints, and freshness. | PASS |
| 9 | Strategy provenance | Strategy specifications and fit results are re-fingerprinted, immutable, and allowlisted. Held-out mutations cannot influence train-only selection. | PASS |
| 10 | Canonical secret redaction | Recursive redaction covers encoded, escaped, nested, quoted, separatorless aliases, including sensitive mapping keys and values. | PASS |
| 11 | V1 account scope | A different `account_id` is rejected with `V1_SECOND_ACCOUNT_UNSUPPORTED`; risk, intent, ledger, capability, reconciliation, and lock records use the V1 account scope. | PASS |

## Test And Migration Evidence

- Full deterministic backend suite: **395 passed, 28 deselected, 1 warning**. The one warning is
  Starlette's upstream `httpx` deprecation notice, not a failed control.
- Real local PostgreSQL suite: **27 passed, 396 deselected, 1 warning**.
- Focused populated migration/preflight/retry suite: **24 passed, 8 deliberate PostgreSQL
  deselections**. The separate PostgreSQL suite above includes the real PostgreSQL migration,
  role-graph, and concurrent-fill cases.
- Project validation: `scripts/check.ps1` passed backend format/lint/mypy/tests/coverage gates;
  frontend format/lint/typecheck, **1** unit test, build, and **2** Playwright E2E tests.
- `pre-commit run --all-files` passed all six hooks through the locked backend environment.
- Gitleaks scanned **18 commits** and the working tree: no leaks. Dependency scan found no known
  vulnerabilities.
- `docker compose config` parsed successfully.

## True Branch Coverage

The table reports true branch coverage only. It is not the combined statement-plus-branch
percentage reported by pytest-cov (82.13%).

| Module | Covered branches | True branch coverage | Configured gate |
|---|---:|---:|---|
| Total application | 1253/1892 | 66.23% | 65% PASS |
| `app/simulation/intent_ledger.py` | 352/502 | 70.12% | 70% PASS |
| `app/planning/fills.py` | 89/126 | 70.63% | 70% PASS |
| `app/simulation/simulator.py` | 86/122 | 70.49% | 70% PASS |
| `app/persistence/circuit_breaker.py` | 38/50 | 76.00% | 70% PASS |
| `app/persistence/recovery_service.py` | 8/8 | 100.00% | 70% PASS |
| `app/exchange/contracts.py` | 119/170 | 70.00% | 70% PASS |
| `app/observability/logging.py` | 39/44 | 88.64% | 70% PASS |
| `alembic/versions/0012_account_scope_safety.py` | 49/136 | 36.03% | No migration percentage gate |
| `app/strategy/backtest.py` | 52/154 | 33.77% | No module percentage gate |
| `app/strategy/models.py` | 30/54 | 55.56% | No module percentage gate |

## Exact Validation Results

| Command | Result |
|---|---|
| `uv run pytest --cov-fail-under=80 --cov-report=json:coverage.json -q` | PASS: 395 passed, 28 deselected, 1 warning; combined coverage 82.13%. |
| `uv run pytest -m postgresql --no-cov -q` | PASS: 27 passed, 396 deselected, 1 warning on local PostgreSQL. |
| `uv run python scripts/verify_branch_coverage.py ...` | PASS: global and all seven configured risk-module gates passed. |
| Focused `test_fifth_audit_high_regressions.py` and `test_seventh_audit_persistence_regressions.py` migration run | PASS: 24 passed, 8 PostgreSQL deselected; `0012` branch measurement recorded above. |
| `scripts/check.ps1` | PASS: secret/dependency scans, backend checks, coverage gates, frontend checks, build, and E2E. |
| `uv run --directory backend --locked pre-commit run --all-files` | PASS: all six hooks. |
| `scripts/secret-scan.ps1` | PASS: Gitleaks history and working-tree scans clean. |
| `docker compose config` | PASS. |
| `git diff --check` | PASS after final documentation update. |

## Changed Files

- Runtime and persistence: `backend/alembic/env.py`, new
  `backend/alembic/versions/0012_account_scope_safety.py`, exchange contracts, logging,
  persistence models/database/circuit breaker, fills, simulator models, and durable intent
  ledger.
- Strategy: `backend/app/strategy/backtest.py`, `models.py`, and `strategies.py`.
- Regression coverage: new seventh-audit HIGH/MEDIUM/persistence suites and focused updates to
  existing migration, branch-hardening, exchange, reconciliation, recovery, logging, and prior
  audit suites.
- Documentation: this report and `docs/PHASE_STATUS.md`.

## Pre-Checkpoint Working Tree

`git diff --check` passed with no whitespace errors. Git emitted only Windows CRLF
normalization warnings. The original audit instruction required no commit before reporting, so
this `git status --short` output was captured immediately before the implementation checkpoint:

```text
 M .gitignore
 M backend/alembic/env.py
 M backend/app/exchange/contracts.py
 M backend/app/observability/logging.py
 M backend/app/persistence/circuit_breaker.py
 M backend/app/persistence/database.py
 M backend/app/persistence/models.py
 M backend/app/planning/fills.py
 M backend/app/simulation/intent_ledger.py
 M backend/app/simulation/models.py
 M backend/app/strategy/backtest.py
 M backend/app/strategy/models.py
 M backend/app/strategy/strategies.py
 M backend/tests/test_fifth_audit_high_regressions.py
 M backend/tests/test_fourth_audit_branch_hardening.py
 M backend/tests/test_fourth_audit_high_regressions.py
 M backend/tests/test_locked_exchange_adapter.py
 M backend/tests/test_observability.py
 M backend/tests/test_reconciliation.py
 M backend/tests/test_security_recovery.py
 M backend/tests/test_sixth_audit_high_regressions.py
 M backend/tests/test_third_audit_high_regressions.py
 M docs/PHASE_STATUS.md
?? backend/alembic/versions/0012_account_scope_safety.py
?? backend/tests/test_seventh_audit_high_regressions.py
?? backend/tests/test_seventh_audit_medium_regressions.py
?? backend/tests/test_seventh_audit_persistence_regressions.py
?? docs/SEVENTH_AUDIT_REMEDIATION_REPORT.md
```

## Remaining Verification And Phase Gate

- No seventh-audit code finding remains open in the locally executed deterministic and local
  PostgreSQL scope.
- Hosted GitHub Actions is **NOT VERIFIED**.
- The external `live_public` check is **NOT VERIFIED** and was not run.
- These unverified external controls do not enable Phase 14. Phase 14 remains **LOCKED** and
  requires its separate explicit acceptance process.
