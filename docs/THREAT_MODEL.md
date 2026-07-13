# Threat Model

**Date:** 2026-07-12
**Scope:** Phase 12 local security and chaos validation. The project remains in its pre-live state: `LIVE_TRADING_ENABLED=false`, no credential is present or read, and no authenticated Binance request, user stream, test order, or real order is possible.

## Protected Assets

- Future credential material, signatures, authorization headers, account identifiers, and local secret files.
- Backend hard caps (D-026), no-frequency obligation (D-027), stop-protection invariant, and fail-closed locked adapter with Phase 14 lock.
- Decimal trade-plan and audit evidence, including duplicate-delivery records and audit hash chain.
- Local operator UI, health/control-plane data, logs, metrics, backups, dependency locks, and source integrity.

## Trust Boundaries

| Boundary | Untrusted input or failure | Required protection |
|---|---|---|
| Browser to local API | Malformed UI data, stale UI, local browser compromise | Backend validation remains authoritative; the current UI exposes no trade command or credential field. |
| Public market data to strategy | Stale, missing, inconsistent, or manipulated market information | Freshness and candidate gates can reject an opportunity; D-027 makes no-trade valid. |
| AI advisory input/output | Prompt injection, malformed JSON, tool-escalation attempt | Structured local parser, no tools, no credential input, advisory/veto only, and no mutable risk or execution authority. |
| Local persistence to restart logic | Partial write, tampering, duplicate event, stale projection | Atomic checkpoint write, strict schema decode, audit chain validation, replay/reconciliation gate, and fail-closed result. |
| Process to network | Crash, disconnect, partition, unknown remote state | Pause entries, require reconciliation and stop verification, hard halt when protection is absent or unconfirmed. |
| Dependency supply chain | Known vulnerable Python or Node package | Locked dependencies, `pip-audit`, `npm audit`, secret scanning, and full acceptance checks. |

## Threats And Controls

| ID | Threat | Current mitigation | Residual condition / Phase 14 gate |
|---|---|---|---|
| T-01 | Secret disclosure through source, logs, metrics, or test output | `.gitignore`, Gitleaks, redacting structured logs, label-free metrics, and no credential code path | A compromised local machine is outside the pre-live control plane; never enter a secret before the explicit Phase 14 flow. |
| T-02 | UI or local caller attempts to exceed pilot risk | D-026 backend hard caps reject over-cap input; planner cannot round upward to pass a filter | Future authenticated endpoint must repeat server-side checks immediately before submission. |
| T-03 | Strategy manufactures trades to meet a target count or cadence | D-027 candidate gate requires strategy, risk, data, cost, and validity together; zero trade is correct | Strategy changes must retain deterministic no-trade tests. |
| T-04 | AI gains execution, limit, stop, leverage, or secret authority | Responses contract supplies no tools; AI schema excludes secrets and mutable risk fields; provider is mock-only | Any future provider integration requires a separate locked review and no trade capability. |
| T-05 | Duplicate or unknown order evidence causes duplicate economic action | Idempotency keys, audit delivery dedupe, simulator UNKNOWN handling, and reconciliation-only recovery | A future exchange client must query authoritative state before any retry. |
| T-06 | Crash occurs while a simulated position is open | Atomic local recovery journal, strict schema, partial-fill recovery test, audit/projection checks | `REMOTE_CONFIRMED` is simulated evidence only; actual exchange confirmation is deferred to Phase 14. |
| T-07 | Network partition hides stop loss state | Entries pause, reconciliation and stop re-verification are mandatory; missing or unconfirmed protection hard-halts | No process may infer current remote state while disconnected. |
| T-08 | Audit or recovery evidence is modified or malformed | Database append-only guards, hash-chain head/count checks, strict checkpoint schema, actual replay, and typed reconciliation evidence | A privileged database operator can still tamper; the next verification/replay detects it and requires containment. |
| T-09 | Known dependency vulnerability enters the build | Full Python and Node audit in `scripts/dependency-scan.ps1`; audit findings fail the check | Audits are point-in-time evidence and must run again before every acceptance checkpoint. |
| T-10 | Browser, Codex, or MCP invokes a trade function | No browser command route, MCP trade tool, AI tool, signer, transport, or reachable order implementation | The Phase 14 activation design must preserve this separation and obtain explicit local consent. |

## Security Invariants

1. `LIVE_TRADING_ENABLED` is false and every fail-closed locked-adapter operation rejects before any signer or transport could be reached.
2. A nonzero simulated position must have `REMOTE_CONFIRMED` stop evidence to be locally recovered; otherwise the result hard-halts.
3. A network partition never restores entry authority. It pauses entries and requires reconciliation plus stop re-verification.
4. A locally reconciled checkpoint still reports `entry_authority_enabled=false`; Phase 12 cannot unlock Phase 14.
5. No scan result, recovery test, or UI status is evidence of a real Binance-side stop. That check is intentionally deferred until an explicitly authorized authenticated Phase 14 workflow.

## Out Of Scope

- Defending a fully compromised workstation, operating-system account, or physical disk.
- Production authentication, TLS termination, remote alert delivery, exchange permission checks, or a real user-data stream.
- Any credential entry, signed request, Binance `/order/test`, or matching-engine order.
