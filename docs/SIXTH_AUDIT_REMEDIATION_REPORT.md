# Sixth Independent Audit Remediation Report

**Date:** 2026-07-15
**Base revision:** `cd3b4dc48dd4fd571654acd56aa901cf0d708ec3`
**Implementation checkpoint:** `0e336b798988c0236745d4a5d7edebb6a51ebb24`
(`fix: remediate sixth independent audit findings`).

## Safety Boundary

- Phase 14 remains **LOCKED**.
- No API key, signer, authenticated Binance transport, account endpoint, testnet path,
  test-order path, or live-order path was added or invoked.
- Simulator protection remains rehearsal evidence. It is never promoted to
  exchange-confirmed evidence; breaker recovery still requires a typed exchange snapshot.
- Hosted GitHub Actions and the external `live_public` check were not run: **NOT VERIFIED**.

## Red-First Evidence

- The initial non-PostgreSQL HIGH regression file produced 6 failures, 1 pass, and 1
  PostgreSQL deselection before remediation. The PostgreSQL transitive-role counterexample
  also failed independently.
- The initial MEDIUM regression set produced 5 failures and 5 passes. The durable capability
  subset initially produced 3 failures.
- Crash checkpoints were injected after fill persistence, before and after protection
  evaluation, after actual-risk calculation, and before and after pending cancellation.
- The final PostgreSQL sweep exposed one additional cross-account-scope portfolio defect.
  A dedicated SQLite regression reproduced it before the policy query was scoped by
  `account_scope`.
- No finding below is marked `LOCAL PASS` without a green executable regression.

## Finding Disposition

| # | Finding | Remediation and evidence | Status |
|---:|---|---|---|
| 1 | Aggregate plan risk reused candidate-plan caps, equity, and leverage. | Added immutable/versioned `AccountPortfolioEnvelope` plus independently margined exposure slices. Aggregate margin is the sum of each slice's notional divided by its own leverage, plus one reserve. Conflicting versions/fingerprints fail closed, while distinct account scopes remain independent. LONG/SHORT, strict/loose order permutations, confirmed, partially filled, pending, and proposed states are covered. | LOCAL PASS |
| 2 | Fill, protection, actual risk, reduction requirement, and pending cancellation were not atomic. | These writes now share one transaction. Every blocked active entry becomes `CANCEL_REQUIRED`; crash injection proves rollback or complete commit with no intermediate restart state. Late and duplicate terminal fills remain idempotent and update VWAP, fees, quantity, and risk. | LOCAL PASS |
| 3 | Published migration `0009` must remain unchanged while existing installations gain current invariants. | The checkpoint keeps `0009` byte-identical to base `HEAD`; its canonical Git blob is `06c396c69f96ca6bf1f5b556f36ac13cbb3cae4f`. All sixth-remediation migration behavior lives in forward-only `0011_forward_invariants`, which introspects legacy state, normalizes or quarantines ambiguous query evidence, resolves duplicates deterministically, reinstalls/verifies append-only guards and constraints, and is retry-safe after partial SQLite DDL. | LOCAL PASS |
| 4 | PostgreSQL runtime effective privileges were not transitively constrained. | `0011` rejects direct and multi-level `SET ROLE` paths to config owner, table owner, predefined write, or other write-capable roles; normalizes runtime flags; revokes CREATE/TEMP; and leaves immutable policy/evidence tables SELECT-only. Failed role-graph migrations clean up and then reach HEAD. | LOCAL PASS |
| 5 | Stop reconciliation accepted an ID without the full protective contract. | Added exact typed stop contracts covering plan, symbol, position and inverse order sides, ID, STOP_MARKET type, Decimal trigger, working type, close-position and quantity semantics, active status, policy/envelope versions, and fingerprints. Every mismatch is dirty; same-symbol multi-plan swaps fail. | LOCAL PASS |
| 6 | Walk-forward training could depend on arbitrary factory/process state. | Walk-forward accepts only serializable allowlisted `StrategySpecification` values and immutable train-slice fit results. Arbitrary callables are rejected before invocation. Held-out mutation leaves train/config fingerprints unchanged; direct and walk-forward funding behavior remains equal. | LOCAL PASS |
| 7 | Structured and encoded secret aliases could evade redaction. | Mapping and text paths now use one canonical alias set, bounded to eight decode layers, 65,536 text characters, depth 24, collection size 4,096, and 16,384 visited nodes. Separatorless, escaped, quoted, percent-encoded, Basic/Bearer, and nested forms fail closed. | LOCAL PASS |
| 8 | Process-local breaker state could authorize entries without durable authority. | Every entry check derives a short-lived typed capability from an append-only grant, current audit hash/replay/projection, clean stored exchange reconciliation, unresolved-intent state, and current envelope fingerprints. Failures durably revoke grants; private state mutation and stale recovery probes cannot authorize. | LOCAL PASS |

## Test And Migration Evidence

- Deterministic backend total: **380 passed** across disjoint local suites:
  **356 non-PostgreSQL passed** and **24 PostgreSQL passed**. The one credential-free
  `live_public` test was deliberately not run and remains **NOT VERIFIED**.
- Published populated `0009 -> HEAD`, fresh HEAD, schema equivalence, partial-DDL retry,
  replay/recovery, and role-graph matrix: **10 passed** on SQLite and real PostgreSQL.
- Populated `0008 -> HEAD` with append-only guard handling and replay/recovery:
  **3 passed** on SQLite and real PostgreSQL.
- Six fill/protection/cancel transaction crash checkpoints: **6 passed**.
- SHORT multi-stage property tests and portfolio order permutations: **7 passed**.
- Complete LONG/SHORT stop mismatch and same-symbol binding matrix: **20 passed**.
- Walk-forward held-out metamorphic isolation and funding parity: **3 passed**.
- Adversarial redaction matrix: **28 passed**.
- Durable breaker/capability tests: **15 passed**.
- The working copy of `0009_evidence_risk_hardening.py` has no canonical content diff from
  base `HEAD`; both resolve to Git blob `06c396c69f96ca6bf1f5b556f36ac13cbb3cae4f`.

## Coverage Evidence

The current deterministic non-PostgreSQL app suite reached 82.26% combined
statement/branch coverage. True branch coverage is reported separately and passed the
project gate at **66.36% (`1136/1712`)**.

| Risk module | Covered branches | True branch coverage | Gate |
|---|---:|---:|---:|
| `app/planning/fills.py` | 77/108 | 71.30% | 70% PASS |
| `app/simulation/intent_ledger.py` | 274/390 | 70.26% | 70% PASS |
| `app/simulation/simulator.py` | 86/122 | 70.49% | 70% PASS |
| `app/persistence/circuit_breaker.py` | 34/44 | 77.27% | 70% PASS |
| `app/persistence/recovery_service.py` | 8/8 | 100.00% | 70% PASS |
| `app/exchange/contracts.py` | 104/134 | 77.61% | 70% PASS |
| `app/observability/logging.py` | 35/40 | 87.50% | 70% PASS |

Migration coverage was measured separately from the app gate. The focused cross-dialect
migration suite covered 82/142 branches (57.75%) in `0011_forward_invariants.py`; no
migration-specific percentage threshold is configured. Migration acceptance is based on the
green populated-upgrade, replay/recovery, retry, invariant, and real-role tests above.

## Exact Validation Results

| Command | Result |
|---|---|
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1` | PASS: Gitleaks history/worktree and dependency scans clean; format, lint, mypy clean; backend 356 passed/25 deselected; app true branches 1136/1712; all risk-module gates passed; frontend format/lint/type/unit/build and 2 E2E tests passed. |
| `uv run --directory backend --locked pytest -m postgresql --no-cov -q` | PASS: 24 passed, 357 deselected on real PostgreSQL. |
| Focused populated `0009`, fresh/legacy schema, retry, and role-graph command | PASS: 10 passed, 41 deselected. |
| Focused populated `0008` SQLite/PostgreSQL command | PASS: 3 passed, 16 deselected. |
| Focused transaction crash command | PASS: 6 passed, 45 deselected. |
| Focused financial property/permutation command | PASS: 7 passed, 46 deselected. |
| Focused stop reconciliation matrix command | PASS: 20 passed, 31 deselected. |
| Focused walk-forward/funding command | PASS: 3 passed, 58 deselected. |
| Focused redaction command | PASS: 28 passed, 25 deselected. |
| Focused durable breaker/capability command | PASS: 15 passed, 11 deselected. |
| `docker compose config --quiet` | PASS. |
| `uv run --directory backend --locked pre-commit run --all-files` | PASS: all six hooks passed. |
| `git diff --check` | PASS after the final documentation update. |

## PostgreSQL Role Results

- Direct config-owner membership: migration rejected the effective `SET ROLE` path.
- Three-level config-owner membership: rejected.
- Two-level predefined `pg_write_all_data` membership: rejected.
- Two-level custom policy-table owner membership: rejected.
- After each rejection and role cleanup, the same database upgraded to HEAD.
- Existing tests also passed runtime INSERT/UPDATE/DELETE/TRUNCATE/DDL, TEMP/shadow-table,
  trigger mutation, and policy-version forgery denials.

## Changed Files

- Forward migration/runtime: `backend/alembic/env.py`, new `0011_forward_invariants.py`,
  exchange contracts, logging, audit/breaker/database/models,
  portfolio fills, durable intent ledger, simulator, and strategy models/backtest/strategies.
- Tests: fixture and prior durable UNKNOWN, breaker, reconciliation, recovery, strategy, and
  walk-forward suites; new `test_sixth_audit_high_regressions.py` and
  `test_sixth_audit_medium_regressions.py`.
- Documentation: this report and `docs/PHASE_STATUS.md`.

## Remaining Verification

- No sixth-audit code finding remains open in the local deterministic scope.
- Hosted GitHub Actions: **NOT VERIFIED**.
- External credential-free `live_public` Binance connectivity: **NOT VERIFIED**.
- Phase 14 remains **LOCKED** and requires its existing explicit acceptance process.

## Git Checkpoint

The implementation checkpoint is `0e336b798988c0236745d4a5d7edebb6a51ebb24`.
The verified pre-checkpoint `git status --short` output was:

```text
 M backend/alembic/env.py
 M backend/app/exchange/contracts.py
 M backend/app/observability/logging.py
 M backend/app/persistence/audit.py
 M backend/app/persistence/circuit_breaker.py
 M backend/app/persistence/database.py
 M backend/app/persistence/models.py
 M backend/app/planning/fills.py
 M backend/app/simulation/intent_ledger.py
 M backend/app/simulation/simulator.py
 M backend/app/strategy/backtest.py
 M backend/app/strategy/models.py
 M backend/app/strategy/strategies.py
 M backend/tests/conftest.py
 M backend/tests/test_durable_unknown_ledger.py
 M backend/tests/test_fifth_audit_high_regressions.py
 M backend/tests/test_fourth_audit_branch_hardening.py
 M backend/tests/test_fourth_audit_high_regressions.py
 M backend/tests/test_fourth_audit_medium_regressions.py
 M backend/tests/test_persistence_breaker.py
 M backend/tests/test_reconciliation.py
 M backend/tests/test_security_recovery.py
 M backend/tests/test_strategy_lab.py
 M backend/tests/test_third_audit_high_regressions.py
 M backend/tests/test_third_audit_medium_regressions.py
 M backend/tests/test_walkforward_isolation.py
 M backend/tests/test_walkforward_train_only.py
 M docs/PHASE_STATUS.md
?? backend/alembic/versions/0011_forward_invariants.py
?? backend/tests/test_sixth_audit_high_regressions.py
?? backend/tests/test_sixth_audit_medium_regressions.py
?? docs/SIXTH_AUDIT_REMEDIATION_REPORT.md
```
