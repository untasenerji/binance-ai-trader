# Local Operator Guide

## Purpose

The Phase 13 workspace is a local pre-live review surface. It helps inspect safety status, research evidence, and the immutable pilot-risk preview. It cannot connect an account or submit an order.

## Start The Workspace

With Docker Desktop:

```powershell
docker compose up --build
```

Open `http://localhost:5173`. For the Windows fallback, use the commands in `README.md`; the frontend and backend then run in separate local PowerShell windows.

## Review The Workspace

1. Open **Overview** to confirm `Live execution locked`, local API health, and the control-stream state.
2. Open **Risk Center** to inspect the read-only backend D-026 preview. `ISOLATED`, `ONE_WAY`, the 20 USDT cap, and server-side stop requirement are policy values, not editable UI settings.
3. Use **Market Radar**, **Trade Planner**, **Positions & Orders**, and **Strategy Lab** as local research and safety views. The scenario selector changes only the visual test state; it does not create a plan or contact an exchange.
4. Use **System Health** and **Audit Log** to review local recovery, stream, and audit status.
5. Open **Connection Wizard** only to verify that its Phase 14 activation control is disabled.

## Verify A Build

```powershell
.\scripts\check.ps1
```

The command runs dependency and secret scans, backend checks/tests, frontend checks/tests/build, and Playwright. It must remain green before treating a change as acceptance-ready.

## Safety Rules

- Do not enter, paste, or store a Binance or OpenAI credential in this project during Phase 13.
- Do not interpret a simulated stop, local health badge, or risk preview as a live exchange confirmation.
- A stale/disconnected, missing-stop, unknown, or hard-halt state means no new entry authority. Resolve it through reconciliation in a future explicitly authorized flow.
- Zero trades are normal when strategy, risk, data quality, cost, or validity conditions fail.

Phase 14 is intentionally not started by this guide. It requires a separate explicit user approval and its own local activation checklist.
