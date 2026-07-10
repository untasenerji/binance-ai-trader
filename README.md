# Binance AI Trader

Phase 1 provides a local development skeleton only. It includes a health endpoint, a deliberately locked dashboard shell, quality checks, and Docker/Windows startup paths. No Binance credentials, authenticated Binance requests, order submission, test orders, or OpenAI tool access are implemented.

## Prerequisites

- Docker Desktop with Docker Compose, or
- Python 3.12, `uv`, Node.js 24, npm, and Gitleaks.

## Docker Compose

From the repository root:

```powershell
docker compose up --build
```

The web shell is available at `http://localhost:5173` and the only active API route is `http://localhost:8000/api/health`.

Stop the stack with:

```powershell
docker compose down
```

If Docker Desktop was installed in the current terminal session, open a new PowerShell window before using the `docker` command.

## Windows fallback

```powershell
.\scripts\bootstrap.ps1
.\scripts\dev-backend.ps1
```

Open a second PowerShell window in the repository and run:

```powershell
.\scripts\dev-frontend.ps1
```

## Verification

```powershell
.\scripts\check.ps1
```

The exact individual commands are maintained in `AGENTS.md`. The Phase 1 scope and safety decisions remain governed by `PLAN.md`, `MASTER_SPEC.md`, and `docs/DECISIONS.md`.
