# Security Scan Record

**Date:** 2026-07-11
**Scope:** All locked Python and Node dependencies used by the Phase 12 working tree, plus the repository secret scan.

| Command | Result | Action |
|---|---|---|
| `uv run --directory backend --locked pip-audit` | PASS: `No known vulnerabilities found` | An initial audit identified `pytest 8.4.2` (`PYSEC-2026-1845`); the project constraint was raised to `pytest>=9.0.3,<10.0`, lock refreshed to 9.1.1, and the audit was rerun successfully. |
| `npm --prefix frontend audit --audit-level=high` | PASS: `found 0 vulnerabilities` | The scan covers production and development dependencies. |
| `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\secret-scan.ps1` | PASS: Gitleaks reported `no leaks found` in the final acceptance run | Gitleaks remains an enforced part of `scripts/check.ps1`; no secret is read or printed by this record. |

`scripts/dependency-scan.ps1` fails on a nonzero audit result and is invoked by `scripts/check.ps1`. It does not exclude development dependencies.
