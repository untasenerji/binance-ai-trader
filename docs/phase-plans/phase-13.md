# Phase 13 Plan: Pre-Live Acceptance

## Scope

- Produce final acceptance evidence for Phases 0-12.
- Publish a read-only D-026 risk-profile preview from the backend and display it in the local Risk Center.
- Publish a local operator user guide and final acceptance report.
- Run the complete quality, security, browser, and Compose validation set.

## Non-goals

- No Phase 14 activation, credential entry or reading, authenticated Binance request, user stream, account query, `/order/test`, real order, signing, or AI provider request.

## Exit Gate

- Phase 0-12 acceptance evidence is green and traceable.
- Risk preview is read-only, sourced from backend hard caps, and cannot mutate configuration or unlock execution.
- The local Connection Wizard remains display-only and disabled.
- Final full validation passes with no critical dependency or secret-scan finding.
