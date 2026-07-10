# Decisions

All locked decisions are listed in `MASTER_SPEC.md` Section 2. Any change requires:

- decision ID,
- reason,
- risk impact,
- migration impact,
- user approval,
- date and source references.

## Phase 0 decision log

| ID | Decision | Status | Approval | Date | Source / impact |
|---|---|---|---|---|---|
| D-026 | V1 pilot profile values are backend-enforced hard ceilings. Frontend values may only lower a ceiling; any higher request is rejected before planning or exchange interaction. | Locked | User instruction | 2026-07-10 | `MASTER_SPEC.md` Sections 1, 2, 9.1.1 and 18; `docs/RISK_FORMULAS.md`; later backend policy tests in Phases 2, 6 and 8. |
| D-027 | The system never creates an order to fill a trade count or meet a trading frequency. A candidate exists only when strategy, risk, data quality, cost, and validity conditions all pass; zero trades is correct when no opportunity passes. Timeframe and scan cadence change search speed, never create an order obligation. | Locked | User instruction | 2026-07-10 | `MASTER_SPEC.md` Sections 2, 6, 8, 9 and 18; enforced by the Phase 5 no-trade baseline and candidate gate tests. |

D-001 through D-025 remain unchanged in `MASTER_SPEC.md` Section 2. D-026 and D-027 do not authorize live trading, secret access, order generation, or a change to Phase 14 activation requirements.
