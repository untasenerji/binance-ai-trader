# AGENTS.md — Repository Rules

## Source of truth

Read `MASTER_SPEC.md`, `PLAN.md`, `docs/DECISIONS.md`, and `docs/PHASE_STATUS.md` before making changes.

## Safety boundaries

- Never read, print, commit, copy, summarize, or expose real `.env` values, API secrets, signatures, auth headers, keyring records, database credentials, or account identifiers.
- Never send a live Binance order unless Phase 14 is explicitly active and the local user completes the activation flow.
- Never expose a Binance order/trade tool to OpenAI, Codex, MCP, or the browser frontend.
- Never increase risk, leverage, stage count, or pilot equity cap to make an order pass exchange minimums.
- Decimal only for all trading calculations.
- One-way + isolated only in V1.
- No martingale or uncontrolled averaging down.

## Documentation rules

- Binance behavior: official Binance Developer Docs only.
- OpenAI behavior: use the OpenAI Developer Docs MCP first.
- Mark unverified assumptions explicitly. Do not silently invent defaults.
- Update `docs/SOURCES.md` when an API contract changes.

## Validation commands

Codex must add and maintain exact commands here after scaffolding:

```text
backend format: uv run --directory backend --locked ruff format --check .
backend lint: uv run --directory backend --locked ruff check .
backend typecheck: uv run --directory backend --locked mypy
backend tests: uv run --directory backend --locked pytest
public market smoke: uv run --directory backend --locked pytest -m live_public
PostgreSQL migration: docker compose --profile storage run --rm -e DATABASE_URL=postgresql+psycopg://postgres@db:5432/uta api uv run --no-sync alembic -c alembic.ini upgrade head
frontend format: npm --prefix frontend run format:check
frontend lint: npm --prefix frontend run lint
frontend typecheck: npm --prefix frontend run typecheck
frontend tests: npm --prefix frontend run test
E2E Playwright: npm --prefix frontend run test:e2e
security scan: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\secret-scan.ps1
full acceptance: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1
```

## Phase discipline

- Work on one phase at a time.
- Add/update `docs/test-reports/phase-XX.md`.
- Do not mark a phase complete with skipped failing tests.
- If a test cannot run, record the precise blocker and keep the phase open.
